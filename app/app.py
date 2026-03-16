from flask import (
    Flask, request, jsonify, send_file,
    render_template, redirect, url_for, session, abort, Response
)
from pathlib import Path
import os, sqlite3, random, json, secrets, socket, io, subprocess, logging
from functools import wraps

APP = Flask(__name__)

BASE   = Path(__file__).parent.parent
MEDIA  = BASE / "media"
DATA   = BASE / "data"
DB     = DATA / "state.db"
REQUEST_FILE = DATA / "request.json"
COMMAND_FILE = DATA / "command.txt"
CONFIG = BASE / "config.env"

# ── Config ────────────────────────────────────────────────────────────────────
def _load_config():
    cfg = {}
    if CONFIG.exists():
        for line in CONFIG.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    return cfg

_cfg = _load_config()
APP.secret_key = _cfg.get("APP_SECRET") or secrets.token_hex(32)
ADMIN_PASS     = _cfg.get("ADMIN_PASS", "changeme")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    filename=Path(__file__).parent / "app.log",
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

def _client_ip():
    for h in ("CF-Connecting-IP", "X-Forwarded-For"):
        v = request.headers.get(h)
        if v:
            return v.split(",")[0].strip()
    return request.remote_addr

# ── Network ───────────────────────────────────────────────────────────────────
def _local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.254.254.254", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

def web_url():
    return f"http://{_local_ip()}:9000"

# ── SQLite ────────────────────────────────────────────────────────────────────
def _con():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
    con.commit()
    return con

def kget(k, default=None):
    con = _con()
    row = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    con.close()
    return row[0] if row else default

def kset(k, v):
    con = _con()
    con.execute(
        "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (k, "" if v is None else str(v)),
    )
    con.commit()
    con.close()

def kset_many(pairs: dict):
    con = _con()
    for k, v in pairs.items():
        con.execute(
            "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, "" if v is None else str(v)),
        )
    con.commit()
    con.close()

# ── State ─────────────────────────────────────────────────────────────────────
def _ensure_init():
    if kget("initialized"):
        return
    kset_many({
        "mode":             "idle",
        "pin":              f"{random.randint(1000, 9999)}",
        "controller_token": "",
        "stop_requested":   "0",
        "now_title":        "",
        "now_poster":       "",
        "initialized":      "1",
    })

def get_state():
    _ensure_init()
    return {
        "mode":             kget("mode", "idle"),
        "pin":              kget("pin",  "0000"),
        "controller_token": kget("controller_token", ""),
        "stop_requested":   kget("stop_requested", "0"),
        "now_title":        kget("now_title", ""),
        "now_poster":       kget("now_poster", ""),
    }

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

def _boot_reset():
    try:
        _ensure_init()
        reset_to_idle()
    except Exception:
        pass

_boot_reset()

# ── Media ─────────────────────────────────────────────────────────────────────
_VIDEO  = {".mp4", ".mkv", ".mov", ".avi"}
_POSTER = {".webp", ".png", ".jpg", ".jpeg"}

def _first(folder, exts):
    hits = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts]
    return sorted(hits, key=lambda p: p.name.lower())[0] if hits else None

def list_titles():
    MEDIA.mkdir(parents=True, exist_ok=True)
    out = []
    for sub in sorted([p for p in MEDIA.iterdir() if p.is_dir()], key=lambda x: x.name.lower()):
        vid    = _first(sub, _VIDEO)
        poster = _first(sub, _POSTER)
        if not vid or not poster:
            continue
        out.append({
            "key":    sub.name,
            "title":  sub.name,
            "poster": poster.relative_to(MEDIA).as_posix(),
            "video":  vid.relative_to(MEDIA).as_posix(),
        })
    return out

def _safe_media(rel: str) -> Path:
    p = (MEDIA / rel).resolve()
    if not str(p).startswith(str(MEDIA.resolve()) + os.sep):
        raise PermissionError("path escape")
    if not p.is_file():
        raise FileNotFoundError
    return p

# ── Session helpers ───────────────────────────────────────────────────────────
def _is_controller(st=None):
    st = st or get_state()
    tok = st["controller_token"]
    return bool(tok) and session.get("token") == tok

def require_controller(f):
    @wraps(f)
    def inner(*a, **kw):
        if not _is_controller():
            return redirect(url_for("home"))
        return f(*a, **kw)
    return inner

# ── QR ────────────────────────────────────────────────────────────────────────
def _make_qr(url: str) -> bytes:
    import qrcode
    qr = qrcode.QRCode(box_size=12, border=3)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0B0B10", back_color="#F0F2FF")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

# ── Routes ────────────────────────────────────────────────────────────────────
@APP.before_request
def _init():
    _ensure_init()

