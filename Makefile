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

# Run against a specific CSV file:
#   make dry-run FILE=data/DAT_ASCII_XAUUSD_T_202603.csv
#
# Run against a JSON candles file:
#   make dry-run FILE=data/chart_candles_202603.json
#
# Run all available datasets:
#   make dry-run-all

dry-run:
ifndef FILE
	@echo "Usage: make dry-run FILE=data/DAT_ASCII_XAUUSD_T_202603.csv"
	@exit 1
endif
	@echo "[Dry Run] Running analyzer backtest against $(FILE)..."
	bash -c 'cd services/analyzer && source venv/bin/activate && python dry_run.py "../../$(FILE)"'

dry-run-all:
	@echo "[Dry Run] Running analyzer backtest against ALL datasets..."
	bash -c 'cd data && source ../services/analyzer/venv/bin/activate && python dry_run_xau.py all'

chart:
	@echo "[Chart] Serving Dry Run Visualization at http://localhost:8000"
	@lsof -ti:8000 | xargs kill -9 2>/dev/null || true
	python3 data/serve_ui.py

