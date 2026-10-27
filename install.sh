#!/usr/bin/env bash
# Installs Pi MIDI Router on a Raspberry Pi (Raspberry Pi OS Bookworm or later).
#   sudo bash install.sh              # dashboard on port 8080
#   sudo bash install.sh --port 9000  # pick another port
# Safe to run again to upgrade: your routes, device names and port are kept.
set -euo pipefail

PORT=8080
PORT_GIVEN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="${2:-}"; PORT_GIVEN=1; shift 2 ;;
    --port=*) PORT="${1#*=}"; PORT_GIVEN=1; shift ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "Port must be a number between 1 and 65535" >&2; exit 1
fi
if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo:  sudo bash install.sh" >&2; exit 1
fi

NAME=pi-midi-router
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP=/opt/$NAME
DATA=/var/lib/$NAME
SVC_USER=$NAME
UNIT=/etc/systemd/system/$NAME.service
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

for f in router.py server.py demo_backend.py $NAME.service static/index.html static/app.js static/style.css; do
  [[ -f "$SRC/$f" ]] || { echo "Missing $f - run this from inside the unzipped (or cloned) project folder." >&2; exit 1; }
done

if [[ -d "$APP" || -f "$UNIT" ]]; then
  OLD_PORT="$(sed -n 's/^Environment=PI_MIDI_ROUTER_PORT=\([0-9]\{1,\}\)$/\1/p' "$UNIT" 2>/dev/null | head -n1 || true)"
  if [[ $PORT_GIVEN -eq 0 && -n "$OLD_PORT" ]]; then
    PORT="$OLD_PORT"
  fi
  step "Existing installation found"
  echo "The program in $APP and its boot service will be overwritten with this version."
  echo "Your saved routes, device nicknames and hidden message types are kept."
  if systemctl is-active --quiet "$NAME" 2>/dev/null; then
    echo "The router is running now; MIDI routing pauses for a few seconds while it restarts."
  fi
  if [[ -n "$OLD_PORT" && "$OLD_PORT" != "$PORT" ]]; then
    echo "The dashboard moves from port $OLD_PORT to port $PORT."
  else
    echo "The dashboard stays on port $PORT."
  fi
  if [[ -t 0 ]]; then
    echo "Continuing in 5 seconds - press Ctrl+C to cancel."
    sleep 5
  fi
fi

step "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 alsa-utils avahi-daemon curl >/dev/null
apt-get install -y -qq python3-mido python3-rtmidi >/dev/null 2>&1 || true

# Make sure the ALSA MIDI sequencer is loaded now and at every boot
echo snd-seq > /etc/modules-load.d/$NAME.conf
modprobe snd-seq 2>/dev/null || true

PYTHON=/usr/bin/python3
if ! "$PYTHON" -c 'import mido, rtmidi' 2>/dev/null; then
  step "System MIDI packages unavailable - building them in a private environment (takes a few minutes)"
  apt-get install -y -qq python3-venv python3-dev build-essential pkg-config libasound2-dev >/dev/null
  mkdir -p "$APP"
  python3 -m venv "$APP/venv"
  "$APP/venv/bin/pip" install --quiet --upgrade pip
  "$APP/venv/bin/pip" install --quiet mido python-rtmidi
  PYTHON="$APP/venv/bin/python"
  "$PYTHON" -c 'import mido, rtmidi'
fi
echo "Using $PYTHON"

step "Creating service user '$SVC_USER'"
if ! id "$SVC_USER" &>/dev/null; then
  useradd --system --no-create-home --home-dir "$DATA" --shell /usr/sbin/nologin "$SVC_USER"
fi
usermod -aG audio,video "$SVC_USER"

step "Copying files to $APP"
systemctl stop "$NAME" 2>/dev/null || true
install -d -m 755 "$APP" "$APP/static"
install -m 644 "$SRC/router.py" "$SRC/server.py" "$SRC/demo_backend.py" "$APP/"
install -m 644 "$SRC"/static/* "$APP/static/"
install -d -m 750 -o "$SVC_USER" -g "$SVC_USER" "$DATA"
chown -R "$SVC_USER:$SVC_USER" "$DATA"

step "Installing the boot service"
sed -e "s|@PYTHON@|$PYTHON|" -e "s|@PORT@|$PORT|" "$SRC/$NAME.service" > "$UNIT"
systemctl daemon-reload
systemctl enable "$NAME" >/dev/null 2>&1
systemctl restart "$NAME"

# Open the port to the home network only, if a firewall is switched on.
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  step "Allowing port $PORT through the firewall (home network only)"
  for net in 192.168.0.0/16 10.0.0.0/8 172.16.0.0/12; do
    ufw allow from "$net" to any port "$PORT" proto tcp >/dev/null
  done
fi
systemctl enable --now avahi-daemon >/dev/null 2>&1 || true

step "Checking it started"
ok=0
for _ in $(seq 1 20); do
  if command -v curl >/dev/null; then
    curl -fs "http://127.0.0.1:$PORT/api/state" >/dev/null && { ok=1; break; }
  else
    systemctl is-active --quiet "$NAME" && { ok=1; break; }
  fi
  sleep 0.5
done
if [[ $ok -ne 1 ]]; then
  echo "The service did not start. Recent log:" >&2
  journalctl -u "$NAME" -n 30 --no-pager >&2 || true
  exit 1
fi

HOST="$(hostname)"
IPS="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || true)"
echo
echo "Pi MIDI Router is running and will start automatically at boot."
echo
echo "Open the dashboard from any phone or computer on the same network:"
echo "    http://$HOST.local:$PORT"
for ip in $IPS; do echo "    http://$ip:$PORT"; done
echo
echo "Live log:   journalctl -u $NAME -f"
