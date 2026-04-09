"""
XAU/USD Strategy Analyzer — Live Paper Trading Engine
=====================================================
Implements the exact strategies from XAUUSD_STRATEGIES.md:
  A) EMA Trend Pullback
  B) Bollinger Band Mean Reversion
  C) Institutional Breakout (Volume Anomaly)

Primary scanning timeframe: M5 (indicators computed on M5)
Entry precision: M1 candles trigger entries
Volume: tickVolume field (tick count) used as reliable proxy

Exness 0.01 lot sizing: 1 lot = 100 oz, 0.01 lot = 1 oz
PnL = price_movement × 1.0
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

# Load shared .env from project root
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))

MONGO_URI = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/trading')
SYMBOL = os.getenv('SYMBOL', 'OANDA:XAU_USD')
ANALYZER_PORT = int(os.getenv('ANALYZER_PORT', '4002'))
NOTIFICATION_URL = os.getenv('NOTIFICATION_URL', f"http://localhost:{os.getenv('NOTIFICATION_PORT', '4003')}/api/notify")

LOT_SIZE = 0.01
CONTRACT_SIZE = 100  # 1 lot = 100 oz
POSITION_OZ = LOT_SIZE * CONTRACT_SIZE  # 1.0 oz

# Cooldowns (seconds)
COOLDOWN_SECS = 600  # 10 minutes (matches backtest)
last_ema_time = 0.0
last_bb_time = 0.0
last_inst_time = 0.0


# ===================== INDICATOR FUNCTIONS =====================

def calc_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_atr(df, period=14):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = ranges.max(axis=1)
    return true_range.rolling(period).mean()


# ===================== NOTIFICATION HELPER =====================

notify_fail_count = 0

def notify(type_: str, title: str, message: str, trade: dict = None):
    """Send notification to the notification service."""
    global notify_fail_count
    try:
        payload = {'type': type_, 'title': title, 'message': message}
        if trade:
            # Sanitize MongoDB ObjectId — not JSON serializable
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

    # TIMESTAMP HANDLING: MongoDB stores as Date objects.
    # Convert to pandas DatetimeIndex (UTC).
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    df.set_index('timestamp', inplace=True)
    df.sort_index(inplace=True)

    # Use tickVolume as volume if available and > 0, else fallback to volume field
    if 'tickVolume' in df.columns:
        df['volume'] = df['tickVolume'].where(df['tickVolume'] > 0, df.get('volume', 1))
    # Ensure volume is never 0 (prevents division errors)
    df['volume'] = df['volume'].fillna(1).replace(0, 1)

    return df


def resample_m5(df_m1):
    """Resample M1 candles to M5 with tick volume."""
    df_m5 = df_m1.resample('5min').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    })
    df_m5.dropna(inplace=True)
    return df_m5


def attach_indicators(df):
    """Compute all indicators on a DataFrame."""
    df['ema9'] = calc_ema(df['close'], 9)
    df['ema21'] = calc_ema(df['close'], 21)
    df['ema50'] = calc_ema(df['close'], 50)
    df['rsi'] = calc_rsi(df['close'], 14)
    df['atr'] = calc_atr(df, 14)
    df['bb_sma'] = df['close'].rolling(20).mean()
    df['bb_std'] = df['close'].rolling(20).std()
    df['upper_bb'] = df['bb_sma'] + (df['bb_std'] * 2.0)
    df['lower_bb'] = df['bb_sma'] - (df['bb_std'] * 2.0)
    df['vol_sma20'] = df['volume'].rolling(20).mean()
    return df


# ===================== STRATEGY ENGINE =====================

def get_live_price(db):
    """Get the latest live tick price."""
    tick = db.live_tick.find_one({'symbol': SYMBOL})
    if tick and (time.time() * 1000 - tick.get('timestamp', 0)) < 30000:
        return tick['price']
    # Fallback to latest candle close
    candle = db.candles.find_one({'symbol': SYMBOL}, sort=[('timestamp', -1)])
    return candle['close'] if candle else None


def format_trade_message(action, direction, price, tp, sl, strategy, meta):
    """Format a trade notification message."""
    emoji = '🟢' if direction == 'LONG' else '🔴'
    lines = [
        f"{emoji} <b>{action} {direction} | Entry: ${price:.2f} | TP: ${tp:.2f} | SL: ${sl:.2f}</b>",
        f"Strategy: <b>{strategy}</b>",
        f"Lot: 0.01 (1 oz)",
        ""
    ]
    for k, v in meta.items():
        if k == 'rule':
            lines.append(f"📋 {v}")
        else:
            label = k.replace('_', ' ').upper()
            val = f"{v:.3f}" if isinstance(v, float) else str(v)
            lines.append(f"  {label}: {val}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def run_strategies(db):
    """Execute all 3 strategies with two-stage PREPARE → OPEN flow. Called every 5s."""
    global last_ema_time, last_bb_time, last_inst_time

    # (PREPARE logic removed — strategies enter OPEN directly)

    df_m1 = load_candles(db, SYMBOL, 500)
    if df_m1 is None or len(df_m1) < 50:
        print(f"[Analyzer] Insufficient data: {len(df_m1) if df_m1 is not None else 0} M1 candles. Need 50+.")
        return

    df_m5 = resample_m5(df_m1)
    if len(df_m5) < 20:
        print(f"[Analyzer] Only {len(df_m5)} M5 candles. Need 20+.")
        return

    # Attach indicators
    df_m1 = attach_indicators(df_m1)
    df_m5 = attach_indicators(df_m5)

    # Shift M5 by 1 to prevent lookahead (use previously completed M5 candle)
    df_m5_shifted = df_m5.shift(1)

    # Get current values from the LAST COMPLETED M1 candle (index -2)
    if len(df_m1) < 3:
        return

    idx = -2  # Last completed candle
    m1 = df_m1.iloc[idx]
    m1_close = m1['close']
    m1_high = m1['high']
    m1_low = m1['low']
    m1_rsi = m1['rsi']
    m1_ema21 = m1['ema21']
    m1_upper_bb = m1['upper_bb']
    m1_lower_bb = m1['lower_bb']
    m1_bb_sma = m1['bb_sma']
    m1_atr = m1['atr']

    # Find the M5 values for this M1 timestamp
    m1_time = df_m1.index[idx]
    m5_idx = df_m5_shifted.index.searchsorted(m1_time) - 1
    if m5_idx < 0 or m5_idx >= len(df_m5_shifted):
        return

    m5 = df_m5_shifted.iloc[m5_idx]

    # Check for NaN
    if pd.isna(m5['atr']) or pd.isna(m5['ema9']) or pd.isna(m1_rsi):
        return

    m5_atr = m5['atr']
    m5_ema9 = m5['ema9']
    m5_ema21 = m5['ema21']
    m5_ema50 = m5['ema50']
    m5_rsi = m5['rsi']
    m5_vol = m5['volume']
    m5_vol_sma = m5['vol_sma20'] if not pd.isna(m5['vol_sma20']) else 1
    m5_close = m5['close']
    m5_upper_bb = m5['upper_bb']
    m5_lower_bb = m5['lower_bb']

    # Global guard: M5 ATR must be alive
    if m5_atr < 0.05:
        return

    now = time.time()
    live_price = get_live_price(db) or m1_close

    # ==================== M5 TREND STRENGTH FILTER ====================
    # Compute M5 EMA slope to detect strong directional trends.
    # When the M5 trend is strongly one-directional, block ALL counter-trend entries
    # (including mean reversion) to avoid "catching a falling knife."
    m5_trend_bias = 'NEUTRAL'  # BULL_STRONG, BEAR_STRONG, or NEUTRAL

    # Check if we have enough M5 history for slope calculation
    if len(df_m5) >= 6:
        m5_prev = df_m5.iloc[-4]  # 3 M5 candles back (15 min)
        if not pd.isna(m5_prev['ema9']) and not pd.isna(m5_prev['ema21']):
            ema9_slope = m5_ema9 - m5_prev['ema9']
            ema21_slope = m5_ema21 - m5_prev['ema21']

            # Strong bearish: EMAs falling, price below both, EMA9 < EMA21
            if (ema9_slope < 0 and ema21_slope < 0 and
                m5_close < m5_ema9 and m5_close < m5_ema21 and
                m5_ema9 < m5_ema21):
                m5_trend_bias = 'BEAR_STRONG'

            # Strong bullish: EMAs rising, price above both, EMA9 > EMA21
            elif (ema9_slope > 0 and ema21_slope > 0 and
                  m5_close > m5_ema9 and m5_close > m5_ema21 and
                  m5_ema9 > m5_ema21):
                m5_trend_bias = 'BULL_STRONG'

    if m5_trend_bias != 'NEUTRAL':
        print(f"[Trend Filter] M5 bias: {m5_trend_bias} | EMA9: {m5_ema9:.2f} | EMA21: {m5_ema21:.2f} | Close: {m5_close:.2f}")


    def is_counter_trend(direction, signal_type):
        """Return True if this direction fights the strong M5 trend."""
        if m5_trend_bias == 'BEAR_STRONG' and direction == 'LONG':
            print(f"[Trend Filter] Blocked {signal_type} LONG — M5 strongly bearish")
            return True
        if m5_trend_bias == 'BULL_STRONG' and direction == 'SHORT':
            print(f"[Trend Filter] Blocked {signal_type} SHORT — M5 strongly bullish")
            return True
        return False

    # ==================== STRATEGY A: EMA Trend Pullback ====================
    if now - last_ema_time > COOLDOWN_SECS:
        m5_bull = m5_ema9 > m5_ema21 > m5_ema50
        m5_bear = m5_ema9 < m5_ema21 < m5_ema50

        # --- FULL conditions (OPEN) ---
        full_dir = None
        if m5_bull and m1_low <= m1_ema21 and m1_rsi <= 45:
            full_dir = 'LONG'
        elif m5_bear and m1_high >= m1_ema21 and m1_rsi >= 55:
            full_dir = 'SHORT'

        if full_dir and not is_counter_trend(full_dir, 'EMA_PULLBACK'):
            last_ema_time = now
            tp_dist = m5_atr * 3.0
            sl_dist = m5_atr * 1.2
            tp = live_price + tp_dist if full_dir == 'LONG' else live_price - tp_dist
            sl = live_price - sl_dist if full_dir == 'LONG' else live_price + sl_dist
            meta = {
                'rule': f"M5 EMA {'9>21>50 BULL' if m5_bull else '9<21<50 BEAR'}, M1 pullback to EMA21, M1 RSI reset",
                'm1_rsi': round(m1_rsi, 2), 'm1_ema21': round(m1_ema21, 3),
                'm5_ema9': round(m5_ema9, 3), 'm5_ema21': round(m5_ema21, 3),
                'm5_ema50': round(m5_ema50, 3), 'm5_atr': round(m5_atr, 3),
                'tp_mult': 3.0, 'sl_mult': 1.2,
            }
            trade_doc = {
                'symbol': SYMBOL, 'direction': full_dir, 'status': 'OPEN',
                'entryPrice': round(live_price, 3), 'tp': round(tp, 3), 'sl': round(sl, 3),
                'entryTime': int(now * 1000),
                'signalType': 'EMA_PULLBACK', 'meta': meta, 'lotSize': LOT_SIZE,
            }
            db.paper_trades.insert_one(trade_doc)
            msg = format_trade_message('NEW ORDER', full_dir, live_price, tp, sl, 'EMA_PULLBACK', meta)
            notify('TRADE_OPEN', f"📐 EMA Pullback {full_dir}", msg, trade_doc)
            print(f"[Strategy A] {full_dir} at {live_price:.3f} | TP: {tp:.3f} | SL: {sl:.3f}")

    # ==================== STRATEGY B: Bollinger Mean Reversion ====================
    if now - last_bb_time > COOLDOWN_SECS:
        full_dir = None
        bb_trigger = ''
        if m1_high > m1_upper_bb and m1_rsi >= 75:
            full_dir = 'SHORT'
            bb_trigger = f"M1 High ({m1_high:.3f}) > Upper BB ({m1_upper_bb:.3f}), RSI {m1_rsi:.1f} >= 75"
        elif m1_low < m1_lower_bb and m1_rsi <= 25:
            full_dir = 'LONG'
            bb_trigger = f"M1 Low ({m1_low:.3f}) < Lower BB ({m1_lower_bb:.3f}), RSI {m1_rsi:.1f} <= 25"

        if full_dir and not is_counter_trend(full_dir, 'BB_REVERSION'):
            last_bb_time = now
            tp_dist = m5_atr * 2.0
            sl_dist = m5_atr * 1.5
            tp = live_price + tp_dist if full_dir == 'LONG' else live_price - tp_dist
            sl = live_price - sl_dist if full_dir == 'LONG' else live_price + sl_dist
            meta = {
                'rule': bb_trigger, 'm1_rsi': round(m1_rsi, 2),
                'm1_upper_bb': round(m1_upper_bb, 3), 'm1_lower_bb': round(m1_lower_bb, 3),
                'm5_atr': round(m5_atr, 3), 'tp_mult': 2.0, 'sl_mult': 1.5,
            }
            trade_doc = {
                'symbol': SYMBOL, 'direction': full_dir, 'status': 'OPEN',
                'entryPrice': round(live_price, 3), 'tp': round(tp, 3), 'sl': round(sl, 3),
                'entryTime': int(now * 1000),
                'signalType': 'BB_REVERSION', 'meta': meta, 'lotSize': LOT_SIZE,
            }
            db.paper_trades.insert_one(trade_doc)
            msg = format_trade_message('NEW ORDER', full_dir, live_price, tp, sl, 'BB_REVERSION', meta)
            notify('TRADE_OPEN', f"📊 BB Reversion {full_dir}", msg, trade_doc)
            print(f"[Strategy B] {full_dir} at {live_price:.3f} | TP: {tp:.3f} | SL: {sl:.3f}")

    # ==================== STRATEGY C: Institutional Breakout ====================
    if now - last_inst_time > COOLDOWN_SECS:
        vol_ratio = (m5_vol / m5_vol_sma) if m5_vol_sma > 0 else 0

        full_dir = None
        if vol_ratio > 2.0:
            if m5_close > m5_upper_bb and m1_low <= m5_ema9:
                full_dir = 'LONG'
            elif m5_close < m5_lower_bb and m1_high >= m5_ema9:
                full_dir = 'SHORT'

        if full_dir and not is_counter_trend(full_dir, 'INST_BREAKOUT'):
            last_inst_time = now
            tp_dist = m5_atr * 4.0
            sl_dist = m5_atr * 1.0
            tp = live_price + tp_dist if full_dir == 'LONG' else live_price - tp_dist
            sl = live_price - sl_dist if full_dir == 'LONG' else live_price + sl_dist
            meta = {
                'rule': f"M5 Vol surge ({vol_ratio:.1f}x > 2.0x), M5 close {'above Upper BB' if full_dir == 'LONG' else 'below Lower BB'}, M1 pullback to M5 EMA9",
                'm5_volume': round(m5_vol, 0), 'm5_vol_ratio': round(vol_ratio, 2),
                'm5_close': round(m5_close, 3), 'm5_ema9': round(m5_ema9, 3),
                'm5_atr': round(m5_atr, 3), 'tp_mult': 4.0, 'sl_mult': 1.0,
            }
            trade_doc = {
                'symbol': SYMBOL, 'direction': full_dir, 'status': 'OPEN',
                'entryPrice': round(live_price, 3), 'tp': round(tp, 3), 'sl': round(sl, 3),
                'entryTime': int(now * 1000),
                'signalType': 'INST_BREAKOUT', 'meta': meta, 'lotSize': LOT_SIZE,
            }
            db.paper_trades.insert_one(trade_doc)
            msg = format_trade_message('NEW ORDER', full_dir, live_price, tp, sl, 'INST_BREAKOUT', meta)
            notify('TRADE_OPEN', f"🏦 Inst. Breakout {full_dir}", msg, trade_doc)
            print(f"[Strategy C] {full_dir} at {live_price:.3f} | TP: {tp:.3f} | SL: {sl:.3f}")

# ===================== REAL-TIME BROADCAST =====================

def get_frontend_payload(db):
    """Generate the full data payload for the frontend (trades, livePrice, indicators)."""
    trades = list(db.paper_trades.find().sort('entryTime', -1).limit(200))
    for t in trades:
        t['_id'] = str(t['_id'])
        # Convert any remaining ObjectId/datetime fields
        for k, v in t.items():
            if hasattr(v, '__str__') and type(v).__name__ in ('ObjectId', 'datetime'):
                t[k] = str(v)
                
    live = get_live_price(db)
    
    # Compute current indicators for live display
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
    """Broadcast the full trade state via notification WS for real-time frontend updates."""
    try:
        payload = get_frontend_payload(db)
        payload['type'] = 'TRADES_UPDATE'

        requests.post(NOTIFICATION_URL, json=payload, timeout=2)
    except Exception:
        pass  # Non-critical — frontend falls back to polling


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
        else:  # SHORT
            if live_price <= tp:
                close_reason = 'TAKE_PROFIT'
            elif live_price >= sl:
                close_reason = 'STOP_LOSS'

        if close_reason:

            # Correct PnL
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

            # Get overall stats
            all_closed = list(db.paper_trades.find({'status': 'CLOSED'}))
            total_pnl = sum(t.get('pnl', 0) for t in all_closed)
            wins = sum(1 for t in all_closed if t.get('pnl', 0) > 0)
            wr = (wins / len(all_closed) * 100) if all_closed else 0

            emoji = '✅' if close_reason == 'TAKE_PROFIT' else '❌'
            strat = trade.get('signalType', '?')
            hold_mins = (time.time() * 1000 - trade.get('entryTime', 0)) / 60000

            msg = (
                f"{emoji} <b>ORDER CLOSED | {direction} | {close_reason}</b>\n"
                f"Strategy: <b>{strat}</b>\n"
                f"Entry: ${entry:.2f} → Exit: ${live_price:.2f}\n"
                f"PnL: <b>{'+'if pnl>0 else ''}${pnl:.2f}</b> (0.01 lot)\n"
                f"Hold: {hold_mins:.0f} min\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 Overall: ${total_pnl:.2f} | WR: {wr:.1f}% | Trades: {len(all_closed)}"
            )
            notify('TRADE_CLOSE', f"{emoji} {strat} {close_reason}", msg)
            print(f"[Monitor] {emoji} {direction} {close_reason} | Entry: {entry:.2f} | Exit: {live_price:.2f} | PnL: ${pnl:.2f}")

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
    """Start the Express-like REST API using Flask-style threading."""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import json as json_mod
    from urllib.parse import urlparse, parse_qs

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Suppress noisy logs

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
                closed = list(db.paper_trades.find({'status': 'CLOSED'}))
                wins = sum(1 for t in closed if t.get('pnl', 0) > 0)
                losses = sum(1 for t in closed if t.get('pnl', 0) < 0)
                total_pnl = sum(t.get('pnl', 0) for t in closed)
                wr = (wins / len(closed) * 100) if closed else 0
                open_count = db.paper_trades.count_documents({'status': 'OPEN'})
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
    STRATEGY_INTERVAL = 5  # Run strategies every 5 seconds
    HEARTBEAT_INTERVAL = 30  # Status print every 30s

    while True:
        try:
            now = time.time()

            # Monitor TP/SL every second, then broadcast state
            monitor_trades(db)
            broadcast_trades(db)

            # Run strategies every 5 seconds
            if now - last_strategy_run >= STRATEGY_INTERVAL:
                last_strategy_run = now
                run_strategies(db)

            # Heartbeat: confirm analyzer is alive
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
