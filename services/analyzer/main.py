"""
XAU/USD Strategy Analyzer — Live Paper Trading Engine
=====================================================
Thin orchestrator that:
  1. Loads M1 candles from MongoDB
  2. Builds a MarketSnapshot
  3. Calls evaluate_strategies() from strategy.py (single source of truth)
  4. Writes trades to MongoDB + sends notifications
  5. Monitors open trades for TP/SL hits
  6. Serves REST API for the frontend

All strategy logic lives in strategy.py — this file does NOT contain
any entry conditions, indicator thresholds, or TP/SL multipliers.
"""

import os
import sys

# Force unbuffered output for real-time logging
os.environ['PYTHONUNBUFFERED'] = '1'
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)
import time
import json
import threading
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests
from pymongo import MongoClient
from dotenv import load_dotenv

from strategy import (
    calc_ema, calc_rsi, calc_atr,
    attach_indicators, resample_m5, resample_ohlcv,
    evaluate_strategies,
    MarketSnapshot, Signal, CooldownState,
    LOT_SIZE, CONTRACT_SIZE, POSITION_OZ, COOLDOWN_SECS,
)

# Load shared .env from project root
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))

MONGO_URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/trading')
SYMBOL = os.getenv('SYMBOL', 'OANDA:XAU_USD')
ANALYZER_PORT = int(os.getenv('ANALYZER_PORT', '4002'))
NOTIFICATION_URL = os.getenv('NOTIFICATION_URL', f"http://localhost:{os.getenv('NOTIFICATION_PORT', '4003')}/api/notify")
SPREAD_OFFSET = float(os.getenv('SPREAD_OFFSET', '0.0'))

# Cooldown state per context timeframe (each TF trades independently)
# Backtested: M5 -$53, M10 +$69, M15 +$61
CONTEXT_TIMEFRAMES = ['10min', '15min']
cooldowns_per_tf = {tf: CooldownState() for tf in CONTEXT_TIMEFRAMES}


# ===================== NOTIFICATION HELPER =====================

notify_fail_count = 0

def notify(type_: str, title: str, message: str, trade: dict = None):
    """Send notification to the notification service."""
    global notify_fail_count
    try:
        payload = {'type': type_, 'title': title, 'message': message}
        if trade:
            clean = {k: str(v) if k == '_id' else v for k, v in trade.items()}
            payload['trade'] = clean
        resp = requests.post(NOTIFICATION_URL, json=payload, timeout=5)
        if resp.status_code != 200:
            notify_fail_count += 1
            print(f"[Notify] ⚠️ HTTP {resp.status_code}: {resp.text[:100]} (fail #{notify_fail_count})")
        else:
            if notify_fail_count > 0:
                print(f"[Notify] ✅ Recovered after {notify_fail_count} failures")
            notify_fail_count = 0
    except requests.exceptions.ConnectionError:
        notify_fail_count += 1
        print(f"[Notify] ❌ Connection refused to {NOTIFICATION_URL} (fail #{notify_fail_count})")
    except requests.exceptions.Timeout:
        notify_fail_count += 1
        print(f"[Notify] ❌ Timeout after 5s (fail #{notify_fail_count})")
    except Exception as e:
        notify_fail_count += 1
        print(f"[Notify] ❌ {type(e).__name__}: {e} (fail #{notify_fail_count})")


# ===================== DATA LOADING =====================

def load_candles(db, symbol, limit=500):
    """Load M1 candles from MongoDB and return as DataFrame."""
    docs = list(db.candles.find({'symbol': symbol, 'interval': '1m'}).sort('timestamp', -1).limit(limit))
    if not docs:
        return None

    df = pd.DataFrame(docs)
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    df.set_index('timestamp', inplace=True)
    df.sort_index(inplace=True)

    # Use tickVolume as volume if available and > 0, else fallback
    if 'tickVolume' in df.columns:
        df['volume'] = df['tickVolume'].where(df['tickVolume'] > 0, df.get('volume', 1))
    df['volume'] = df['volume'].fillna(1).replace(0, 1)

    return df