@APP.get("/")
def home():
    st   = get_state()
    mode = st["mode"]
    ctrl = _is_controller(st)

    if mode == "idle":
        if ctrl:
            return render_template("pick.html", titles=list_titles())
        return render_template("pin.html")

    if mode == "picking":
        if ctrl:
            return render_template("pick.html", titles=list_titles())
        return render_template("wait.html", mode="picking", title="")

    if mode == "playing":
        if ctrl:
            return render_template("control.html",
                                   title=st["now_title"],
                                   poster_rel=st["now_poster"])
        return render_template("wait.html", mode="playing", title=st["now_title"])

    return render_template("pin.html")

@APP.post("/pin")
def post_pin():
    st   = get_state()
    mode = st["mode"]

    # Block if a video is already playing
    if mode == "playing":
        return render_template("wait.html", mode=mode, title=st["now_title"])

    pin = (request.form.get("pin") or "").strip()
    if pin != st["pin"]:
        return render_template("pin.html", error="Wrong PIN — check the screen and try again.")

    # Claim or reclaim the controller slot (re-entering PIN restores access)
    token = secrets.token_hex(24)
    kset_many({"controller_token": token, "mode": "picking", "picking_since": str(int(__import__("time").time()))})
    session["token"]     = token
    session.permanent    = False
    return redirect(url_for("home"))

@APP.post("/api/play")
@require_controller
def api_play():
    st = get_state()
    if st["mode"] not in ("picking", "idle"):
        return ("Busy", 409)
    key   = (request.json or {}).get("key")
    match = next((t for t in list_titles() if t["key"] == key), None) if key else None
    if not match:
        abort(404)

    kset_many({"mode": "playing", "now_title": match["title"], "now_poster": match["poster"]})
    REQUEST_FILE.write_text(json.dumps({
        "path":  str((MEDIA / match["video"]).resolve()),
        "title": match["title"],
    }))
    logging.info("%s picked '%s'", _client_ip(), match["title"])
    return ("OK", 200)

@APP.post("/api/stop")
@require_controller
def api_stop():
    kset("stop_requested", "1")
    return ("OK", 200)

@APP.get("/api/state")
def api_state():
    st  = get_state()
    url = web_url()
    return jsonify({
        "mode":      st["mode"],
        "pin":       st["pin"],
        "now_title": st["now_title"],
        "qr_url":    url,
    })

@APP.get("/tv")
def tv():
    return render_template("tv.html")

@APP.get("/qr.png")
def qr_png():
    data = _make_qr(web_url())
    return Response(data, mimetype="image/png",
                    headers={"Cache-Control": "no-store"})

@APP.get("/poster")
def poster():
    rel = request.args.get("rel", "")
    try:
        p = _safe_media(rel)
    except Exception:
        abort(404)
    return send_file(p, conditional=True)

# ── Admin ─────────────────────────────────────────────────────────────────────
@APP.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST" and not session.get("adm"):
        if request.form.get("password") == ADMIN_PASS:
            session["adm"] = True
            return redirect(url_for("admin"))
        return render_template("pin.html", admin_mode=True, error="Wrong password.")
    if not session.get("adm"):
        return render_template("pin.html", admin_mode=True)
    return render_template("admin.html", state=get_state(), titles=list_titles(), url=web_url())

@APP.post("/admin/action")
def admin_action():
    if not session.get("adm"):
        abort(403)
    action = request.form.get("action")

    if action == "reset":
        st = get_state()
        if st["mode"] == "playing":
            # Let player.py kill mpv and reset — do not call reset_to_idle() here
            # or it will clear stop_requested before player.py sees it
            kset("stop_requested", "1")
        else:
            reset_to_idle()
        session.pop("token", None)
        return redirect(url_for("admin"))

    if action == "play":
        key   = request.form.get("key")
        match = next((t for t in list_titles() if t["key"] == key), None) if key else None
        if match:
            token = secrets.token_hex(24)
            st    = get_state()
            pairs = {
                "mode":             "playing",
                "controller_token": token,
                "now_title":        match["title"],
                "now_poster":       match["poster"],
            }
            if st["mode"] == "playing":
                pairs["stop_requested"] = "1"
            kset_many(pairs)
            REQUEST_FILE.write_text(json.dumps({
                "path":  str((MEDIA / match["video"]).resolve()),
                "title": match["title"],
            }))
            logging.info("admin played '%s'", match["title"])
        return redirect(url_for("admin"))

    if action == "reboot":
        try:
            subprocess.Popen(["sudo", "/sbin/reboot", "now"])
        except Exception:
            pass
        return ("<html><body style='background:#0B0B10;color:#F0F2FF;"
                "display:grid;place-items:center;height:100vh'>"
                "<p>Rebooting…</p></body></html>")

    abort(400)

@APP.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

@APP.errorhandler(404)
def _404(_e):
    return redirect(url_for("home"))
