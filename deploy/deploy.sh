#!/usr/bin/env bash
# Push local code to the VPS and restart nothing (timers pick it up next fire).
#   ./deploy/deploy.sh screener@1.2.3.4
# Requires rsync locally and on the remote.
set -euo pipefail

TARGET="${1:-}"
[ -z "$TARGET" ] && { echo "usage: $0 user@host"; exit 1; }
REMOTE_DIR=/opt/screener/app

rsync -az --delete \
  --exclude '.git' --exclude '.env' --exclude 'data/' --exclude 'journal/' \
  --exclude '__pycache__' --exclude '.tokenwise/' --exclude 'logs/' \
  ./ "$TARGET:$REMOTE_DIR/"

ssh "$TARGET" "/opt/screener/venv/bin/pip install -q -r $REMOTE_DIR/requirements.txt"

echo "deployed. next runs:"
ssh "$TARGET" "systemctl list-timers 'screener-*' --all --no-pager"
