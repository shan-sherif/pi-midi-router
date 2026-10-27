#!/usr/bin/env bash
# Removes Pi MIDI Router.  Your saved routes are kept unless you add --purge.
#   sudo bash uninstall.sh           # keep routes in /var/lib/pi-midi-router
#   sudo bash uninstall.sh --purge   # remove everything
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo "Please run with sudo." >&2; exit 1; fi

systemctl disable --now pi-midi-router 2>/dev/null || true
rm -f /etc/systemd/system/pi-midi-router.service /etc/modules-load.d/pi-midi-router.conf
systemctl daemon-reload
rm -rf /opt/pi-midi-router

if [[ "${1:-}" == "--purge" ]]; then
  rm -rf /var/lib/pi-midi-router
  userdel pi-midi-router 2>/dev/null || true
  echo "Pi MIDI Router and its saved routes have been removed."
else
  echo "Pi MIDI Router removed. Saved routes are still in /var/lib/pi-midi-router (use --purge to delete)."
fi
