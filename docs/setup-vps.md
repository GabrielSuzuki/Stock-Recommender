# VPS setup — Hetzner CX22

Target: a $4.59/mo box (2 vCPU, 4 GB RAM, 40 GB NVMe) that runs Stage A at 01:00 Pacific and Stage B at 05:00 Pacific, every weekday, without you thinking about it. Budget 30–40 minutes.

Assumes you can SSH but haven't necessarily administered a Linux server before.

---

## 1. Create the server

1. Sign up at **console.hetzner.cloud**. Expect ID verification on a new account — this can take a few hours, so do it before you're in a hurry.
2. **New Project** → name it `screener`.
3. Before creating the server, add your SSH key: **Security → SSH Keys → Add**.

   If you don't have one:
   ```bash
   ssh-keygen -t ed25519 -C "screener"
   cat ~/.ssh/id_ed25519.pub      # paste this into Hetzner
   ```
   Paste the **`.pub`** file. Never the one without the extension.

4. **Add Server**:

   | Setting | Choice |
   |---|---|
   | Location | Ashburn (US East) or Hillsboro (US West) — Hillsboro if you want lowest latency to US market data |
   | Image | **Ubuntu 24.04** |
   | Type | **Shared vCPU → CX22** (2 vCPU / 4 GB / 40 GB) |
   | Networking | IPv4 + IPv6 |
   | SSH key | the one you just added |
   | Backups | optional, +20% — skip it, everything here is reproducible from git |
   | Name | `screener-01` |

5. Create. You get a public IP in about 15 seconds.

**Don't skip the SSH key.** If you create the server with a password instead, Hetzner emails it in plaintext and the hardening step in the next section will refuse to run.

---

## 2. Bootstrap

From your local machine, in the repo:

```bash
ssh root@<YOUR_IP> 'bash -s' < deploy/bootstrap.sh
```

This is idempotent — safe to re-run if something goes wrong. It does:

| Step | Why |
|---|---|
| `timedatectl set-timezone America/Los_Angeles` | **The DST fix.** Set the box to Pacific once, and every timer below is written in local time and follows DST on its own. No UTC arithmetic, no twice-yearly hour drift. |
| Installs python3, venv, git, sqlite3, ufw, fail2ban, unattended-upgrades | |
| Creates a system user `screener` with no login shell | The pipeline never runs as root |
| `ufw`: deny inbound except SSH, allow all outbound | Nothing here listens on a port |
| Disables SSH password auth | Only if it finds an authorized key first — otherwise it warns and leaves it alone rather than locking you out |
| Enables fail2ban + unattended security upgrades | |
| Creates `/opt/screener/venv` | |
| Adds 2 GB swap | 4 GB is plenty, but a pandas job that briefly spikes shouldn't get OOM-killed at 1 AM with nobody watching |

Verify the clock before anything else:

```bash
ssh root@<YOUR_IP> timedatectl
# Time zone: America/Los_Angeles (PDT, -0700)   <-- this line is the point
```

---

## 3. Ship the code

```bash
./deploy/deploy.sh root@<YOUR_IP>
```

Then create the environment file **on the server** (never rsync your `.env`):

```bash
ssh root@<YOUR_IP>
nano /opt/screener/.env          # paste from your local .env
chown screener:screener /opt/screener/.env
chmod 600 /opt/screener/.env
```

Sanity check Telegram from the server, since that's where it actually has to work:

```bash
sudo -u screener /opt/screener/venv/bin/python /opt/screener/app/notify/send_test.py
```

---

## 4. Install the timers

```bash
bash /opt/screener/app/deploy/install-timers.sh
```

You get:

| Unit | Fires | Does |
|---|---|---|
| `screener-nightly.timer` | Mon–Fri 01:00 PT | Stage A: refresh cache, indicators, screen, `candidates.json` |
| `screener-brief.timer` | Mon–Fri 05:00 PT | Stage B: regime → catalysts → thesis → Telegram |
| `screener-watchdog.timer` | Mon–Fri 05:20 PT | Alerts if no brief was delivered |

**Check that systemd agrees about the time:**

```bash
systemctl list-timers 'screener-*' --all
```

The `NEXT` column must show Pacific times. If it shows UTC, the timezone step didn't take — fix that before trusting anything.

### Why systemd timers and not cron

- `Persistent=true` — if the box was down at 01:00, Stage A runs as soon as it's back instead of silently skipping the day. Cron just loses the run.
- `OnCalendar=Mon..Fri 01:00` is local time and DST-aware once the system timezone is set.
- Failures are visible: `systemctl status`, `journalctl -u`, and a non-zero exit that actually surfaces, rather than a mail spool nobody reads.
- `Restart=on-failure` with `StartLimitBurst` gives Stage A one retry 10 minutes later — enough to survive a data provider hiccup — then stops.

Note: `StartLimitBurst` and `StartLimitIntervalSec` must be in the `[Unit]` section. systemd silently ignores them in `[Service]` and your retry cap does nothing. The units here are verified with `systemd-analyze verify`.

---

## 5. Test before trusting

Run each stage by hand first:

```bash
systemctl start screener-nightly.service
journalctl -u screener-nightly.service -n 50 --no-pager

systemctl start screener-brief.service
journalctl -u screener-brief.service -n 50 --no-pager
```

Then simulate a bad morning — this is the test people skip and regret:

```bash
# Kill Stage A's output and confirm Stage B fails loudly rather than
# silently briefing you on last week's prices.
mv /opt/screener/data/candidates.json /tmp/
systemctl start screener-brief.service
# You should get a Telegram alert, not silence and not a stale brief.
```

**Run the whole thing for a week before you act on it.** Watch that 05:00 actually means 05:00 and that the Sunday→Monday transition works.

---

## 6. Operating it

```bash
# What's scheduled
systemctl list-timers 'screener-*' --all

# Last night's Stage A
journalctl -u screener-nightly.service --since yesterday

# Follow live
journalctl -u screener-brief.service -f

# Pause for vacation
systemctl disable --now screener-brief.timer

# Update code
./deploy/deploy.sh root@<YOUR_IP>     # from your laptop
```

Logs are also written to `/opt/screener/logs/`. Add logrotate when they get annoying — they won't for months.

---

## 7. Hardening notes

The service units run with `ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp=true`, `NoNewPrivileges=true`, and an explicit `ReadWritePaths` allowlist of exactly three directories. If you add a component that needs to write somewhere else, add that path to `ReadWritePaths` rather than loosening `ProtectSystem` — the whole value is in the allowlist being short enough to read.

The `.env` holds your Anthropic key, your Alpaca keys, and your Telegram token. It's `chmod 600` and owned by `screener`. If the box is ever compromised, rotate all four.

---

## 8. Cost

| | Monthly |
|---|---|
| Hetzner CX22 | $4.59 |
| IPv4 address | ~$0.60 |
| Backups (skipped) | — |
| **Total** | **~$5.20** |

Verify current pricing at checkout — Hetzner adjusted prices in April 2026 and these figures are from August 2026.