# ===================== STRATEGY ENGINE =====================

def get_live_price(db):
    """Get the latest live tick price."""
    tick = db.live_tick.find_one({'symbol': SYMBOL})
    if tick and (time.time() * 1000 - tick.get('timestamp', 0)) < 30000:
        return tick['price']
    candle = db.candles.find_one({'symbol': SYMBOL}, sort=[('timestamp', -1)])
    return candle['close'] if candle else None


def _dir_arrow(direction, is_win=None):
    """Return arrow icon for direction. ✅/❌ for win/loss, plain arrow for open."""
    if direction == 'LONG':
        if is_win is True:   return '✅↑'
        if is_win is False:  return '❌↑'
        return '↑'
    else:
        if is_win is True:   return '✅↓'
        if is_win is False:  return '❌↓'
        return '↓'


def _fmt_time_short(epoch_ms):
    """Format epoch ms to HH:MM local time string."""
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=7))  # ICT
    dt = datetime.fromtimestamp(epoch_ms / 1000, tz=tz)
    return dt.strftime('%H:%M')


def _get_today_trades(db):
    """Get all trades from today (local ICT timezone), sorted latest first."""
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=7))
    now = datetime.now(tz)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int(start_of_day.timestamp() * 1000)
    trades = list(db.paper_trades.find(
        {'entryTime': {'$gte': start_ms}}
    ).sort('entryTime', -1))
    return trades


def _build_trade_list(trades, live_price):
    """Build the daily trade list lines.

    Each line: Icon | opened_time | $amount | $entry | TP/SL | peak/low
    Green for active, gray for closed.
    """
    lines = []
    for t in trades:
        direction = t.get('direction', '?')
        status = t.get('status', '?')
        entry = t.get('entryPrice', 0)
        tp = t.get('tp', 0)
        sl = t.get('sl', 0)
        entry_time = _fmt_time_short(t.get('entryTime', 0))
        peak = t.get('peakProfit', 0)
        low = t.get('peakLoss', 0)

        if status == 'CLOSED':
            is_win = t.get('pnl', 0) > 0
            arrow = _dir_arrow(direction, is_win)
            pnl = t.get('pnl', 0)
            pnl_str = f"{'+'if pnl>=0 else ''}${pnl:.2f}"
            reason = 'TP' if t.get('closeReason') == 'TAKE_PROFIT' else 'SL'
            line = f"{arrow} {entry_time} {pnl_str} ${entry:.0f} {reason} pk:${peak:+.1f}/lo:${low:+.1f}"
            # Gray for closed
            lines.append(f"<i>{line}</i>")
        else:
            # Active — compute unrealized
            arrow = _dir_arrow(direction)
            if live_price:
                if direction == 'LONG':
                    unr = (live_price - entry) * POSITION_OZ
                else:
                    unr = (entry - live_price) * POSITION_OZ
                unr_str = f"{'+'if unr>=0 else ''}${unr:.2f}"
            else:
                unr_str = '---'
            tp_dist = abs(tp - entry)
            sl_dist = abs(sl - entry)
            line = f"{arrow} {entry_time} {unr_str} ${entry:.0f} +${tp_dist:.1f}/-${sl_dist:.1f} pk:${peak:+.1f}/lo:${low:+.1f}"
            # Green bold for active
            lines.append(f"<b>{line}</b>")

    return lines


def _build_daily_footer(trades):
    """Build footer line: overall profit, win rate, trade count."""
    closed = [t for t in trades if t.get('status') == 'CLOSED']
    open_count = sum(1 for t in trades if t.get('status') == 'OPEN')
    total_pnl = sum(t.get('pnl', 0) for t in closed)
    wins = sum(1 for t in closed if t.get('pnl', 0) > 0)
    wr = (wins / len(closed) * 100) if closed else 0
    pnl_icon = '📈' if total_pnl >= 0 else '📉'
    return f"{pnl_icon} Day: <b>{'+'if total_pnl>=0 else ''}${total_pnl:.2f}</b> | WR: {wr:.0f}% ({wins}/{len(closed)}) | Trades: {len(trades)}"


