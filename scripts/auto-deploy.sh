#!/bin/bash
# Auto-deploy script for production VM
# Replaces ArgoCD — runs via systemd timer every 5 minutes
# Pulls latest code from git and restarts changed services

set -euo pipefail

REPO_DIR="/home/ubuntu/trading"
COMPOSE_FILE="docker-compose.prod.yml"
LOG_FILE="/var/log/auto-deploy.log"
BRANCH="main"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

cd "$REPO_DIR" || { log "ERROR: Repo dir not found"; exit 1; }

# Fetch latest changes
git fetch origin "$BRANCH" --quiet 2>/dev/null

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
  log "✅ Up to date ($LOCAL)"
  exit 0
fi

log "🔄 New commits detected: $LOCAL → $REMOTE"
log "📥 Pulling changes..."
git reset --hard "origin/$BRANCH"

log "🔨 Rebuilding containers..."
docker compose -f "$COMPOSE_FILE" build --no-cache --quiet 2>&1 | tail -5 | tee -a "$LOG_FILE"

log "🚀 Restarting services..."
docker compose -f "$COMPOSE_FILE" up -d --remove-orphans 2>&1 | tee -a "$LOG_FILE"

# Cleanup old images to save disk space (important on 50GB boot volume)
docker image prune -f --filter "until=1h" >/dev/null 2>&1 || true

log "✅ Deploy complete: $(git rev-parse --short HEAD)"

# Send Telegram notification
if [ -f "$REPO_DIR/.env" ]; then
  set -a; source "$REPO_DIR/.env"; set +a
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    COMMIT=$(git log -1 --format="%h %s")
    TIME=$(date "+%Y-%m-%d %H:%M:%S")
    TEXT="🚀 <b>Auto-Deploy Complete</b>%0A%0A📍 OCI VM (auto)%0A🔖 ${COMMIT}%0A🕐 ${TIME}"
    curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      -d chat_id="${TELEGRAM_CHAT_ID}" \
      -d parse_mode=HTML \
      -d text="${TEXT}" > /dev/null 2>&1 \
      && log "📨 Telegram notification sent" \
      || log "⚠️ Telegram notification failed"
  fi
fi
