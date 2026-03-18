#!/usr/bin/env bash
# Waiting Room — setup script
# Run as root: sudo bash setup.sh
set -euo pipefail
cd "$(dirname "$0")"
BASE="$(pwd)"

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'; NC='\033[0m'
ok()   { echo -e "${GRN}✓${NC} $*"; }
warn() { echo -e "${YLW}!${NC} $*"; }
die()  { echo -e "${RED}✗${NC} $*"; exit 1; }

[ "$(id -u)" = "0" ] || die "Run as root: sudo bash setup.sh"
# ── Change this if you want a different username ──────────────────────────────
APP_USER="pitv"

echo ""
echo "  Waiting Room — Setup"
echo "  ════════════════════"
echo ""

# ── 1. System packages ────────────────────────────────────────────────────────
echo "Installing system packages…"
apt-get update -qq
apt-get install -y -qq \
  python3-venv python3-dev python3-pip \
  mpv chromium-browser \
  curl sqlite3 \
  libsdl2-2.0-0 2>/dev/null || true
ok "Packages installed"

# ── 2. User + groups ──────────────────────────────────────────────────────────
if ! id "$APP_USER" &>/dev/null; then
  useradd -m -s /bin/bash "$APP_USER"
  ok "Created user $APP_USER"
else
  ok "User $APP_USER already exists"
fi

groupadd -f autologin 2>/dev/null || true
for g in audio video input render netdev autologin; do
  usermod -aG "$g" "$APP_USER" 2>/dev/null && ok "Added $APP_USER to group $g" || warn "Could not add to $g"
done

# ── 3. Sudo rules ─────────────────────────────────────────────────────────────
SUDOERS_FILE="/etc/sudoers.d/waiting-room"
cat > "$SUDOERS_FILE" << 'EOF'
pitv ALL=(root) NOPASSWD: /sbin/reboot
EOF
chmod 0440 "$SUDOERS_FILE"
ok "Sudo rules written"

# ── 4. Python venv ────────────────────────────────────────────────────────────
VENV="$BASE/env"
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
  ok "Created venv"
fi
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$BASE/requirements.txt"
ok "Python dependencies installed"

# ── 5. Config ─────────────────────────────────────────────────────────────────
CONFIG="$BASE/config.env"
if grep -q "^APP_SECRET=$" "$CONFIG" 2>/dev/null; then
  SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  sed -i "s/^APP_SECRET=$/APP_SECRET=$SECRET/" "$CONFIG"
  ok "Generated APP_SECRET"
fi
warn "Review $CONFIG and set ADMIN_PASS before starting"

# ── 6. Media symlink (preserve old content) ───────────────────────────────────
MEDIA="$BASE/media"
OLD_MEDIA="/opt/waiting-room/media"
if [ ! -e "$MEDIA" ] && [ -d "$OLD_MEDIA" ]; then
  ln -s "$OLD_MEDIA" "$MEDIA"
  ok "Linked media from $OLD_MEDIA"
elif [ ! -e "$MEDIA" ]; then
  mkdir -p "$MEDIA"
  ok "Created empty media dir — add your videos here: $MEDIA"
fi

# ── 7. Permissions ────────────────────────────────────────────────────────────
chown -R "$APP_USER:$APP_USER" "$BASE"
mkdir -p "$BASE/data"
chown "$APP_USER:$APP_USER" "$BASE/data"
ok "Permissions set"

# ── 8. Systemd services ───────────────────────────────────────────────────────
cat > /etc/systemd/system/pitv-app.service << EOF
[Unit]
Description=Waiting Room Web App
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
EnvironmentFile=$BASE/config.env
WorkingDirectory=$BASE/app
ExecStartPre=/bin/rm -f $BASE/app/gunicorn.ctl
ExecStart=$BASE/env/bin/gunicorn -w 1 --threads 4 -b 0.0.0.0:9000 app:APP
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/pitv-player.service << EOF
[Unit]
Description=Waiting Room Player Daemon
After=pitv-app.service
Wants=pitv-app.service

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$BASE/player
ExecStart=$BASE/env/bin/python $BASE/player/player.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable pitv-app.service pitv-player.service
ok "Systemd services installed and enabled"

# ── 9. Desktop autostart for display ──────────────────────────────────────────
AUTOSTART_DIR="/home/$APP_USER/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/waiting-room-display.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Waiting Room Display
Exec=$BASE/start_display.sh
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=5
Terminal=false
EOF
chown -R "$APP_USER:$APP_USER" "/home/$APP_USER/.config"
ok "Autostart entry created"

# ── labwc autostart (Wayland compositor) ──────────────────────────────────────
LABWC_DIR="/home/$APP_USER/.config/labwc"
mkdir -p "$LABWC_DIR"
cat > "$LABWC_DIR/autostart" << EOF
$BASE/start_display.sh &
EOF

cat > "$LABWC_DIR/environment" << 'EOF'
XKB_DEFAULT_MODEL=pc105
XKB_DEFAULT_LAYOUT=us
XKB_DEFAULT_VARIANT=
XKB_DEFAULT_OPTIONS=
XCURSOR_THEME=blank-cursor
XCURSOR_SIZE=1
EOF

chown -R "$APP_USER:$APP_USER" "$LABWC_DIR"
ok "labwc autostart created"

# ── Blank cursor theme (hide cursor on Wayland kiosk) ─────────────────────────
CURSOR_DIR="/home/$APP_USER/.local/share/icons/blank-cursor/cursors"
mkdir -p "$CURSOR_DIR"
python3 << 'PYEOF'
import struct, pathlib, os, sys

app_user = os.environ.get('APP_USER', 'pitv')
base = pathlib.Path(f'/home/{app_user}/.local/share/icons/blank-cursor/cursors')
base.mkdir(parents=True, exist_ok=True)

# Minimal 1x1 transparent XCursor file
data  = b'Xcur'
data += struct.pack('<I', 16)            # header size
data += struct.pack('<I', 0x10000)       # version
data += struct.pack('<I', 1)             # ntoc
data += struct.pack('<III', 0xfffd0002, 32, 28)   # toc entry: type, subtype, offset
data += struct.pack('<IIIIIIIII', 40, 0xfffd0002, 32, 1, 1, 1, 0, 0, 100)  # image header
data += b'\x00\x00\x00\x00'             # 1 transparent ARGB pixel

(base / 'left_ptr').write_bytes(data)
names = ['default','top_left_arrow','pointer','hand1','hand2','watch','wait',
         'xterm','crosshair','move','fleur','not-allowed','no-drop','grab',
         'grabbing','col-resize','row-resize','n-resize','s-resize','e-resize',
         'w-resize','ne-resize','nw-resize','se-resize','sw-resize','ns-resize',
         'ew-resize','nesw-resize','nwse-resize','zoom-in','zoom-out','text',
         'help','progress','context-menu','cell','copy','alias','all-scroll']
for n in names:
    p = base / n
    if not p.exists():
        p.symlink_to('left_ptr')
print('Blank cursor theme created')
PYEOF
chown -R "$APP_USER:$APP_USER" "/home/$APP_USER/.local"

cat > "$LABWC_DIR/rc.xml" << 'XML'
<?xml version="1.0"?>
<openbox_config>
  <theme>
    <cursor>
      <theme>blank-cursor</theme>
      <size>1</size>
    </cursor>
  </theme>
</openbox_config>
XML
chown "$APP_USER:$APP_USER" "$LABWC_DIR/rc.xml"
ok "Blank cursor theme installed"

# ── 10. LightDM autologin ─────────────────────────────────────────────────────
LIGHTDM_CONF="/etc/lightdm/lightdm.conf"
if [ -f "$LIGHTDM_CONF" ]; then
  if ! grep -q "^autologin-user=$APP_USER" "$LIGHTDM_CONF"; then
    sed -i "s/^#*autologin-user=.*/autologin-user=$APP_USER/" "$LIGHTDM_CONF" || true
    grep -q "^autologin-user=" "$LIGHTDM_CONF" || echo "autologin-user=$APP_USER" >> "$LIGHTDM_CONF"
    warn "LightDM autologin set — verify $LIGHTDM_CONF looks right"
  fi
fi

# ── 11. Start services now ────────────────────────────────────────────────────
systemctl start pitv-app.service pitv-player.service
ok "Services started"
sleep 3

# ── 12. Health check ──────────────────────────────────────────────────────────
echo ""
if curl -sf http://127.0.0.1:9000/api/state > /dev/null 2>&1; then
  ok "Health check passed — app is running"
  echo ""
  echo "  Local URL: http://$(hostname -I | awk '{print $1}'):9000"
  echo "  Admin:     http://$(hostname -I | awk '{print $1}'):9000/admin"
else
  warn "Health check failed — check: journalctl -u pitv-app.service"
fi

echo ""
echo "  Setup complete. Reboot to start the kiosk display."
echo "  sudo reboot"
echo ""