def _normalize_tf(tf: str) -> str:
    """Normalize contextTf to display format: 'M5' → '5M', '5M' → '5M'."""
    if tf.startswith('M') and tf[1:].isdigit():
        return tf[1:] + 'M'
    return tf


def _group_trades_by_tf(trades):
    """Group trades by timeframe, maintaining order."""
    from collections import OrderedDict
    tf_order = ['5M', '10M', '15M', '30M']
    grouped = OrderedDict((tf, []) for tf in tf_order)
    for t in trades:
        tf = _normalize_tf(t.get('contextTf', 'M5'))
        if tf not in grouped:
            grouped[tf] = []
        grouped[tf].append(t)
    return grouped


def _build_tf_footer(tf: str, tf_trades: list) -> str:
    """Build per-TF footer: PnL, win rate, trade count."""
    closed = [t for t in tf_trades if t.get('status') == 'CLOSED']
    total_pnl = sum(t.get('pnl', 0) for t in closed)
    wins = sum(1 for t in closed if t.get('pnl', 0) > 0)
    wr = (wins / len(closed) * 100) if closed else 0
    pnl_icon = '📈' if total_pnl >= 0 else '📉'
    return f"{pnl_icon} {tf}: <b>{'+'if total_pnl>=0 else ''}${total_pnl:.2f}</b> | WR: {wr:.0f}% ({wins}/{len(closed)}) | Trades: {len(tf_trades)}"


def build_tf_message(header: str, db, tf: str, live_price=None) -> str:
    """Build a Telegram message for a single TF group.

    Format:
      header
      ━━━ 5M ━━━
      (trades for this TF)
      📈 5M: +$12.50 | WR: 50% (1/2) | Trades: 2
      ──────────
      📈 Day: +$30.00 | WR: 60% (3/5) | Trades: 8

    Telegram max: 4096 characters.
    """
    today_trades = _get_today_trades(db)
    overall_footer = _build_daily_footer(today_trades)
    tf_normalized = _normalize_tf(tf)

    # Filter trades for this TF only
    tf_trades = [t for t in today_trades if _normalize_tf(t.get('contextTf', 'M5')) == tf_normalized]

    parts = [header, '']

    if tf_trades:
        parts.append(f"━━━ {tf_normalized} ━━━")
        parts.extend(_build_trade_list(tf_trades, live_price))
        parts.append('')
        parts.append(_build_tf_footer(tf_normalized, tf_trades))
    else:
        parts.append(f"━━━ {tf_normalized} ━━━")
        parts.append('<i>No trades yet</i>')

    parts.append('──────────')
    parts.append(overall_footer)
    parts.append('')
    parts.append('━━━━━━━━━━oOo━━━━━━━━━━')
    parts.append('')
    return '\n'.join(parts)


