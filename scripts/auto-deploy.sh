#!/bin/bash
# Auto-deploy script for production VM
# Runs via systemd timer every 1 minute
# Pulls latest code from git, restarts changed services, and health-checks

set -uo pipefail

REPO_DIR="/home/ubuntu/trading"
COMPOSE_FILE="docker-compose.prod.yml"
LOG_FILE="/var/log/auto-deploy.log"
BRANCH="main"
EXPECTED_CONTAINERS=("xau-analyzer" "xau-data-ingest" "xau-notification" "xau-trading-db")

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

send_telegram() {
  if [ -f "$REPO_DIR/.env" ]; then
    set -a; source "$REPO_DIR/.env"; set +a
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
      curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_CHAT_ID}" \
        -d parse_mode=HTML \
        -d text="$1" > /dev/null 2>&1 \
        && log "📨 Telegram notification sent" \
        || log "⚠️ Telegram notification failed"
    fi
  fi
}

cd "$REPO_DIR" || { log "ERROR: Repo dir not found"; exit 1; }

# ─── Step 1: Check for new code ───
git fetch origin "$BRANCH" --quiet 2>/dev/null

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" != "$REMOTE" ]; then
  log "🔄 New commits detected: ${LOCAL:0:7} → ${REMOTE:0:7}"
  log "📥 Pulling changes..."
  git reset --hard "origin/$BRANCH"

  log "🔨 Rebuilding containers..."
  docker compose -f "$COMPOSE_FILE" build --quiet 2>&1 | tail -5 | tee -a "$LOG_FILE"

  log "🚀 Restarting services..."
  docker compose -f "$COMPOSE_FILE" up -d --remove-orphans 2>&1 | tee -a "$LOG_FILE"

  # Cleanup old images
  docker image prune -f --filter "until=1h" >/dev/null 2>&1 || true

  # Wait for containers to start
  sleep 10

  COMMIT=$(git log -1 --format="%h %s")
  TIME=$(date "+%Y-%m-%d %H:%M:%S")
  send_telegram "🚀 <b>Auto-Deploy Complete</b>%0A%0A📍 OCI VM (auto)%0A🔖 ${COMMIT}%0A🕐 ${TIME}"
  log "✅ Deploy complete: $(git rev-parse --short HEAD)"
fi

# ─── Step 2: Health check — ensure all containers are running ───
RESTART_NEEDED=false
for name in "${EXPECTED_CONTAINERS[@]}"; do
  if ! docker ps --format '{{.Names}}' | grep -q "^${name}$"; then
    log "⚠️ Container ${name} is DOWN — restarting..."
    RESTART_NEEDED=true
  fi
done

if [ "$RESTART_NEEDED" = true ]; then
  docker compose -f "$COMPOSE_FILE" up -d --remove-orphans 2>&1 | tee -a "$LOG_FILE"
  sleep 5

  # Verify after restart
  STILL_DOWN=()
  for name in "${EXPECTED_CONTAINERS[@]}"; do
    if ! docker ps --format '{{.Names}}' | grep -q "^${name}$"; then
      STILL_DOWN+=("$name")
    fi
  done

  if [ ${#STILL_DOWN[@]} -gt 0 ]; then
    log "❌ CRITICAL: Containers still down after restart: ${STILL_DOWN[*]}"
    send_telegram "❌ <b>ALERT: Containers DOWN</b>%0A%0A${STILL_DOWN[*]}%0AManual intervention needed!"
  else
    log "✅ All containers recovered"
    send_telegram "🔧 <b>Auto-Recovery</b>%0A%0AContainers were down and have been restarted successfully."
  fi
fi

