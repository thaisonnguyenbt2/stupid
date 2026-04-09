#!/bin/bash
# Local GitOps Hook Tracker

REPO_DIR="/home/ubuntu/exness" # Default server directory from Oracle Cloud
if [ ! -d "$REPO_DIR" ]; then
  REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" # Fallback to local machine
fi

BRANCH="main"
POLL_INTERVAL=30

echo "🚀 Starting ArgoCD GitOps Build Hook Watcher"
echo "📂 Monitoring Directory: $REPO_DIR"
echo "🌿 Branch: $BRANCH"

# Navigate to the repository
cd "$REPO_DIR" || exit 1

build_and_deploy() {
  echo "🛠️ Starting local image builds..."
  
  docker build -t trading-frontend:latest ./frontend
  docker build -t trading-analyzer:latest ./services/analyzer
  docker build -t trading-data-ingest:latest ./services/data-ingest
  docker build -t trading-notification:latest ./services/notification
  
  echo "♻️ Images built natively into the local Daemon."
  echo "⛵ Triggering Kubernetes Pod Rotation to pull fresh local images..."
  
  kubectl rollout restart deployment frontend analyzer data-ingest notification -n trading-system
  
  echo "✅ Deployment signal sent to Kubernetes!"
}

# Initial build just in case
build_and_deploy

# Keep track of local vs remote git SHAs
while true; do
  git fetch origin "$BRANCH" >/dev/null 2>&1
  
  LOCAL_SHA=$(git rev-parse HEAD)
  REMOTE_SHA=$(git rev-parse origin/"$BRANCH")
  
  if [ "$LOCAL_SHA" != "$REMOTE_SHA" ]; then
    echo "🔔 Detected changes on origin/$BRANCH! (Local: $LOCAL_SHA, Remote: $REMOTE_SHA)"
    echo "⬇️ Pulling new code..."
    git pull origin "$BRANCH"
    
    # Wait for ArgoCD to detect the YAML changes (Optional buffer)
    sleep 5
    
    # Build the modified images and bounce the pods
    build_and_deploy
  fi
  
  sleep "$POLL_INTERVAL"
done
