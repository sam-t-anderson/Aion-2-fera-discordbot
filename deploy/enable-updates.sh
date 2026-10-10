#!/usr/bin/env bash
# Turn on automatic updates: installs the updater's systemd units and starts its timer.
# Does not touch the running bot, its .env or its data.
#   sudo bash deploy/enable-updates.sh
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo bash deploy/enable-updates.sh" >&2
  exit 1
fi
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if ! git -c safe.directory="$SRC" -C "$SRC" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "$SRC is not a Git checkout; the updater needs one (see README, Automatic updates)." >&2
  exit 1
fi
if [ ! -f /etc/aion2bot-update.env ]; then
  printf 'SRC=%s\n' "$SRC" > /etc/aion2bot-update.env
  chmod 644 /etc/aion2bot-update.env
fi
install -m 644 "$SRC/deploy/aion2bot-update.service" /etc/systemd/system/aion2bot-update.service
install -m 644 "$SRC/deploy/aion2bot-update.timer" /etc/systemd/system/aion2bot-update.timer
systemctl daemon-reload
systemctl enable --now aion2bot-update.timer
systemctl list-timers aion2bot-update.timer --no-pager
echo "Automatic updates enabled: CI-verified main, every 15 minutes plus startup."
