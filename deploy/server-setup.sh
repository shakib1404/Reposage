#!/usr/bin/env bash
#
# RepoSage — one-shot server setup for a fresh Ubuntu droplet/VM.
#
#   scp -r deploy/ root@DROPLET_IP:~/           # this script
#   scp .env "repo-task executor/.env" ...      # your secrets (see README in this dir)
#   ssh root@DROPLET_IP 'bash ~/deploy/server-setup.sh'
#
# Installs Docker, sizes swap, opens the firewall, fetches the compose files and
# starts the stack from the published Docker Hub images. Safe to re-run: every
# step checks before acting, and it never overwrites an existing .env.
#
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/reposage}"
RAW="https://raw.githubusercontent.com/shakib1404/Reposage/master"
DOCKERHUB_USER_DEFAULT="shakib1404"
TAG_DEFAULT="v1"

say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (or with sudo)"

# ── Docker ───────────────────────────────────────────────────────────────────
if command -v docker >/dev/null 2>&1; then
  say "Docker already installed — skipping"
else
  say "Installing Docker"
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || die "the compose plugin is missing"

# ── Swap ─────────────────────────────────────────────────────────────────────
# Sized against RAM, not a fixed number: on a 1-2GB droplet a heavy pip install
# is the thing most likely to get OOM-killed, and swap is the cheap insurance.
RAM_MB=$(free -m | awk '/^Mem:/{print $2}')
say "Detected ${RAM_MB}MB RAM"

if swapon --show | grep -q .; then
  say "Swap already active — skipping"
else
  if   [ "$RAM_MB" -lt 1536 ]; then SWAP=4G
  elif [ "$RAM_MB" -lt 3072 ]; then SWAP=4G
  else                              SWAP=2G
  fi
  say "Creating ${SWAP} swap"
  fallocate -l "$SWAP" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ── Firewall ─────────────────────────────────────────────────────────────────
if command -v ufw >/dev/null 2>&1; then
  say "Configuring ufw (SSH + 80 + 443)"
  ufw allow OpenSSH >/dev/null
  ufw allow 80      >/dev/null
  ufw allow 443     >/dev/null
  ufw --force enable >/dev/null
else
  warn "ufw not installed — skipping firewall. Your cloud firewall still applies."
fi

# ── App files ────────────────────────────────────────────────────────────────
say "Setting up $APP_DIR"
mkdir -p "$APP_DIR"
cd "$APP_DIR"

for f in docker-compose.hub.yml docker-compose.lowmem.yml; do
  if [ -f "$f" ]; then
    say "$f already here — keeping it"
  else
    say "Fetching $f"
    curl -fsSL -O "$RAW/$f" \
      || die "could not fetch $f — has it been committed and pushed yet?"
  fi
done

# ── Secrets ──────────────────────────────────────────────────────────────────
# Never generated here and never fetched: .env holds live credentials and is
# deliberately absent from both git and the image. Copy it up yourself.
[ -f .env ] || die "$APP_DIR/.env is missing.
   Copy it from your machine first:
     scp .env root@THIS_SERVER:$APP_DIR/.env
     scp 'repo-task executor/.env' root@THIS_SERVER:'$APP_DIR/repo-task executor/.env'"

# Append the Hub settings if absent.
#
# The newline check is the point: a .env whose last line has no trailing
# newline would otherwise get the new key glued onto it, silently producing
#     GMAIL_PASS=xxxDOCKERHUB_USER=shakib1404
# Only add the separator when one is actually missing, so re-runs don't
# accumulate blank lines.
add_kv() {
  local key="$1" val="$2"
  if grep -q "^${key}=" .env; then
    say "$key already set in .env"
    return
  fi
  say "Adding $key to .env"
  # Written as an if, not `a && b && printf`: under `set -e` a false condition
  # makes the whole && list return non-zero and kills the script.
  if [ -s .env ] && [ -n "$(tail -c1 .env)" ]; then
    printf '\n' >> .env
  fi
  printf '%s=%s\n' "$key" "$val" >> .env
}
add_kv DOCKERHUB_USER "${DOCKERHUB_USER:-$DOCKERHUB_USER_DEFAULT}"
add_kv TAG            "${TAG:-$TAG_DEFAULT}"

for required in MONGO_URL JWT_SECRET GROQ_API_KEY_1; do
  val=$(grep "^${required}=" .env | cut -d= -f2- || true)
  [ -n "${val:-}" ] || warn "$required is empty in .env — the app will not start correctly"
done

grep -q '^SITE_ADDRESS=' .env || add_kv SITE_ADDRESS ':80'

# ── Launch ───────────────────────────────────────────────────────────────────
# Below ~3GB the base compose limits are actively harmful: a 3g container cap
# exceeds total RAM so it never engages, and the 6g build tmpfs is RAM-backed.
FILES=(-f docker-compose.hub.yml)
if [ "$RAM_MB" -lt 3072 ]; then
  say "Under 3GB RAM — layering the low-memory override"
  FILES+=(-f docker-compose.lowmem.yml)
fi

say "Pulling images and starting (first pull is ~790MB)"
docker compose "${FILES[@]}" up -d

say "Waiting for the backend to finish warming up"
for _ in $(seq 1 60); do
  if docker compose "${FILES[@]}" logs backend 2>&1 | grep -q "Application startup complete"; then
    # `|| echo fallback` is not enough: curl can exit 0 and still print
    # nothing, which produced a bare "Open http://".
    IP=$(curl -fsS --max-time 5 https://ifconfig.me 2>/dev/null || true)
    [ -n "$IP" ] || IP="YOUR_SERVER_IP"
    say "Up. Open http://${IP}"
    docker compose "${FILES[@]}" logs backend 2>&1 | grep -E "warmed up|startup complete" | tail -2
    exit 0
  fi
  sleep 5
done

warn "Backend did not report startup within 5 minutes. Recent logs:"
docker compose "${FILES[@]}" logs --tail 40 backend
warn "Most common cause: MongoDB Atlas is blocking this server."
warn "Fix: Atlas -> Network Access -> add this droplet's IP."
exit 1
