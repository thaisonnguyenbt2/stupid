.PHONY: dev dev-frontend dev-ingest dev-analyzer dev-notification install clean-ghosts

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
	docker compose up -d

db-down:
	docker compose down
