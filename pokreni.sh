#!/bin/bash
# Pokreće Denon Studio (ako već ne radi) i otvara ga u browseru.
cd "$(dirname "$0")"
if ! curl -s -m1 -o /dev/null http://localhost:8766/api/status; then
  nohup python3 denon-studio.py >/tmp/denon-studio.log 2>&1 &
  sleep 1.5
fi
xdg-open http://localhost:8766 >/dev/null 2>&1 &
