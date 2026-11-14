#!/bin/bash
# Denon Studio kao aplikacija: pokrene server, otvori prozor, a kad se prozor zatvori
# zaustavi muziku na Denonu i ugasi server.
DIR="$(cd "$(dirname "$0")" && pwd)"
URL="http://localhost:8766"
PROFILE="$HOME/.config/denon-studio-app"
LOG="/tmp/denon-studio.log"
BROWSER="$(command -v chromium || command -v chromium-browser || command -v google-chrome)"

# prozor je već otvoren -> samo ga prikaži, ne pokreći ništa ponovo
if pgrep -u "$USER" -f -- "--user-data-dir=$PROFILE" >/dev/null; then
  exec "$BROWSER" --user-data-dir="$PROFILE" --app="$URL"
fi

if ! curl -s -m1 -o /dev/null "$URL/api/status"; then
  python3 "$DIR/denon-studio.py" >"$LOG" 2>&1 &
  for i in $(seq 1 40); do curl -s -m1 -o /dev/null "$URL/api/status" && break; sleep 0.25; done
fi

# čeka ovde dok je prozor otvoren
"$BROWSER" --user-data-dir="$PROFILE" --app="$URL" --class=DenonStudio --start-maximized \
  --no-first-run --no-default-browser-check --autoplay-policy=no-user-gesture-required >/dev/null 2>&1

# prozor zatvoren: stop muzike + gašenje servera
curl -s -m6 "$URL/api/quit" >/dev/null
sleep 1
pkill -u "$USER" -f "python3 $DIR/denon-studio.py" 2>/dev/null
exit 0
