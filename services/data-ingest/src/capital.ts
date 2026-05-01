import WebSocket from 'ws';
import axios from 'axios';
import { Candle } from './models/Candle';
import mongoose from 'mongoose';

/**
 * Capital.com WebSocket live tick stream for XAU/USD (GOLD).
 *
 * Replaces Finnhub WebSocket. Uses Capital.com REST API for session auth,
 * then WebSocket for real-time price streaming.
 *
 * Session tokens (CST + X-SECURITY-TOKEN) expire after 10 min of inactivity.
 * We refresh every 9 min to stay alive.
 */

const DEMO_REST_BASE = 'https://demo-api-capital.backend-capital.com/api/v1';
const LIVE_REST_BASE = 'https://api-capital.backend-capital.com/api/v1';
const DEMO_WS_URL = 'wss://api-streaming-capital.backend-capital.com/connect';
const LIVE_WS_URL = 'wss://api-streaming-capital.backend-capital.com/connect';

const EPIC = 'GOLD'; // Capital.com symbol for XAU/USD

interface LiveCandle {
  symbol: string;
  interval: string;
  timestamp: Date;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  tickVolume: number;
}

interface SessionTokens {
  cst: string;
  securityToken: string;
  expiresAt: number; // Unix ms
}

let sessionTokens: SessionTokens | null = null;

/**
 * Create or refresh a Capital.com REST session.
 * Returns CST + X-SECURITY-TOKEN for authenticated requests.
 */
async function createSession(): Promise<SessionTokens> {
  const apiKey = process.env.CAPITAL_API_KEY;
  const password = process.env.CAPITAL_API_PASSWORD;
  const email = process.env.CAPITAL_EMAIL;
  const isDemo = process.env.CAPITAL_DEMO !== 'false'; // Default to demo
  const baseUrl = isDemo ? DEMO_REST_BASE : LIVE_REST_BASE;

  if (!apiKey || !password || !email) {
    throw new Error('[Capital] Missing CAPITAL_API_KEY, CAPITAL_API_PASSWORD, or CAPITAL_EMAIL in .env');
  }

  console.log(`[Capital] Creating session (${isDemo ? 'DEMO' : 'LIVE'})...`);

  const resp = await axios.post(`${baseUrl}/session`, {
    identifier: email,
    password: password,
    encryptedPassword: false,
  }, {
    headers: {
      'X-CAP-API-KEY': apiKey,
      'Content-Type': 'application/json',
    },
  });

  const cst = resp.headers['cst'];
  const securityToken = resp.headers['x-security-token'];

  if (!cst || !securityToken) {
    throw new Error('[Capital] Session creation failed: missing CST or X-SECURITY-TOKEN in response headers');
  }

  sessionTokens = {
    cst,
    securityToken,
    expiresAt: Date.now() + 9 * 60 * 1000, // Refresh before 10-min expiry
  };

  console.log(`[Capital] ✅ Session created | CST: ${cst.substring(0, 10)}...`);
  return sessionTokens;
}

/**
 * Get current session tokens, refreshing if needed.
 */
async function getSession(): Promise<SessionTokens> {
  if (!sessionTokens || Date.now() > sessionTokens.expiresAt) {
    return await createSession();
  }
  return sessionTokens;
}

/**
 * Start Capital.com WebSocket stream for real-time GOLD prices.
 * Replaces Finnhub's startFinnhubStream().
 */
