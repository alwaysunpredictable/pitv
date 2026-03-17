#!/usr/bin/env python3
"""
Video player daemon.
- Polls SQLite for play requests
- Launches mpv when a video is queued
- Handles stop_requested flag
- Calls reset_to_idle() when done (rotates PIN, clears controller)
"""
import json, subprocess, time, sqlite3, random, logging, os
from pathlib import Path

BASE         = Path(__file__).parent.parent
DATA         = BASE / "data"
DB           = DATA / "state.db"
REQUEST_FILE = DATA / "request.json"
COMMAND_FILE = DATA / "command.txt"

logging.basicConfig(
    filename=Path(__file__).parent / "player.log",
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ── SQLite helpers ────────────────────────────────────────────────────────────
def _con():
    con = sqlite3.connect(DB, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    return con

def kget(k, default=None):
    con = _con()
    row = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    con.close()
    return row[0] if row else default

def kset_many(pairs: dict):
    con = _con()
    for k, v in pairs.items():
        con.execute(
            "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, "" if v is None else str(v)),
        )
    con.commit()
    con.close()

# ── Wayland / display environment ────────────────────────────────────────────
def _get_display_env():
    """
    The player runs as a systemd service with no display env.
    Grab WAYLAND_DISPLAY and XDG_RUNTIME_DIR from the labwc process
    so mpv can open a Wayland window on top of Chromium.
    """
    env = os.environ.copy()

    # Try to read from labwc process environment
    try:
        pid_out = subprocess.check_output(
            ["pgrep", "-x", "labwc"], text=True
        ).strip().split()[0]
        with open(f"/proc/{pid_out}/environ", "rb") as f:
            for item in f.read().split(b"\0"):
                if b"=" in item:
                    k, _, v = item.partition(b"=")
                    env[k.decode(errors="replace")] = v.decode(errors="replace")
    except Exception as e:
        logging.warning("Could not read labwc env: %s", e)

    # Fallback: common defaults on Raspberry Pi OS
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")

    logging.info("Display env — WAYLAND_DISPLAY=%s XDG_RUNTIME_DIR=%s",
                 env.get("WAYLAND_DISPLAY"), env.get("XDG_RUNTIME_DIR"))
    return env

# ── Audio detection ───────────────────────────────────────────────────────────
def _detect_hdmi_sink():
    """Return the first HDMI PulseAudio/PipeWire sink name, or None."""
    try:
        out = subprocess.check_output(
            ["pactl", "list", "short", "sinks"],
            stderr=subprocess.DEVNULL, text=True,
        )
        for line in out.splitlines():
            if "hdmi" in line.lower():
                parts = line.split()
                if len(parts) >= 2:
                    return parts[1]
    except Exception:
        pass
    return None

def _set_audio_defaults():
    sink = _detect_hdmi_sink()
    if not sink:
        logging.warning("No HDMI sink found, using default audio")
        return
    logging.info("Using audio sink: %s", sink)
    for cmd in (
        ["pactl", "set-default-sink", sink],
        ["pactl", "set-sink-mute",   "@DEFAULT_SINK@", "0"],
        ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "70%"],
    ):
        try:
            subprocess.run(cmd, check=False, stderr=subprocess.DEVNULL)
        except Exception:
            pass

# ── State ─────────────────────────────────────────────────────────────────────
def _new_pin():
    old = kget("pin", "")
    while True:
        p = f"{random.randint(1000, 9999)}"
        if p != old:
            return p

def reset_to_idle():
    for f in (REQUEST_FILE, COMMAND_FILE):
        try: f.unlink()
        except FileNotFoundError: pass
    kset_many({
        "mode":             "idle",
        "pin":              _new_pin(),
        "controller_token": "",
        "stop_requested":   "0",
        "now_title":        "",
        "now_poster":       "",
    })
    logging.info("Reset to idle, new PIN: %s", kget("pin"))

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    DATA.mkdir(parents=True, exist_ok=True)
    logging.info("Player daemon started")
    reset_to_idle()
    _set_audio_defaults()

    mpv_proc    = None
    last_hb     = 0.0
    display_env = None   # populated lazily when labwc is running

    while True:
        now = time.time()

        # Heartbeat once per second
        if now - last_hb >= 1.0:
            try:
                con = _con()
                con.execute(
                    "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    ("player_ts", str(int(now))),
                )
                con.commit(); con.close()
            except Exception:
                pass
            last_hb = now

        # Picking timeout — reset if no video chosen within 2 minutes
        if kget("mode") == "picking":
            try:
                since = int(kget("picking_since", "0") or "0")
                if since and (time.time() - since) > 120:
                    logging.info("Picking timeout — resetting to idle")
                    reset_to_idle()
            except Exception:
                pass

        # Check stop flag
        if mpv_proc and mpv_proc.poll() is None:
            if kget("stop_requested", "0") == "1":
                logging.info("Stop requested — terminating mpv")
                try: mpv_proc.terminate()
                except Exception: pass

        # Pick up a new play request
        if REQUEST_FILE.exists() and (mpv_proc is None or mpv_proc.poll() is not None):
            # Discard stale request if DB says we are not playing (e.g. after reboot reset)
            if kget("mode") != "playing":
                REQUEST_FILE.unlink(missing_ok=True)
                logging.info("Discarded stale request.json (mode is not playing)")
                time.sleep(0.3)
                continue
            try:
                req = json.loads(REQUEST_FILE.read_text())
            except Exception:
                REQUEST_FILE.unlink(missing_ok=True)
                time.sleep(0.3)
                continue
            REQUEST_FILE.unlink(missing_ok=True)

            path  = req.get("path", "")
            title = req.get("title", "")
            logging.info("Playing: %s", title)

            # Re-detect audio and display env
            _set_audio_defaults()
            display_env = _get_display_env()

            # Clear any stop flag before starting (admin override leaves it set)
            kset_many({"stop_requested": "0"})

            try:
                mpv_proc = subprocess.Popen(
                    [
                        "/usr/bin/mpv",
                        "--fs",
                        "--no-osc",
                        "--really-quiet",
                        "--no-sub",
                        "--sub-auto=no",
                        "--gpu-context=wayland",
                        "--", path,
                    ],
                    env=display_env,
                )
            except Exception as e:
                logging.error("mpv launch failed: %s", e)
                reset_to_idle()
                mpv_proc = None

        # Video finished
        if mpv_proc and mpv_proc.poll() is not None:
            logging.info("Playback ended (exit code %s)", mpv_proc.returncode)
            if not REQUEST_FILE.exists():
                reset_to_idle()
            mpv_proc = None

        time.sleep(0.25)

if __name__ == "__main__":
    main()
