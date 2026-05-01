import axios from 'axios';
import { Candle } from './models/Candle';

/**
 * TwelveData integration for XAU/USD historical candle sync.
 * 
 * TIMESTAMP HANDLING:
 * TwelveData returns datetime as "2026-04-08 16:30:00" in UTC.
 * We parse this to a JS Date object for MongoDB storage.
 * This matches the Finnhub timestamp format (also stored as Date).
 */

function getTwelveDataSymbol(sym: string): string {
  if (sym.includes(':')) {
    const pair = sym.split(':')[1];
    if (pair.includes('_')) return pair.replace('_', '/');
    return pair;
  }
  return sym;
}

/**
 * Bootstrap: Fetch last N hours of 1m candles and bulk upsert.
 * Called once on startup if MongoDB has < bootstrapHours of data.
 */
export async function bootstrapHistoricalData(symbol: string, hours: number): Promise<number> {
  const API_KEY = process.env.TWELVEDATA_API_KEY;
  if (!API_KEY) {
    console.warn('[TwelveData] No API key, skipping bootstrap.');
    return 0;
  }

  const tdSymbol = getTwelveDataSymbol(symbol);
  const outputSize = hours * 60; // 1 candle per minute
  const url = `https://api.twelvedata.com/time_series?symbol=${tdSymbol}&interval=1min&outputsize=${outputSize}&timezone=UTC&apikey=${API_KEY}`;

  console.log(`[TwelveData] Bootstrapping ${hours}h of data (${outputSize} candles)...`);

  try {
    const res = await axios.get(url);
    const data = res.data;

    if (data.status === 'error') {
      console.error('[TwelveData Bootstrap Error]', data.message);
      return 0;
    }
    if (!data.values || !Array.isArray(data.values)) return 0;

    let count = 0;
    for (const item of data.values) {
      // TwelveData format: "2026-04-08 16:30:00" — parse as UTC by appending 'Z'
      // This ensures consistency with Finnhub's Unix ms timestamps — both produce UTC Date objects.
      const timestampDate = new Date(`${item.datetime.replace(' ', 'T')}Z`);
      if (isNaN(timestampDate.getTime())) continue;

      await Candle.findOneAndUpdate(
        { symbol, interval: '1m', timestamp: timestampDate },
        {
          $set: {
            open: parseFloat(item.open),
            high: parseFloat(item.high),
            low: parseFloat(item.low),
            close: parseFloat(item.close),
            volume: parseFloat(item.volume || '0'),
            isFinal: true
          }
        },
        { upsert: true }
      );
      count++;
    }

    console.log(`[TwelveData] Bootstrap complete: ${count} candles synced.`);
    return count;
  } catch (err) {
    console.error('[TwelveData Bootstrap Error]', err);
    return 0;
  }
}

/**
 * CRON: Fetch latest candles every TWELVEDATA_POLL_INTERVAL_MS (default 60s).
 * Only fetches last 5 candles to stay within API limits.
 */
export function startTwelveDataCron(symbol: string): void {
  const API_KEY = process.env.TWELVEDATA_API_KEY;
  const POLL_MS = parseInt(process.env.TWELVEDATA_POLL_INTERVAL_MS || '60000', 10);

  if (!API_KEY) {
    console.warn('[TwelveData] No API key, CRON disabled.');
    return;
  }

  const tdSymbol = getTwelveDataSymbol(symbol);

  const syncTask = async () => {
    try {
      // --- Weekend market closure: skip writes Fri 22:00 → Sun 22:00 UTC ---
      const utcNow = new Date();
      const day = utcNow.getUTCDay();    // 0=Sun, 5=Fri, 6=Sat
      const hour = utcNow.getUTCHours();
      const isWeekendClosed =
        (day === 5 && hour >= 22) || // Friday 22:00+
        (day === 6) ||               // All Saturday
        (day === 0 && hour < 22);    // Sunday before 22:00
      if (isWeekendClosed) return;

      const url = `https://api.twelvedata.com/time_series?symbol=${tdSymbol}&interval=1min&outputsize=5&timezone=UTC&apikey=${API_KEY}`;
      const res = await axios.get(url);
      const data = res.data;

      if (data.status === 'error') {
        console.error('[TwelveData CRON]', data.message);
        return;
      }
      if (!data.values || !Array.isArray(data.values)) return;

      let synced = 0;
      for (const item of data.values) {
        const timestampDate = new Date(`${item.datetime.replace(' ', 'T')}Z`);
        if (isNaN(timestampDate.getTime())) continue;

        await Candle.findOneAndUpdate(
          { symbol, interval: '1m', timestamp: timestampDate },
          {
            $set: {
              open: parseFloat(item.open),
              high: parseFloat(item.high),
              low: parseFloat(item.low),
              close: parseFloat(item.close),
              volume: parseFloat(item.volume || '0'),
              isFinal: true
            }
          },
          { upsert: true }
        );
        synced++;
      }

      console.log(`[TwelveData] Synced ${synced} candles (poll interval: ${POLL_MS / 1000}s)`);
    } catch (err) {
      console.error('[TwelveData CRON Error]', err);
    }
  };

  console.log(`[TwelveData] CRON armed: polling every ${POLL_MS / 1000}s for ${symbol}`);
  syncTask(); // Run once immediately
  setInterval(syncTask, POLL_MS);
}
