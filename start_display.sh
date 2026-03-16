#!/usr/bin/env bash
# Launched by labwc autostart — runs in Wayland session

# Give the session and services time to settle
sleep 10

# Disable screen blanking / power saving (best-effort)
command -v xset >/dev/null 2>&1 && xset s off -dpms 2>/dev/null || true

# Remove Chromium lock/restore files
PROFILE="$HOME/.config/chromium-kiosk"
mkdir -p "$PROFILE"
rm -f "$PROFILE"/Singleton* 2>/dev/null || true

# Delete the keyring so Chromium never prompts for it
rm -f "$HOME/.local/share/keyrings/login.keyring" 2>/dev/null || true
rm -f "$HOME/.local/share/keyrings/default.keyring" 2>/dev/null || true

# Launch kiosk
exec chromium-browser \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --no-first-run \
  --disable-session-crashed-bubble \
  --disable-restore-session-state \
  --disable-pinch \
  --overscroll-history-navigation=0 \
  --ozone-platform=wayland \
  --password-store=basic \
  --disable-features=TranslateUI \
  --user-data-dir="$PROFILE" \
  "http://127.0.0.1:9000/tv"