def build_snapshot(df_m1, df_m5, df_m5_shifted, db) -> MarketSnapshot:
    """Build a MarketSnapshot from the current M1/M5 data.

    Uses the LIVE (partial) M1 bar with live tick price injected for
    minimum latency. M5 indicators use completed shifted bars for stability.

    Returns None if data is insufficient.
    """
    if len(df_m1) < 3:
        return None

    live_price = get_live_price(db)

    # Use bar -1 (current live partial candle) with live tick injected
    idx = -1
    m1 = df_m1.iloc[idx].copy()

    # Inject live tick price into the partial bar for freshest data
    if live_price:
        m1['close'] = live_price
        m1['high'] = max(m1['high'], live_price)
        m1['low'] = min(m1['low'], live_price)

    # Check for NaN in critical M1 fields
    if pd.isna(m1['rsi']) or pd.isna(m1['ema21']):
        return None

    # Find M5 values for this M1 timestamp
    m1_time = df_m1.index[idx]
    m5_idx = df_m5_shifted.index.searchsorted(m1_time) - 1
    if m5_idx < 0 or m5_idx >= len(df_m5_shifted):
        return None

    m5 = df_m5_shifted.iloc[m5_idx]

    # Check for NaN in critical M5 fields
    if pd.isna(m5['atr']) or pd.isna(m5['ema9']) or pd.isna(m5['rsi']):
        return None

    # Slope lookback: 3 M5 candles back for trend detection
    has_slope = False
    m5_ema9_prev = None
    m5_ema21_prev = None
    if len(df_m5) >= 6:
        m5_prev = df_m5.iloc[-4]  # 3 M5 candles back (15 min)
        if not pd.isna(m5_prev['ema9']) and not pd.isna(m5_prev['ema21']):
            has_slope = True
            m5_ema9_prev = m5_prev['ema9']
            m5_ema21_prev = m5_prev['ema21']

    entry = live_price or m1['close']

    return MarketSnapshot(
        m1_close=m1['close'],
        m1_high=m1['high'],
        m1_low=m1['low'],
        m1_rsi=m1['rsi'],
        m1_ema21=m1['ema21'],
        m1_upper_bb=m1['upper_bb'],
        m1_lower_bb=m1['lower_bb'],
        m1_bb_sma=m1['bb_sma'],
        m1_atr=m1['atr'],
        m5_atr=m5['atr'],
        m5_ema9=m5['ema9'],
        m5_ema21=m5['ema21'],
        m5_ema50=m5['ema50'],
        m5_rsi=m5['rsi'],
        m5_close=m5['close'],
        m5_upper_bb=m5['upper_bb'],
        m5_lower_bb=m5['lower_bb'],
        m5_volume=m5['volume'],
        m5_vol_sma20=m5['vol_sma20'] if not pd.isna(m5['vol_sma20']) else 1,
        m5_ema9_prev=m5_ema9_prev,
        m5_ema21_prev=m5_ema21_prev,
        has_slope_data=has_slope,
        live_price=entry,
    )