export async function startCapitalStream(
  symbol: string,
  onTrade: (data: string) => void
): Promise<void> {
  // Track reconnect attempts
  const MAX_RECONNECT_ATTEMPTS = 20;
  let reconnectAttempts = 0;
  let isReconnecting = false;

  // Get initial session
  let session: SessionTokens;
  try {
    session = await createSession();
  } catch (err: any) {
    console.error('[Capital] ❌ Initial session failed:', err?.message || err);
    console.log('[Capital] Retrying in 10s...');
    setTimeout(() => startCapitalStream(symbol, onTrade), 10000);
    return;
  }

  const wsUrl = DEMO_WS_URL; // Demo for now; switch to LIVE_WS_URL for production
  console.log(`[Capital] Connecting WebSocket to ${wsUrl}...`);

  const ws = new WebSocket(wsUrl);
  let currentCandle: LiveCandle | null = null;
  let lastSaveTime = 0;
  let lastDataTime = Date.now();
  let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  let sessionRefreshTimer: ReturnType<typeof setInterval> | null = null;
  let pingTimer: ReturnType<typeof setInterval> | null = null;

  function cleanup() {
    if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
    if (sessionRefreshTimer) { clearInterval(sessionRefreshTimer); sessionRefreshTimer = null; }
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
  }

  function reconnect() {
    if (isReconnecting) return;
    isReconnecting = true;
    cleanup();
    try { ws.terminate(); } catch {}
    reconnectAttempts++;
    if (reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
      console.error(`[Capital] ❌ Max reconnect attempts (${MAX_RECONNECT_ATTEMPTS}) reached. Giving up.`);
      return;
    }
    const delay = Math.min(5000 * Math.pow(1.5, reconnectAttempts - 1), 60000);
    console.log(`[Capital] Reconnecting in ${(delay/1000).toFixed(0)}s... (attempt ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})`);
    setTimeout(() => startCapitalStream(symbol, onTrade), delay);
  }

  ws.on('open', () => {
    console.log('[Capital] WebSocket connected. Subscribing to GOLD...');

    // Subscribe to GOLD market data
    ws.send(JSON.stringify({
      destination: 'marketData.subscribe',
      correlationId: '1',
      cst: session.cst,
      securityToken: session.securityToken,
      payload: { epics: [EPIC] },
    }));

    lastDataTime = Date.now();
    reconnectAttempts = 0;

    // Ping the WS every 5 min to keep alive
    pingTimer = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          destination: 'ping',
          correlationId: String(Date.now()),
          cst: session.cst,
          securityToken: session.securityToken,
        }));
      }
    }, 5 * 60 * 1000);

    // Refresh session tokens every 9 min (before 10-min expiry)
    sessionRefreshTimer = setInterval(async () => {
      try {
        session = await createSession();
        console.log('[Capital] 🔄 Session tokens refreshed');
      } catch (err: any) {
        console.error('[Capital] ⚠️ Session refresh failed:', err?.message);
      }
    }, 9 * 60 * 1000);

    // Check for stale connection every 30s — reconnect if no data for 60s
    heartbeatTimer = setInterval(() => {
      const silenceSecs = (Date.now() - lastDataTime) / 1000;
      if (silenceSecs > 60) {
        console.warn(`[Capital] No data for ${silenceSecs.toFixed(0)}s — forcing reconnect...`);
        reconnect();
      }
    }, 30000);
  });

  ws.on('message', async (rawData: any) => {
    lastDataTime = Date.now();
    try {
      const payloadStr = rawData.toString('utf-8');
      const msg = JSON.parse(payloadStr);

      // Handle subscription confirmation
      if (msg.destination === 'marketData.subscribe') {
        console.log(`[Capital] ✅ Subscribed: ${JSON.stringify(msg.payload?.subscriptions)}`);
        return;
      }

      // Handle ping response
      if (msg.destination === 'ping') return;

      // Handle quote updates
      if (msg.destination !== 'quote' || !msg.payload) return;

      const payload = msg.payload;
      if (payload.epic !== EPIC) return;

      const bid = payload.bid;
      const ask = payload.ofr;
      const price = (bid + ask) / 2; // Midpoint price
      const timestamp = payload.timestamp; // Unix ms

      // Broadcast to frontend in Finnhub-compatible format
      const broadcastMsg = JSON.stringify({
        type: 'trade',
        data: [{
          s: symbol,
          p: price,
          t: timestamp,
          v: 1,
          bid: bid,
          ask: ask,
        }],
      });
      onTrade(broadcastMsg);

      // --- Weekend market closure: skip writes Fri 22:00 → Sun 22:00 UTC ---
      const utcNow = new Date();
      const day = utcNow.getUTCDay();    // 0=Sun, 5=Fri, 6=Sat
      const hour = utcNow.getUTCHours();
      const isWeekendClosed =
        (day === 5 && hour >= 22) || // Friday 22:00+
        (day === 6) ||               // All Saturday
        (day === 0 && hour < 22);    // Sunday before 22:00
      if (isWeekendClosed) {
        // Keep WS alive but don't write stale data to DB
        return;
      }

      // Write live_tick for real-time price access (same as Finnhub)
      const db = mongoose.connection.db;
      if (db) {
        await db.collection('live_tick').updateOne(
          { symbol },
          { $set: { symbol, price, bid, ask, timestamp: Date.now() } },
          { upsert: true }
        ).catch(() => {});
      }

      // Floor timestamp to M1 bucket
      const bucketMs = Math.floor(timestamp / 60000) * 60000;
      const bucketDate = new Date(bucketMs);

      if (!currentCandle || currentCandle.timestamp.getTime() !== bucketDate.getTime()) {
        // Save previous candle as final
        if (currentCandle) {
          await Candle.findOneAndUpdate(
            { symbol: currentCandle.symbol, interval: currentCandle.interval, timestamp: currentCandle.timestamp },
            { $set: { ...currentCandle, isFinal: true } },
            { upsert: true }
          );
        }

        // Start new M1 candle
        currentCandle = {
          symbol,
          interval: '1m',
          timestamp: bucketDate,
          open: price,
          high: price,
          low: price,
          close: price,
          volume: 1,
          tickVolume: 1,
        };
      } else {
        // Update live candle
        currentCandle.high = Math.max(currentCandle.high, price);
        currentCandle.low = Math.min(currentCandle.low, price);
        currentCandle.close = price;
        currentCandle.volume += 1;
        currentCandle.tickVolume += 1;
      }

      // Periodic save (every 10s) of the live candle
      const now = Date.now();
      if (now - lastSaveTime > 10000 && currentCandle) {
        lastSaveTime = now;
        await Candle.findOneAndUpdate(
          { symbol: currentCandle.symbol, interval: currentCandle.interval, timestamp: currentCandle.timestamp },
          { $set: { ...currentCandle, isFinal: false } },
          { upsert: true }
        );
        console.log(
          `[Capital] M1 Live → Bid: ${bid.toFixed(2)} Ask: ${ask.toFixed(2)} Mid: ${price.toFixed(3)} | Ticks: ${currentCandle.tickVolume}`
        );
      }
    } catch (err: any) {
      console.error('[Capital] ⚠️ Parse/DB error:', err?.message || err);
    }
  });

  ws.on('error', (err) => {
    console.error('[Capital] WebSocket error:', err);
  });

  ws.on('close', () => {
    console.log('[Capital] Connection closed.');
    reconnect();
  });
}

/**
 * Get current session tokens for use by other services (e.g., trade execution).
 */
export { getSession, createSession };
