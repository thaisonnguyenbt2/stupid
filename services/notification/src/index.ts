import express from 'express';
import cors from 'cors';
import axios from 'axios';
import * as http from 'http';
import { WebSocketServer, WebSocket } from 'ws';
import * as dotenv from 'dotenv';
import * as path from 'path';

dotenv.config({ path: path.join(__dirname, '..', '..', '..', '.env') });

const app = express();
app.use(cors());
app.use(express.json());

const PORT = process.env.NOTIFICATION_PORT || 4003;
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN || '';
const TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID || '';
const TELEGRAM_CHAT_ID_2 = process.env.TELEGRAM_CHAT_ID_2 || '';
const TELEGRAM_CHAT_ID_3 = process.env.TELEGRAM_CHAT_ID_3 || '';

// Slot → chat routing: '2' → CHAT_ID_2 (1:1 R:R), '3' → CHAT_ID_3 (1.7:1 R:R)
const CHAT_ID_MAP: Record<string, string> = {
  'default': TELEGRAM_CHAT_ID,
  '2': TELEGRAM_CHAT_ID_2,
  '3': TELEGRAM_CHAT_ID_3,
};

const server = http.createServer(app);
const wss = new WebSocketServer({ server, path: '/ws' });
const clients = new Set<WebSocket>();

wss.on('connection', (ws) => {
  console.log(`[Notification WS] Client connected (total: ${clients.size + 1})`);
  clients.add(ws);
  ws.on('close', () => clients.delete(ws));
});

function broadcastEvent(event: any): void {
  const payload = JSON.stringify(event);
  for (const client of clients) {
    if (client.readyState === WebSocket.OPEN) {
      client.send(payload);
    }
  }
}

async function sendTelegram(message: string, chatId?: string): Promise<boolean> {
  const targetChatId = chatId || TELEGRAM_CHAT_ID;
  if (!TELEGRAM_BOT_TOKEN || TELEGRAM_BOT_TOKEN === 'YOUR_BOT_TOKEN_HERE' || !targetChatId) {
    console.warn('[Telegram] Missing credentials. Message:', message.substring(0, 80));
    return false;
  }

  try {
    const url = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`;
    const res = await axios.post(url, {
      chat_id: targetChatId,
      text: message,
      parse_mode: 'HTML'
    }, { timeout: 10000 });

    if (res.status === 200) {
      const chatLabel = chatId ? `chat:${targetChatId}` : 'default';
      console.log(`[Telegram] ✅ Sent (${chatLabel}): ${message.substring(0, 60)}...`);
      return true;
    } else {
      console.error('[Telegram] Failed:', res.data);
      return false;
    }
  } catch (err: any) {
    console.error('[Telegram] Error:', err.message);
    return false;
  }
}

/**
 * POST /api/notify
 * Body: { type, title, message, trade? }
 *
 * type: 'TRADE_OPEN' | 'TRADE_CLOSE' | 'ALERT' | 'INFO'
 */
app.post('/api/notify', async (req, res) => {
  try {
    const { type, title, message, trade, trades, livePrice } = req.body;

    if (!message && type !== 'TRADES_UPDATE') {
      return res.status(400).json({ error: 'message is required' });
    }

    let sent = false;

    // Skip Telegram for bulk state broadcasts (TRADES_UPDATE)
    if (type !== 'TRADES_UPDATE') {
      const { targetChat } = req.body;
      const resolvedChatId = CHAT_ID_MAP[targetChat || 'default'] || TELEGRAM_CHAT_ID;
      const telegramMsg = title ? `${title}\n\n${message}` : message;
      sent = await sendTelegram(telegramMsg, resolvedChatId);
    }

    // Broadcast to WebSocket clients (frontend)
    broadcastEvent({
      type: type || 'INFO',
      title,
      message,
      trade,
      trades,       // Full trade list for TRADES_UPDATE
      livePrice,    // Live price for TRADES_UPDATE
      timestamp: Date.now()
    });

    res.json({ success: true, telegram: sent });
  } catch (err) {
    console.error('[Notification] Error:', err);
    res.status(500).json({ error: 'Notification failed' });
  }
});

app.get('/health', (_req, res) => res.send('OK'));

server.listen(PORT, () => {
  console.log(`[Notification] Running on :${PORT} | WS: ws://localhost:${PORT}/ws`);
  console.log(`[Notification] Telegram: ${TELEGRAM_BOT_TOKEN ? 'Configured ✅' : 'Not configured ❌'}`);
});
