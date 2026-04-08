import WebSocket from 'ws';
import { Candle } from './models/Candle';
import mongoose from 'mongoose';

/**
 * Finnhub WebSocket live tick stream for XAU/USD.
 *
 * TIMESTAMP HANDLING:
 * Finnhub sends trade.t as Unix milliseconds (number).
 * We floor to M1 bucket and store as JS Date in MongoDB.
 * This matches TwelveData's Date format in the same collection.
 *
 * TICK VOLUME:
 * Raw volume from Finnhub for forex is often 0 or unreliable.
 * We track `tickVolume` (count of ticks per M1 candle) as the
 * reliable volume proxy — same approach used by MT4/MT5/Exness.
 */

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

export async function startFinnhubStream(
  symbol: string,
  onTrade: (data: string) => void
): Promise<void> {
  const apiKey = process.env.FINNHUB_API_KEY;
  if (!apiKey) {
    console.error('[Finnhub] No API key provided. Set FINNHUB_API_KEY in .env');
    return;
  }

  const wsUrl = `wss://ws.finnhub.io?token=${apiKey}`;
  console.log(`[Finnhub] Connecting to WebSocket for ${symbol}...`);

  const ws = new WebSocket(wsUrl);
  let currentCandle: LiveCandle | null = null;
  let lastSaveTime = 0;

  ws.on('open', () => {
    console.log('[Finnhub] Connected. Subscribing...');
    ws.send(JSON.stringify({ type: 'subscribe', symbol }));
  });

  ws.on('message', async (rawData: any) => {
    try {
      const payloadStr = rawData.toString('utf-8');
      onTrade(payloadStr); // Broadcast raw to WS clients (frontend)

      const msg = JSON.parse(payloadStr);
      if (msg.type !== 'trade' || !msg.data) return;

      // Process only the latest trade for our symbol
      const trades = msg.data.filter((t: any) => t.s === symbol);
      if (trades.length === 0) return;

      const latestTrade = trades.reduce(
        (best: any, t: any) => (!best || t.t > best.t ? t : best),
        null
      );

      const price = latestTrade.p;
      const rawVolume = latestTrade.v || 0;

      // Write live_tick for real-time price access
      const db = mongoose.connection.db;
      if (db) {
        await db.collection('live_tick').updateOne(
          { symbol },
          { $set: { symbol, price, timestamp: Date.now() } },
          { upsert: true }
        ).catch(() => {});
      }

      // Floor timestamp to M1 bucket (Date object for MongoDB consistency)
      const tradeMs = latestTrade.t; // Unix ms from Finnhub
      const bucketMs = Math.floor(tradeMs / 60000) * 60000;
      const bucketDate = new Date(bucketMs);

      if (!currentCandle || currentCandle.timestamp.getTime() !== bucketDate.getTime()) {
        // Save the previous candle as final
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
          volume: rawVolume,
          tickVolume: 1,  // First tick
        };
      } else {
        // Update live candle
        currentCandle.high = Math.max(currentCandle.high, price);
        currentCandle.low = Math.min(currentCandle.low, price);
        currentCandle.close = price;
        currentCandle.volume += rawVolume;
        currentCandle.tickVolume += 1; // Count every tick
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
          `[Finnhub] M1 Live → Close: ${price.toFixed(3)} | Ticks: ${currentCandle.tickVolume}`
        );
      }
    } catch (err) {
      console.error('[Finnhub] Parse/DB error:', err);
    }
  });

  ws.on('error', (err) => {
    console.error('[Finnhub] WebSocket error:', err);
  });

  ws.on('close', () => {
    console.log('[Finnhub] Connection closed. Reconnecting in 5s...');
    setTimeout(() => startFinnhubStream(symbol, onTrade), 5000);
  });
}