def run_strategies(db):
    """Execute all strategies across all context timeframes. Called every 5s."""
    df_m1 = load_candles(db, SYMBOL, 500)
    if df_m1 is None or len(df_m1) < 50:
        print(f"[Analyzer] Insufficient data: {len(df_m1) if df_m1 is not None else 0} M1 candles. Need 50+.")
        return

    # Compute M1 indicators once (shared across all context TFs)
    df_m1 = attach_indicators(df_m1)

    for ctx_tf in CONTEXT_TIMEFRAMES:
        tf_label = ctx_tf.upper().replace('MIN', 'M')  # '5min' → '5M'

        # Resample M1 → context TF
        df_ctx = resample_ohlcv(df_m1, ctx_tf)
        min_bars = max(20, 50 // int(ctx_tf.replace('min', '')))
        if len(df_ctx) < min_bars:
            continue  # Not enough data yet for this TF

        df_ctx = attach_indicators(df_ctx)
        df_ctx_shifted = df_ctx.shift(1)

        # Build snapshot using this context TF
        snap = build_snapshot(df_m1, df_ctx, df_ctx_shifted, db)
        if snap is None:
            continue

        # Evaluate strategies with per-TF cooldowns
        now = time.time()
        cooldowns = cooldowns_per_tf[ctx_tf]
        signals = evaluate_strategies(snap, cooldowns, now, SPREAD_OFFSET)

        # Execute signals
        for sig in signals:
            trade_doc = {
                'symbol': SYMBOL, 'direction': sig.direction, 'status': 'OPEN',
                'entryPrice': round(sig.entry_price, 3),
                'tp': round(sig.tp, 3), 'sl': round(sig.sl, 3),
                'entryTime': int(now * 1000),
                'signalType': sig.strategy, 'meta': sig.meta, 'lotSize': LOT_SIZE,
                'contextTf': tf_label,
            }
            db.paper_trades.insert_one(trade_doc)

            # Notification: TF + direction + entry + RSI condition + TP/SL
            arrow = '↑' if sig.direction == 'LONG' else '↓'
            tp_dist = abs(sig.tp - sig.entry_price)
            sl_dist = abs(sig.sl - sig.entry_price)
            rsi = sig.meta.get('m1_rsi', 0)
            # Show RSI threshold so user can confirm on MT5
            if sig.strategy == 'BB_REVERSION':
                rsi_cond = '≤25' if sig.direction == 'LONG' else '≥75'
            else:
                rsi_cond = '≤45' if sig.direction == 'LONG' else '≥55'
            header = f"{arrow} <b>NEW {tf_label} {sig.direction} ${sig.entry_price:.2f} | RSI {rsi:.0f} ({rsi_cond}) | TP +${tp_dist:.1f} | SL -${sl_dist:.1f}</b>"

            live = get_live_price(db) or sig.entry_price
            msg = build_tf_message(header, db, tf=tf_label, live_price=live)
            notify('TRADE_OPEN', None, msg, trade_doc)
            print(f"[{sig.strategy}·{tf_label}] {sig.direction} at {sig.entry_price:.3f} | TP: {sig.tp:.3f} | SL: {sig.sl:.3f}")


# ===================== REAL-TIME BROADCAST =====================

def get_frontend_payload(db):
    """Generate the full data payload for the frontend — today's trades only."""
    from datetime import datetime as dt, timezone as tz, timedelta
    ict = tz(timedelta(hours=7))
    now = dt.now(ict)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int(start_of_day.timestamp() * 1000)

    trades = list(db.paper_trades.find(
        {'entryTime': {'$gte': start_ms}}
    ).sort('entryTime', -1))
    for t in trades:
        t['_id'] = str(t['_id'])
        for k, v in t.items():
            if hasattr(v, '__str__') and type(v).__name__ in ('ObjectId', 'datetime'):
                t[k] = str(v)

    live = get_live_price(db)

    indicators = {}
    df_m1 = load_candles(db, SYMBOL, 100)
    if df_m1 is not None and len(df_m1) >= 20:
        df_m1 = attach_indicators(df_m1)
        df_m5 = resample_m5(df_m1)
        if len(df_m5) >= 20:
            df_m5 = attach_indicators(df_m5)
            m5_last = df_m5.iloc[-2] if len(df_m5) >= 2 else df_m5.iloc[-1]
            m1_last = df_m1.iloc[-2] if len(df_m1) >= 2 else df_m1.iloc[-1]
            indicators = {
                'm1_rsi': round(float(m1_last['rsi']), 2) if not pd.isna(m1_last['rsi']) else None,
                'm1_ema21': round(float(m1_last['ema21']), 3) if not pd.isna(m1_last['ema21']) else None,
                'm1_upper_bb': round(float(m1_last['upper_bb']), 3) if not pd.isna(m1_last['upper_bb']) else None,
                'm1_lower_bb': round(float(m1_last['lower_bb']), 3) if not pd.isna(m1_last['lower_bb']) else None,
                'm5_ema9': round(float(m5_last['ema9']), 3) if not pd.isna(m5_last['ema9']) else None,
                'm5_ema21': round(float(m5_last['ema21']), 3) if not pd.isna(m5_last['ema21']) else None,
                'm5_ema50': round(float(m5_last['ema50']), 3) if not pd.isna(m5_last['ema50']) else None,
                'm5_rsi': round(float(m5_last['rsi']), 2) if not pd.isna(m5_last['rsi']) else None,
                'm5_atr': round(float(m5_last['atr']), 3) if not pd.isna(m5_last['atr']) else None,
            }

    return {
        'trades': trades,
        'livePrice': live,
        'indicators': indicators,
    }

def broadcast_trades(db):
    """Broadcast the full trade state via notification WS."""
    try:
        payload = get_frontend_payload(db)
        payload['type'] = 'TRADES_UPDATE'
        requests.post(NOTIFICATION_URL, json=payload, timeout=2)
    except Exception:
        pass


# ===================== TP/SL MONITOR =====================

def monitor_trades(db):
    """Check all OPEN trades for TP/SL hits. Called every 1s."""
    live_price = get_live_price(db)
    if not live_price:
        return

    open_trades = list(db.paper_trades.find({'symbol': SYMBOL, 'status': 'OPEN'}))
    for trade in open_trades:
        direction = trade['direction']
        tp = trade.get('tp')
        sl = trade.get('sl')
        entry = trade['entryPrice']

        if not tp or not sl:
            continue

        close_reason = None
        if direction == 'LONG':
            if live_price >= tp:
                close_reason = 'TAKE_PROFIT'
            elif live_price <= sl:
                close_reason = 'STOP_LOSS'
        else:
            if live_price <= tp:
                close_reason = 'TAKE_PROFIT'
            elif live_price >= sl:
                close_reason = 'STOP_LOSS'

        if close_reason:
            if direction == 'LONG':
                pnl = (live_price - entry) * POSITION_OZ
            else:
                pnl = (entry - live_price) * POSITION_OZ

            db.paper_trades.update_one(
                {'_id': trade['_id']},
                {'$set': {
                    'status': 'CLOSED',
                    'exitPrice': round(live_price, 3),
                    'exitTime': int(time.time() * 1000),
                    'pnl': round(pnl, 2),
                    'closeReason': close_reason,
                }}
            )

            strat = trade.get('signalType', '?')
            ctx_tf = trade.get('contextTf', 'M5')
            hold_mins = (time.time() * 1000 - trade.get('entryTime', 0)) / 60000

            # Build header: colored arrow | TF | amount | entry→exit | hold time
            is_tp = close_reason == 'TAKE_PROFIT'
            arrow_icon = _dir_arrow(direction, is_win=is_tp)
            pnl_str = f"{'+'if pnl>=0 else ''}${pnl:.2f}"
            header = f"{arrow_icon} <b>CLOSED {ctx_tf} {pnl_str} | ${entry:.2f} → ${live_price:.2f} | {hold_mins:.0f}m</b>"

            msg = build_tf_message(header, db, tf=ctx_tf, live_price=live_price)
            notify('TRADE_CLOSE', None, msg)
            print(f"[Monitor] {'✅' if is_tp else '❌'} {direction} {close_reason} | Entry: {entry:.2f} | Exit: {live_price:.2f} | PnL: ${pnl:.2f} ({ctx_tf})")

    # Update peak profit/loss for open trades
    for trade in open_trades:
        if trade.get('status') != 'OPEN':
            continue
        direction = trade['direction']
        entry = trade['entryPrice']
        if direction == 'LONG':
            unrealized = (live_price - entry) * POSITION_OZ
        else:
            unrealized = (entry - live_price) * POSITION_OZ

        updates = {}
        if unrealized > trade.get('peakProfit', 0):
            updates['peakProfit'] = round(unrealized, 2)
        if unrealized < trade.get('peakLoss', 0):
            updates['peakLoss'] = round(unrealized, 2)
        if updates:
            db.paper_trades.update_one({'_id': trade['_id']}, {'$set': updates})


# ===================== REST API =====================

def start_api(db):
    """Start the REST API."""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import json as json_mod
    from urllib.parse import urlparse, parse_qs

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _send_json(self, data, status=200):
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, DELETE, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.end_headers()
            self.wfile.write(json_mod.dumps(data, default=str).encode())

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, DELETE, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)

            if parsed.path == '/health':
                self._send_json({'status': 'ok'})

            elif parsed.path == '/api/paper-trades':
                payload = get_frontend_payload(db)
                self._send_json(payload)

            elif parsed.path == '/api/paper-trades/stats':
                from datetime import datetime as dt, timezone as tz, timedelta
                ict = tz(timedelta(hours=7))
                now = dt.now(ict)
                start_ms = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
                today_filter = {'entryTime': {'$gte': start_ms}}
                closed = list(db.paper_trades.find({**today_filter, 'status': 'CLOSED'}))
                wins = sum(1 for t in closed if t.get('pnl', 0) > 0)
                losses = sum(1 for t in closed if t.get('pnl', 0) < 0)
                total_pnl = sum(t.get('pnl', 0) for t in closed)
                wr = (wins / len(closed) * 100) if closed else 0
                open_count = db.paper_trades.count_documents({**today_filter, 'status': 'OPEN'})
                self._send_json({
                    'wins': wins, 'losses': losses, 'totalPnl': round(total_pnl, 2),
                    'winRate': round(wr, 1), 'openTrades': open_count, 'totalTrades': len(closed)
                })

            else:
                self._send_json({'error': 'Not found'}, 404)

        def do_DELETE(self):
            parsed = urlparse(self.path)
            if parsed.path == '/api/paper-trades':
                result = db.paper_trades.delete_many({})
                self._send_json({'success': True, 'deleted': result.deleted_count})
                print(f"[API] 🧹 Cleared all paper trades ({result.deleted_count})")

            elif parsed.path.startswith('/api/paper-trades/'):
                trade_id = parsed.path.split('/')[-1]
                from bson import ObjectId
                try:
                    result = db.paper_trades.delete_one({'_id': ObjectId(trade_id)})
                    self._send_json({'success': True, 'deleted': result.deleted_count})
                    print(f"[API] Deleted trade {trade_id}")
                except Exception as e:
                    self._send_json({'error': str(e)}, 400)
            else:
                self._send_json({'error': 'Not found'}, 404)

    httpd = HTTPServer(('0.0.0.0', ANALYZER_PORT), Handler)
    print(f"[Analyzer API] Running on :{ANALYZER_PORT}")
    httpd.serve_forever()


