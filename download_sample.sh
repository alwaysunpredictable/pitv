#!/usr/bin/env bash
# Downloads Big Buck Bunny as a sample video for the waiting room.
# Run once after setup: bash download_sample.sh
set -euo pipefail
cd "$(dirname "$0")"

DEST="media/Big Buck Bunny"
mkdir -p "$DEST"

if [ -f "$DEST/Big Buck Bunny.mp4" ] && [ -f "$DEST/Big Buck Bunny.png" ]; then
  echo "Already downloaded."
  exit 0
fi

echo "Downloading Big Buck Bunny (~62MB)..."
curl -L --progress-bar \
  "http://download.blender.org/peach/bigbuckbunny_movies/BigBuckBunny_320x180.mp4" \
  -o "$DEST/Big Buck Bunny.mp4"

echo "Generating poster..."
/home/pitv/waiting-room/env/bin/python3 - << 'EOF'
from PIL import Image, ImageDraw, ImageFont
img = Image.new("RGB", (400, 600), color=(30, 20, 60))
draw = ImageDraw.Draw(img)
draw.rectangle([0, 220, 400, 380], fill=(60, 40, 120))
draw.text((200, 260), "Big Buck", fill="white", anchor="mm",
          font=ImageFont.load_default(size=36))
draw.text((200, 320), "Bunny", fill="#FFC220", anchor="mm",
          font=ImageFont.load_default(size=48))
draw.text((200, 560), "© Blender Foundation", fill=(120,120,160), anchor="mm",
          font=ImageFont.load_default(size=14))
img.save("media/Big Buck Bunny/Big Buck Bunny.png")
print("Poster saved.")
EOF

echo "Done — 'Big Buck Bunny' will appear in the picker."
