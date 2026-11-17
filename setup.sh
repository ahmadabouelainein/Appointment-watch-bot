#!/data/data/com.termux/files/usr/bin/bash
# One-time setup on an Android phone in Termux. Safe to run again.
set -e
REPO=https://github.com/ahmadabouelainein/Appointment-watch-bot.git
BRANCH=ccr-4b08f47b-46ho73
DIR="$HOME/Appointment-watch-bot"

echo "== Installing Python and git (takes a few minutes)"
pkg update -y >/dev/null 2>&1 || true
pkg install -y python git >/dev/null
pip install -q --upgrade requests beautifulsoup4

if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull -q
else
  git clone -q -b "$BRANCH" "$REPO" "$DIR"
fi
cd "$DIR"

if [ ! -f config.env ]; then
  echo
  echo "== Your booking details (stored only on this phone, never uploaded to GitHub)"
  read -r -p "First name: " FN </dev/tty
  read -r -p "Last name: " LN </dev/tty
  read -r -p "Email: " EM </dev/tty
  read -r -p "Phone (optional, Enter to skip): " PH </dev/tty
  TOPIC="lra-rt-$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 20)"
  cat > config.env <<CFG
FIRST_NAME=$FN
LAST_NAME=$LN
EMAIL=$EM
PHONE=$PH
AUTO_BOOK=1
SERVICE_INDEX=3
POLL_SECONDS=75
NTFY_TOPIC=$TOPIC
CFG
fi
TOPIC=$(grep '^NTFY_TOPIC=' config.env | cut -d= -f2)

mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/start-appointment-bot" <<BOOT
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd "$DIR" && nohup ./run.sh > bot.log 2>&1 &
BOOT
chmod +x "$HOME/.termux/boot/start-appointment-bot" run.sh

echo
echo "================================================================"
echo " Notifications topic:  $TOPIC"
echo " In the ntfy app (on your tablet), tap + and subscribe to it."
echo "================================================================"
echo
echo "== Test run (does NOT book anything)..."
python bot.py discover | tail -n 40 || true
echo
echo "Setup done. Start the bot with:   cd ~/Appointment-watch-bot && ./run.sh"