# ===================== MAIN LOOP =====================

def main():
    print("=" * 60)
    print("  XAU/USD Strategy Analyzer — Live Paper Trading")
    print(f"  Symbol: {SYMBOL} | Lot: {LOT_SIZE} | Port: {ANALYZER_PORT}")
    print("=" * 60)

    client = MongoClient(MONGO_URI)
    db = client.get_default_database()
    print(f"[Analyzer] Connected to MongoDB: {MONGO_URI}")

    # Start REST API in background thread
    api_thread = threading.Thread(target=start_api, args=(db,), daemon=True)
    api_thread.start()

    # Main loop
    last_strategy_run = 0
    last_heartbeat = 0
    STRATEGY_INTERVAL = 5
    HEARTBEAT_INTERVAL = 30

    while True:
        try:
            now = time.time()

            monitor_trades(db)
            broadcast_trades(db)

            if now - last_strategy_run >= STRATEGY_INTERVAL:
                last_strategy_run = now
                run_strategies(db)

            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                last_heartbeat = now
                live = get_live_price(db)
                open_count = db.paper_trades.count_documents({'status': 'OPEN'})
                closed_count = db.paper_trades.count_documents({'status': 'CLOSED'})
                candle_count = db.candles.count_documents({'symbol': SYMBOL})
                price_str = f"${live:.2f}" if live else 'N/A'
                print(f"[Analyzer ♥] Price: {price_str} | Candles: {candle_count} | Open: {open_count} | Closed: {closed_count}")
                sys.stdout.flush()

            time.sleep(1)

        except KeyboardInterrupt:
            print("\n[Analyzer] Shutting down...")
            break
        except Exception as e:
            print(f"[Analyzer] Error: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(5)


if __name__ == '__main__':
    main()
