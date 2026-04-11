ifneq (,$(wildcard ./.env))
    include .env
endif

.PHONY: dev dev-frontend dev-ingest dev-analyzer dev-notification install clean-ghosts chart


clean-ghosts:
	@echo "Killing ghost processes..."
	@pkill -f "nodemon.*src/index.ts" || true
	@pkill -f "nodemon.*main.py" || true
	@pkill -f "python main.py" || true
	@pkill -f "next dev" || true
	@lsof -ti:3000 | xargs kill -9 2>/dev/null || true
	@lsof -ti:4000 | xargs kill -9 2>/dev/null || true
	@lsof -ti:4002 | xargs kill -9 2>/dev/null || true
	@lsof -ti:4003 | xargs kill -9 2>/dev/null || true

dev: clean-ghosts
	@echo "Starting MongoDB container..."
	@docker compose up -d trading-db
	@echo "Starting XAU/USD Paper Trading Platform..."
	@make -j4 dev-frontend dev-ingest dev-analyzer dev-notification

dev-frontend:
	@echo "[Frontend] Starting Next.js dev server..."
	bash -c 'cp .env frontend/.env.local 2>/dev/null || true; source ~/.nvm/nvm.sh && nvm use 20 && cd frontend && npm run dev'

dev-ingest:
	@echo "[Data Ingest] Starting TwelveData + Finnhub worker..."
	bash -c 'source ~/.nvm/nvm.sh && nvm use 20 && cd services/data-ingest && npm run dev'

dev-analyzer:
	@echo "[Analyzer] Starting Python strategy engine..."
	bash -c 'cd services/analyzer && source venv/bin/activate && npx nodemon --watch main.py --exec python main.py'

dev-notification:
	@echo "[Notification] Starting Telegram notification service..."
	bash -c 'source ~/.nvm/nvm.sh && nvm use 20 && cd services/notification && npm run dev'

install:
	@echo "Installing all dependencies..."
	bash -c 'source ~/.nvm/nvm.sh && nvm use 20 && cd frontend && npm install'
	bash -c 'source ~/.nvm/nvm.sh && nvm use 20 && cd services/data-ingest && npm install'
	bash -c 'source ~/.nvm/nvm.sh && nvm use 20 && cd services/notification && npm install'
	cd services/analyzer && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt

db-up:
	docker compose up -d --build

db-down:
	docker compose down

# ===================== DRY RUN / BACKTEST =====================
#
# All dry-run commands use the SAME strategy.py as the live analyzer.
# This guarantees parity between backtest and production.
#
# Run against a specific dataset (202601, 202602, 202603):
#   make dry-run DS=202603
#
# Run against a specific CSV file:
#   make dry-run DS=data/DAT_ASCII_XAUUSD_T_202603.csv
#
# Run all available datasets:
#   make dry-run-all
#
# Compare with/without trend filter:
#   make dry-run-compare
#
# Run against MongoDB paper trading data:
#   make dry-run-mongo

dry-run:
ifndef DS
	@echo "Usage: make dry-run DS=202603"
	@echo "       make dry-run DS=data/DAT_ASCII_XAUUSD_T_202603.csv"
	@exit 1
endif
	@echo "[Dry Run] Running backtest against $(DS)..."
	bash -c 'cd services/analyzer && source venv/bin/activate && python dry_run.py --csv "$(DS)"'

dry-run-all:
	@echo "[Dry Run] Running backtest against ALL datasets..."
	bash -c 'cd services/analyzer && source venv/bin/activate && python dry_run.py --csv all'

dry-run-compare:
	@echo "[Dry Run] Comparing WITH vs WITHOUT trend filter..."
	bash -c 'cd services/analyzer && source venv/bin/activate && python dry_run.py --csv compare'

dry-run-mongo:
	@echo "[Dry Run] Running backtest against MongoDB paper trading data..."
	bash -c 'cd services/analyzer && source venv/bin/activate && python dry_run.py --mongo'

chart:
	@echo "[Chart] Serving Dry Run Visualization at http://localhost:8000"
	@lsof -ti:8000 | xargs kill -9 2>/dev/null || true
	python3 data/serve_ui.py

# ===================== KUBERNETES & GITOPS =====================

k8s-build:
	@echo "Building microservices into local Docker..."
	docker build -t trading-frontend:latest ./frontend
	docker build -t trading-analyzer:latest ./services/analyzer
	docker build -t trading-data-ingest:latest ./services/data-ingest
	docker build -t trading-notification:latest ./services/notification

k8s-deploy: k8s-build
	@echo "Deploying newly built images to local cluster..."
	kubectl rollout restart deployment frontend analyzer data-ingest notification -n trading-system

# ===================== DOCKER COMPOSE PRODUCTION =====================

prod-up:
	@echo "Starting production stack (Docker Compose)..."
	docker compose -f docker-compose.prod.yml up -d --build

prod-down:
	@echo "Stopping production stack..."
	docker compose -f docker-compose.prod.yml down

prod-logs:
	docker compose -f docker-compose.prod.yml logs -f --tail=50

prod-status:
	docker compose -f docker-compose.prod.yml ps

# ===================== ORACLE CLOUD (OCI) =====================

ocl-login:
	@echo "Testing OCI API key auth..."
	@oci iam user get --user-id $(shell grep '^user' ~/.oci/config | head -1 | cut -d= -f2 | tr -d ' ') --query 'data.name' --raw-output && echo "✅ OCI auth working" || echo "❌ OCI auth failed — run 'oci setup config'"

ocl-provision:
	@echo "Launching Oracle Cloud Instance with cloud-init.yaml payload..."
	oci compute instance launch \
		--compartment-id $(OCL_COMPARTMENT_ID) \
		--subnet-id $(OCL_SUBNET_ID) \
		--image-id $(OCL_IMAGE_ID) \
		--availability-domain $(OCL_AVAILABILITY_DOMAIN) \
		--shape $(OCL_SHAPE) \
		--assign-public-ip true \
		--display-name "trading-docker-node" \
		--user-data-file cloud-init.yaml

ocl-provision-auto:
	@echo "Starting Oracle Capacity Auto-Retrier..."
	@bash scripts/oci-retry.sh

ocl-setup:
ifndef IP
	$(error Usage: make ocl-setup IP=<vm_public_ip>)
endif
	@echo "📦 Uploading .env to VM $(IP)..."
	scp -o StrictHostKeyChecking=no .env ubuntu@$(IP):/home/ubuntu/trading/.env
	@echo "🔄 Restarting services on VM..."
	ssh -o StrictHostKeyChecking=no ubuntu@$(IP) "cd /home/ubuntu/trading && docker compose -f docker-compose.prod.yml restart"
	@echo "✅ Done! Services running with production secrets."
	@echo "   View logs: ssh ubuntu@$(IP) 'cd /home/ubuntu/trading && docker compose -f docker-compose.prod.yml logs -f'"
