#!/usr/bin/env bash
# Provision a fresh Debian 12 / Ubuntu 24.04 Hetzner CX22 for the screener.
# Run as root on a brand-new server:
#   ssh root@<ip> 'bash -s' < deploy/bootstrap.sh
# Idempotent: safe to re-run.

set -euo pipefail

APP_USER=screener
APP_DIR=/opt/screener
TZ_NAME=America/Los_Angeles

log() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }

# --- 1. timezone -------------------------------------------------------------
# The whole DST problem is solved here. Set the box to Pacific and every
# systemd OnCalendar= below is local time, correct year-round, no UTC math.
log "Setting timezone to ${TZ_NAME}"
timedatectl set-timezone "$TZ_NAME"
timedatectl set-ntp true
timedatectl | sed -n '1,6p'

# --- 2. packages -------------------------------------------------------------
log "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  python3 python3-venv python3-pip \
  git curl ca-certificates \
  ufw fail2ban unattended-upgrades \
  sqlite3 tzdata

# --- 3. unprivileged app user ------------------------------------------------
log "Creating ${APP_USER}"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi
mkdir -p "$APP_DIR"/{data,journal,logs}
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# --- 4. firewall -------------------------------------------------------------
# Outbound only plus SSH. Nothing here needs to listen on a port.
log "Configuring firewall"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'ssh'
ufw --force enable
ufw status verbose

# --- 5. SSH hardening --------------------------------------------------------
# Only runs if you already have an authorized key installed -- otherwise it
# would lock you out, so it checks first.
log "Hardening SSH"
if [ -s /root/.ssh/authorized_keys ]; then
  sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
  sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
  sed -i 's/^#\?KbdInteractiveAuthentication.*/KbdInteractiveAuthentication no/' /etc/ssh/sshd_config
  systemctl reload ssh || systemctl reload sshd
  echo "password auth disabled"
else
  echo "!! /root/.ssh/authorized_keys is empty -- leaving password auth ON."
  echo "!! Install your key, then re-run this script."
fi

systemctl enable --now fail2ban
systemctl enable --now unattended-upgrades

# --- 6. python environment ---------------------------------------------------
log "Creating virtualenv"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip wheel
chown -R "$APP_USER:$APP_USER" "$APP_DIR/venv"

# --- 7. swap -----------------------------------------------------------------
# 4 GB of RAM is plenty, but a pandas job that briefly spikes shouldn't get
# OOM-killed at 1 AM with nobody watching.
if [ ! -f /swapfile ]; then
  log "Adding 2G swap"
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

log "Bootstrap complete"
cat <<EOF

Next:
  1. Copy the repo to ${APP_DIR}/app        (see deploy/deploy.sh)
  2. Put your .env at ${APP_DIR}/.env       chmod 600, owned by ${APP_USER}
  3. Install the timers:                    bash deploy/install-timers.sh
  4. Confirm the clock:                     timedatectl

Timezone is ${TZ_NAME}; systemd timers use local time and follow DST.
EOF
