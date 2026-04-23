import express from 'express';
import mongoose from 'mongoose';
import cors from 'cors';
import * as http from 'http';
import { WebSocketServer, WebSocket } from 'ws';
import { startFinnhubStream } from './finnhub';
import { bootstrapHistoricalData, startTwelveDataCron } from './twelvedata';
import { Candle } from './models/Candle';
import * as dotenv from 'dotenv';
import * as path from 'path';

// Load from project root .env
dotenv.config({ path: path.join(__dirname, '..', '..', '..', '.env') });

const app = express();
app.use(cors());

const PORT = process.env.PORT || 4000;
const MONGODB_URI = process.env.MONGODB_URI || 'mongodb://localhost:27017/trading';
const SYMBOL = process.env.SYMBOL || 'OANDA:XAU_USD';
const BOOTSTRAP_HOURS = parseInt(process.env.BOOTSTRAP_HOURS || '6', 10);

const server = http.createServer(app);
const wss = new WebSocketServer({ server, path: '/ws' });
const clients = new Set<WebSocket>();

wss.on('connection', (ws) => {
  console.log(`[WS] Client connected (total: ${clients.size + 1})`);
  clients.add(ws);
  ws.on('close', () => clients.delete(ws));
});

function broadcast(data: string): void {
  for (const client of clients) {
    if (client.readyState === WebSocket.OPEN) {
      client.send(data);
    }
  }
}

async function main() {
  try {
    console.log(`[Data Ingest] Connecting to MongoDB at ${MONGODB_URI}...`);
    await mongoose.connect(MONGODB_URI);
    console.log('[Data Ingest] MongoDB connected.');

    // Check if we need to bootstrap (< BOOTSTRAP_HOURS of data)
    const cutoffTime = new Date(Date.now() - BOOTSTRAP_HOURS * 60 * 60 * 1000);
    const existingCount = await Candle.countDocuments({
      symbol: SYMBOL,
      interval: '1m',
      timestamp: { $gte: cutoffTime }
    });

    const requiredCandles = BOOTSTRAP_HOURS * 60; // 1 per minute
    if (existingCount < requiredCandles * 0.8) {
      console.log(`[Data Ingest] Only ${existingCount}/${requiredCandles} candles in last ${BOOTSTRAP_HOURS}h. Bootstrapping...`);
      await bootstrapHistoricalData(SYMBOL, BOOTSTRAP_HOURS);
    } else {
      console.log(`[Data Ingest] ${existingCount} candles found. Skipping bootstrap.`);
    }

    // Start TwelveData 60s CRON
    startTwelveDataCron(SYMBOL);

    // Start Finnhub live WebSocket
    await startFinnhubStream(SYMBOL, broadcast);

    // REST API
    app.get('/health', (_req, res) => res.send('OK'));

    app.get('/api/candles', async (req, res) => {
      try {
        const { symbol, limit = '500' } = req.query;
        if (!symbol) return res.status(400).json({ error: 'symbol param required' });

        const candles = await Candle.find({
          symbol: symbol as string,
          interval: '1m'
        })
          .sort({ timestamp: -1 })
          .limit(parseInt(limit as string, 10))
          .lean();

        res.json(candles.reverse());
      } catch (err) {
        console.error(err);
        res.status(500).json({ error: 'Server error' });
      }
    });

    server.listen(PORT, () => {
      console.log(`[Data Ingest] Running on :${PORT} | WS: ws://localhost:${PORT}/ws`);
    });
  } catch (err) {
    console.error('[Data Ingest] Init error:', err);
    process.exit(1);
  }
}

main();
