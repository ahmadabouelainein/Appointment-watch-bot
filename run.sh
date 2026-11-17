#!/data/data/com.termux/files/usr/bin/bash
# Keeps the bot running forever and picks up code fixes from GitHub.
cd "$(dirname "$0")"
command -v termux-wake-lock >/dev/null && termux-wake-lock
while true; do
  git pull -q 2>/dev/null
  python bot.py watch
  [ -f state.json ] && grep -q '"booked"' state.json && { echo "Booked - stopping."; break; }
  sleep 30
done
