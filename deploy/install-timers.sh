#!/usr/bin/env bash
# Install and enable the screener timers. Run as root on the VPS.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/systemd"

install -m 644 "$SRC"/screener-*.service /etc/systemd/system/
install -m 644 "$SRC"/screener-*.timer   /etc/systemd/system/

systemd-analyze verify /etc/systemd/system/screener-*.service || true

systemctl daemon-reload
systemctl enable --now screener-nightly.timer screener-brief.timer \
  screener-watchdog.timer screener-fills.timer \
  screener-review.timer screener-positions.timer

echo
systemctl list-timers 'screener-*' --all
echo
echo "Verify the NEXT column shows Pacific times. If not: timedatectl set-timezone America/Los_Angeles"
