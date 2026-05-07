"""
Backtester Server — Historical Tick Replay + Strategy Analysis
================================================================
FastAPI + WebSocket server that:
  1. Reads CSV tick data (histdata.com format)
  2. Aggregates ticks into M1/M5 candles
  3. Runs strategy.py analyzer on each completed candle
  4. Tracks trades with TP/SL
  5. Streams everything to frontend via WebSocket

CSV format: 20250101 180000455,2625.098000,2626.592000,0
Fields: datetime_ms, bid, ask, volume (no header)
"""

import os
import sys
import glob
import json
import time
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# Add analyzer to path so we can import strategy.py
ANALYZER_DIR = os.path.join(os.path.dirname(__file__), '..', 'analyzer')
sys.path.insert(0, os.path.abspath(ANALYZER_DIR))

from strategy import (
    attach_indicators, resample_m5, resample_ohlcv,
    evaluate_strategies,
    MarketSnapshot, Signal, CooldownState,
    COOLDOWN_SECS,
)

# ===================== CONFIG =====================

CSV_DIR = os.environ.get('CSV_DIR', os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'csv'))
SPREAD_OFFSET = float(os.environ.get('SPREAD_OFFSET', '0.5'))

# R:R Slots (same as production)
RR_SLOTS = [
    {'name': 'A', 'tp_mult': 1.5, 'sl_mult': 1.5, 'label': '1.5:1.5'},
    {'name': 'B', 'tp_mult': 3.0, 'sl_mult': 1.0, 'label': '3:1'},
    {'name': 'C', 'tp_mult': 1.0, 'sl_mult': 1.0, 'label': '1:1'},
]

# ===================== CSV READER =====================

def parse_tick_line(line: str):
    """Parse a single CSV tick line into (datetime, bid, ask, volume)."""
    try:
        parts = line.strip().split(',')
        if len(parts) < 3:
            return None
        dt_str = parts[0]
        bid = float(parts[1])
        ask = float(parts[2])
        vol = float(parts[3]) if len(parts) > 3 else 0

        # Format: "20250101 180000455" → YYYYMMDD HHMMSSmmm
        date_part = dt_str[:8]
        time_part = dt_str[9:]  # Skip space

        year = int(date_part[:4])
        month = int(date_part[4:6])
        day = int(date_part[6:8])
        hour = int(time_part[:2])
        minute = int(time_part[2:4])
        second = int(time_part[4:6])
        ms = int(time_part[6:]) if len(time_part) > 6 else 0

        dt = datetime(year, month, day, hour, minute, second, ms * 1000, tzinfo=timezone.utc)
        mid = (bid + ask) / 2
        return dt, mid, bid, ask, vol
    except Exception:
        return None


def get_csv_files():
    """Get sorted list of CSV files."""
    pattern = os.path.join(CSV_DIR, 'DAT_ASCII_XAUUSD_T_*.csv')
    files = sorted(glob.glob(pattern))
    return files


def get_available_dates():
    """Scan CSV filenames to get available months."""
    files = get_csv_files()
    months = []
    for f in files:
        name = os.path.basename(f)
        # DAT_ASCII_XAUUSD_T_202501.csv → 202501
        ym = name.split('_')[-1].replace('.csv', '')
        year = int(ym[:4])
        month = int(ym[4:6])
        months.append({'year': year, 'month': month, 'file': f})
    return months


def tick_generator(start_date: datetime, end_date: datetime = None):
    """Generator that yields ticks from CSV files starting at start_date."""
    files = get_csv_files()
    for filepath in files:
        name = os.path.basename(filepath)
        ym = name.split('_')[-1].replace('.csv', '')
        file_year = int(ym[:4])
        file_month = int(ym[4:6])

        # Skip files before start month
        file_start = datetime(file_year, file_month, 1, tzinfo=timezone.utc)
        if file_month == 12:
            file_end = datetime(file_year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            file_end = datetime(file_year, file_month + 1, 1, tzinfo=timezone.utc)

        if file_end <= start_date:
            continue
        if end_date and file_start > end_date:
            break

        with open(filepath, 'r') as f:
            for line in f:
                tick = parse_tick_line(line)
                if tick is None:
                    continue
                dt = tick[0]
                if dt < start_date:
                    continue
                if end_date and dt > end_date:
                    return
                # Skip weekends (Sat 00:00 → Sun 22:00 UTC)
                wd = dt.weekday()
                if wd == 5:  # Saturday
                    continue
                if wd == 6 and dt.hour < 22:  # Sunday before 22:00
                    continue
                yield tick


# ===================== CANDLE BUILDER =====================

class CandleBuilder:
    """Aggregates ticks into M1 candles and maintains indicator state."""

    def __init__(self):
        self.current_bucket = None
        self.current_candle = None
        self.candles = []  # List of completed M1 candle dicts
        self.df_m1 = pd.DataFrame()
        self.df_m5 = pd.DataFrame()
        self.max_candles = 500  # Rolling window for indicator calculation
        self.candle_count = 0
        self.indicators_dirty = False

    def process_tick(self, dt, price, bid, ask, vol):
        """Process a tick. Returns completed candle dict if M1 bar closed, else None."""
        bucket = dt.replace(second=0, microsecond=0)

        if self.current_bucket is None or bucket != self.current_bucket:
            completed = None
            if self.current_candle is not None:
                completed = dict(self.current_candle)
                self.candles.append(completed)
                self.candle_count += 1
                # Keep rolling window
                if len(self.candles) > self.max_candles:
                    self.candles = self.candles[-self.max_candles:]
                # Mark dirty on M5 boundaries (minute 0, 5, 10, ...)
                if bucket.minute % 5 == 0:
                    self.indicators_dirty = True

            self.current_bucket = bucket
            self.current_candle = {
                'timestamp': bucket,
                'open': price,
                'high': price,
                'low': price,
                'close': price,
                'volume': 1,
            }
            return completed
        else:
            self.current_candle['high'] = max(self.current_candle['high'], price)
            self.current_candle['low'] = min(self.current_candle['low'], price)
            self.current_candle['close'] = price
            self.current_candle['volume'] += 1
            return None

    def rebuild_indicators(self, force=False):
        """Rebuild M1 and M5 DataFrames with indicators.
        
        Only rebuilds when an M5 bar boundary is crossed (or forced).
        """
        if not force and not self.indicators_dirty:
            return len(self.df_m5) >= 3
        
        if len(self.candles) < 60:
            return False

        self.indicators_dirty = False
        self.df_m1 = pd.DataFrame(self.candles)
        self.df_m1['timestamp'] = pd.to_datetime(self.df_m1['timestamp'], utc=True)
        self.df_m1.set_index('timestamp', inplace=True)
        self.df_m1.sort_index(inplace=True)
        self.df_m1 = attach_indicators(self.df_m1)

        self.df_m5 = resample_m5(self.df_m1)
        self.df_m5 = attach_indicators(self.df_m5)

        return len(self.df_m5) >= 3

    def build_snapshot(self, live_price=None):
        """Build a MarketSnapshot from current state."""
        if len(self.df_m1) < 2 or len(self.df_m5) < 3:
            return None

        m1 = self.df_m1.iloc[-1]
        m5 = self.df_m5.iloc[-2]  # Last COMPLETED M5 bar
        m5_prev = self.df_m5.iloc[-4] if len(self.df_m5) >= 5 else None

        snap = MarketSnapshot(
            m1_close=float(m1['close']),
            m1_high=float(m1['high']),
            m1_low=float(m1['low']),
            m1_rsi=float(m1['rsi']) if not np.isnan(m1['rsi']) else 50.0,
            m1_ema21=float(m1['ema21']),
            m1_upper_bb=float(m1['upper_bb']) if not np.isnan(m1['upper_bb']) else float(m1['close']),
            m1_lower_bb=float(m1['lower_bb']) if not np.isnan(m1['lower_bb']) else float(m1['close']),
            m1_bb_sma=float(m1['bb_sma']) if not np.isnan(m1['bb_sma']) else float(m1['close']),
            m1_atr=float(m1['atr']) if not np.isnan(m1['atr']) else 1.0,
            m5_atr=float(m5['atr']) if not np.isnan(m5['atr']) else 1.0,
            m5_ema9=float(m5['ema9']),
            m5_ema21=float(m5['ema21']),
            m5_ema50=float(m5['ema50']),
            m5_rsi=float(m5['rsi']) if not np.isnan(m5['rsi']) else 50.0,
            m5_close=float(m5['close']),
            m5_high=float(m5['high']),
            m5_low=float(m5['low']),
            m5_upper_bb=float(m5['upper_bb']) if not np.isnan(m5['upper_bb']) else float(m5['close']),
            m5_lower_bb=float(m5['lower_bb']) if not np.isnan(m5['lower_bb']) else float(m5['close']),
            m5_volume=float(m5['volume']),
            m5_vol_sma20=float(m5['vol_sma20']) if not np.isnan(m5['vol_sma20']) else 1.0,
            live_price=live_price,
        )

        if m5_prev is not None:
            snap.m5_ema9_prev = float(m5_prev['ema9'])
            snap.m5_ema21_prev = float(m5_prev['ema21'])
            snap.has_slope_data = True

        return snap


# ===================== TRADE MANAGER =====================

class TradeManager:
    """Tracks open and closed trades across all R:R slots."""

    def __init__(self, mode='normal'):
        self.mode = mode
        self.open_trades = []
        self.pending_trades = []
        self.closed_trades = []
        self.trade_id = 0
        self.is_reverse_mode = (mode == 'reverse')
        self.consecutive_losses = 0
        
    def update_auto_mode(self, spread: float):
        if self.mode != 'auto':
            return
        if spread > 0.70 and self.is_reverse_mode:
            self.is_reverse_mode = False
        elif spread < 0.30 and not self.is_reverse_mode:
            self.is_reverse_mode = True

    def open_trade(self, signal: Signal, slot: dict, timestamp: datetime):
        self.trade_id += 1
        atr = signal.meta.get('m5_atr', 1.0)
        tp_dist = atr * slot['tp_mult'] - SPREAD_OFFSET
        sl_dist = atr * slot['sl_mult'] + SPREAD_OFFSET

        # Limit price logic based on mode
        pullback_pct = 0.20 if self.is_reverse_mode else 0.10
        
        # Grid Spacing: Check for existing OPEN or PENDING trades in this slot + direction
        existing_trades = [t for t in self.open_trades + self.pending_trades 
                           if t['slot'] == slot['name'] and t['direction'] == signal.direction]
                           
        if existing_trades:
            if signal.direction == 'LONG':
                anchor_trade = min(existing_trades, key=lambda t: t.get('entry_price') if t['status'] == 'OPEN' else t.get('limit_price'))
                base_price = anchor_trade.get('entry_price') if anchor_trade['status'] == 'OPEN' else anchor_trade.get('limit_price')
                limit_price = base_price - (pullback_pct * atr)
            else: # SHORT
                anchor_trade = max(existing_trades, key=lambda t: t.get('entry_price') if t['status'] == 'OPEN' else t.get('limit_price'))
                base_price = anchor_trade.get('entry_price') if anchor_trade['status'] == 'OPEN' else anchor_trade.get('limit_price')
                limit_price = base_price + (pullback_pct * atr)
        else:
            if signal.direction == 'LONG':
                limit_price = signal.entry_price - (pullback_pct * atr)
            else:
                limit_price = signal.entry_price + (pullback_pct * atr)

        trade = {
            'id': self.trade_id,
            'slot': slot['name'],
            'slot_label': slot['label'],
            'strategy': signal.strategy,
            'direction': signal.direction,
            'signal_price': round(signal.entry_price, 2),
            'limit_price': round(limit_price, 2),
            'tp_mult': slot['tp_mult'],
            'sl_mult': slot['sl_mult'],
            'tp_dist': round(tp_dist, 2),
            'sl_dist': round(sl_dist, 2),
            'atr': round(atr, 2),
            'create_time': timestamp.isoformat(),
            'create_ts': timestamp.timestamp(),
            'status': 'PENDING',
            'meta': signal.meta,
        }
        self.pending_trades.append(trade)
        return trade

    def check_tp_sl(self, price: float, timestamp: datetime):
        now_ts = timestamp.timestamp()
        
        # 1. Evaluate PENDING trades
        still_pending = []
        for pt in self.pending_trades:
            if now_ts - pt['create_ts'] > 900:  # 15 minutes expired
                continue
                
            triggered = False
            if pt['direction'] == 'LONG' and price <= pt['limit_price']:
                triggered = True
            elif pt['direction'] == 'SHORT' and price >= pt['limit_price']:
                triggered = True
                
            if triggered:
                exec_price = price
                tp_dist = pt['atr'] * pt['tp_mult'] - SPREAD_OFFSET
                sl_dist = pt['atr'] * pt['sl_mult'] + SPREAD_OFFSET
                
                tp = exec_price + tp_dist if pt['direction'] == 'LONG' else exec_price - tp_dist
                sl = exec_price - sl_dist if pt['direction'] == 'LONG' else exec_price + sl_dist
                
                pt['entry_price'] = round(exec_price, 2)
                pt['tp'] = round(tp, 2)
                pt['sl'] = round(sl, 2)
                pt['entry_time'] = timestamp.isoformat()
                pt['entry_ts'] = now_ts
                pt['status'] = 'OPEN'
                self.open_trades.append(pt)
            else:
                still_pending.append(pt)
        self.pending_trades = still_pending

        """Check all open trades for TP/SL hits. Returns list of closed trades."""
        closed = []
        still_open = []

        for trade in self.open_trades:
            hit = False
            if trade['direction'] == 'LONG':
                if price >= trade['tp']:
                    trade['status'] = 'WIN'
                    trade['exit_price'] = trade['tp']
                    trade['pnl'] = trade['tp'] - trade['entry_price']
                    hit = True
                elif price <= trade['sl']:
                    trade['status'] = 'LOSS'
                    trade['exit_price'] = trade['sl']
                    trade['pnl'] = trade['sl'] - trade['entry_price']
                    hit = True
            else:  # SHORT
                if price <= trade['tp']:
                    trade['status'] = 'WIN'
                    trade['exit_price'] = trade['tp']
                    trade['pnl'] = trade['entry_price'] - trade['tp']
                    hit = True
                elif price >= trade['sl']:
                    trade['status'] = 'LOSS'
                    trade['exit_price'] = trade['sl']
                    trade['pnl'] = trade['entry_price'] - trade['sl']
                    hit = True

            if hit:
                trade['exit_time'] = timestamp.isoformat()
                self.closed_trades.append(trade)
                closed.append(trade)

                if trade['status'] == 'WIN':
                    self.consecutive_losses = 0
                elif trade['status'] == 'LOSS':
                    self.consecutive_losses += 1
                    if self.mode == 'reverse' and self.consecutive_losses >= 8:
                        self.is_reverse_mode = not self.is_reverse_mode
                        self.consecutive_losses = 0
                        mode_str = 'REVERSE' if self.is_reverse_mode else 'NORMAL'
                        print(f"🔄 Switched to {mode_str} MODE (8 consecutive losses) at {timestamp}")

            else:
                still_open.append(trade)

        self.open_trades = still_open
        return closed

    def get_stats(self):
        wins = sum(1 for t in self.closed_trades if t['status'] == 'WIN')
        losses = sum(1 for t in self.closed_trades if t['status'] == 'LOSS')
        total = wins + losses
        pnl = sum(t['pnl'] for t in self.closed_trades)
        return {
            'total': total,
            'open': len(self.open_trades),
            'wins': wins,
            'losses': losses,
            'winRate': round(wins / total * 100, 1) if total > 0 else 0,
            'pnl': round(pnl, 2),
        }


# ===================== FASTAPI APP =====================

app = FastAPI(title="XAU/USD Backtester")

# Serve static files
STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')
app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')


@app.get('/')
async def index():
    return FileResponse(os.path.join(STATIC_DIR, 'index.html'))


@app.get('/api/dates')
async def get_dates():
    months = get_available_dates()
    return {'months': months, 'csv_dir': CSV_DIR}


# ===================== DRY RUN (Background Job) =====================

import threading

# Shared state for the background dry run job
_dryrun_state = {
    'running': False,
    'progress': {},
    'result': None,
}


def run_dryrun(start_date: str, end_date: str = None, mode: str = 'normal', rr: str = 'ALL'):
    """Background thread: process all ticks and update shared state."""
    import time as _time
    t0 = _time.time()

    state = _dryrun_state
    state['running'] = True
    state['result'] = None

    start_date_obj = datetime.strptime(start_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    end_date_obj = datetime.strptime(end_date, '%Y-%m-%d').replace(tzinfo=timezone.utc) if end_date else None
    warmup_date = start_date_obj - timedelta(hours=6)

    print(f'[DryRun] Start={start_date}, End={end_date or "ALL"}, Mode={mode}, RR={rr}')

    if rr == 'ALL':
        active_slots = RR_SLOTS
    else:
        try:
            tp_str, sl_str = rr.replace(',', '.').split(':')
            active_slots = [{'name': 'CUSTOM', 'label': rr, 'tp_mult': float(tp_str), 'sl_mult': float(sl_str)}]
        except Exception as e:
            print(f"Failed to parse custom RR: {e}")
            active_slots = RR_SLOTS

    builder = CandleBuilder()
    trade_mgr = TradeManager(mode=mode)
    cooldowns = {s['name']: CooldownState() for s in active_slots}

    tick_count = 0
    candle_count = 0
    warmup_done = False
    current_date = ''

    for tick in tick_generator(warmup_date, end_date_obj):
        # Check if cancelled
        if not state['running']:
            print('[DryRun] Cancelled')
            return

        dt, price, bid, ask, vol = tick
        tick_count += 1

        # Update progress every 100K ticks
        if tick_count % 100000 == 0:
            current_date = dt.strftime('%Y-%m-%d %H:%M')
            stats = trade_mgr.get_stats()
            state['progress'] = {
                'ticks': tick_count,
                'candles': candle_count,
                'current_date': current_date,
                'elapsed': round(_time.time() - t0, 1),
                'trades': stats['total'],
                'wins': stats['wins'],
                'losses': stats['losses'],
                'pnl': stats['pnl'],
                'winRate': stats['winRate'],
            }

        trade_mgr.check_tp_sl(price, dt)

        completed = builder.process_tick(dt, price, bid, ask, vol)
        if completed is not None:
            candle_count += 1
            if builder.rebuild_indicators():
                snap = builder.build_snapshot(live_price=price)
                if snap is not None:
                    # Apply auto-switching hysteresis
                    trade_mgr.update_auto_mode(snap.m5_ema_spread_smooth)
                    
                    bar_ts = completed['timestamp'].timestamp()
                    for slot in active_slots:
                        cd = cooldowns[slot['name']]
                        signals = evaluate_strategies(snap, cd, bar_ts, SPREAD_OFFSET)
                        for sig in signals:
                            if trade_mgr.is_reverse_mode:
                                sig.direction = 'SHORT' if sig.direction == 'LONG' else 'LONG'
                                
                            # --- GUARD 4: Grid Spacing (Minimum 15% ATR advantage) ---
                            slot_open_trades = [t for t in trade_mgr.open_trades if t['slot'] == slot['name'] and t['direction'] == sig.direction]
                            if slot_open_trades:
                                nearest = min(slot_open_trades, key=lambda t: abs(t['entry_price'] - sig.entry_price))
                                if sig.direction == 'LONG':
                                    advantage = nearest['entry_price'] - sig.entry_price
                                else:
                                    advantage = sig.entry_price - nearest['entry_price']
                                
                                req_adv = 0.15 * snap.m5_atr
                                if advantage <= req_adv:
                                    continue
                                    
                            trade_mgr.open_trade(sig, slot, completed['timestamp'])
            if not warmup_done and dt >= start_date_obj:
                warmup_done = True

    elapsed = round(_time.time() - t0, 2)
    stats = trade_mgr.get_stats()

    all_trades = trade_mgr.closed_trades + [
        {**t, 'status': 'STILL_OPEN'} for t in trade_mgr.open_trades
    ]
    all_trades.sort(key=lambda t: t.get('entry_ts', 0))

    daily_pnl = {}
    for t in trade_mgr.closed_trades:
        day = t['entry_time'][:10]
        if day not in daily_pnl:
            daily_pnl[day] = {'pnl': 0, 'wins': 0, 'losses': 0, 'total': 0}
        daily_pnl[day]['pnl'] += t['pnl']
        daily_pnl[day]['total'] += 1
        if t['status'] == 'WIN':
            daily_pnl[day]['wins'] += 1
        else:
            daily_pnl[day]['losses'] += 1

    for d in daily_pnl:
        daily_pnl[d]['pnl'] = round(daily_pnl[d]['pnl'], 2)

    state['result'] = {
        'stats': stats,
        'elapsed_seconds': elapsed,
        'ticks': tick_count,
        'candles': candle_count,
        'trades': all_trades,
        'daily_pnl': daily_pnl,
    }
    state['running'] = False

    print(f'[DryRun] Done in {elapsed}s: {tick_count:,} ticks, {candle_count:,} candles, {stats["total"]} trades, P/L=${stats["pnl"]}')


@app.post('/api/dryrun')
def start_dry_run(start: str = Query('2025-01-05'), end: str = Query(None), mode: str = Query('normal'), rr: str = Query('ALL')):
    """Start a background dry run job."""
    if _dryrun_state['running']:
        return {'error': 'A dry run is already in progress. Cancel it first.'}

    _dryrun_state['progress'] = {'ticks': 0, 'candles': 0, 'current_date': '', 'elapsed': 0, 'trades': 0, 'pnl': 0, 'winRate': 0, 'wins': 0, 'losses': 0}
    _dryrun_state['result'] = None

    thread = threading.Thread(target=run_dryrun, args=(start, end, mode, rr), daemon=True)
    thread.start()

    return {'status': 'started', 'start': start, 'end': end or 'ALL'}


@app.get('/api/dryrun/status')
async def dryrun_status():
    """Poll progress of the running dry run."""
    return {
        'running': _dryrun_state['running'],
        'progress': _dryrun_state['progress'],
        'done': _dryrun_state['result'] is not None,
    }


@app.get('/api/dryrun/result')
async def dryrun_result():
    """Get final result after dry run completes."""
    if _dryrun_state['result'] is None:
        return {'error': 'No result available. Run a dry run first.'}
    return _dryrun_state['result']


@app.post('/api/dryrun/cancel')
async def cancel_dryrun():
    """Cancel a running dry run."""
    _dryrun_state['running'] = False
    return {'status': 'cancelled'}


@app.websocket('/ws/replay')
async def replay_ws(ws: WebSocket):
    await ws.accept()
    print('[WS] Client connected')

    try:
        # Wait for start command
        msg = await ws.receive_json()
        if msg.get('action') != 'start':
            await ws.send_json({'type': 'error', 'data': 'Expected start action'})
            return

        start_str = msg.get('date', '2025-01-05')
        mode_str = msg.get('mode', 'normal')
        rr_str = msg.get('rr', 'ALL')
        speed = msg.get('speed', 100)
        tf = msg.get('tf', 'M5')

        # Parse start date
        start_date = datetime.strptime(start_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        # Start 2 hours earlier for indicator warmup
        warmup_date = start_date - timedelta(hours=6)

        print(f'[Replay] Starting from {start_str}, speed={speed}x, tf={tf}, mode={mode_str}, rr={rr_str}')

        if rr_str == 'ALL':
            active_slots = RR_SLOTS
        else:
            try:
                tp_str, sl_str = rr_str.replace(',', '.').split(':')
                active_slots = [{'name': 'CUSTOM', 'label': rr_str, 'tp_mult': float(tp_str), 'sl_mult': float(sl_str)}]
            except Exception as e:
                print(f"Failed to parse custom RR: {e}")
                active_slots = RR_SLOTS

        builder = CandleBuilder()
        trade_mgr = TradeManager(mode=mode_str)
        cooldowns = {s['name']: CooldownState() for s in active_slots}

        tick_count = 0
        candle_count = 0
        last_send_time = 0
        warmup_done = False
        paused = False

        # Resample frequency for display
        tf_freq = {'M1': '1min', 'M5': '5min', 'M15': '15min'}.get(tf, '5min')

        for tick in tick_generator(warmup_date):
            # Check for control messages (non-blocking)
            try:
                ctrl = await asyncio.wait_for(ws.receive_json(), timeout=0.0001)
                if ctrl.get('action') == 'speed':
                    speed = ctrl.get('speed', speed)
                    print(f'[Replay] Speed changed to {speed}x')
                elif ctrl.get('action') == 'pause':
                    paused = True
                    print('[Replay] Paused')
                elif ctrl.get('action') == 'resume':
                    paused = False
                    print('[Replay] Resumed')
                elif ctrl.get('action') == 'stop':
                    print('[Replay] Stopped by client')
                    break
            except (asyncio.TimeoutError, Exception):
                pass

            while paused:
                try:
                    ctrl = await asyncio.wait_for(ws.receive_json(), timeout=0.5)
                    if ctrl.get('action') == 'resume':
                        paused = False
                    elif ctrl.get('action') == 'stop':
                        return
                except (asyncio.TimeoutError, Exception):
                    pass

            dt, price, bid, ask, vol = tick
            tick_count += 1

            # Check TP/SL on every tick
            closed_trades = trade_mgr.check_tp_sl(price, dt)
            for ct in closed_trades:
                if warmup_done:
                    await ws.send_json({'type': 'trade_close', 'data': ct})

            # Aggregate into M1 candle
            completed = builder.process_tick(dt, price, bid, ask, vol)

            if completed is not None:
                candle_count += 1

                # Rebuild indicators
                if builder.rebuild_indicators():
                    snap = builder.build_snapshot(live_price=price)

                    if snap is not None:
                        bar_ts = completed['timestamp'].timestamp()

                        # Run strategy for each slot
                        for slot in active_slots:
                            cd = cooldowns[slot['name']]
                            saved_cd = (cd.last_ema, cd.last_bb, cd.last_inst)
                            signals = evaluate_strategies(snap, cd, bar_ts, SPREAD_OFFSET)

                            for sig in signals:
                                if trade_mgr.is_reverse_mode:
                                    sig.direction = 'SHORT' if sig.direction == 'LONG' else 'LONG'
                                trade = trade_mgr.open_trade(sig, slot, completed['timestamp'])
                                if warmup_done:
                                    await ws.send_json({'type': 'trade_open', 'data': trade})

                # Send candle to frontend after warmup
                if not warmup_done and dt >= start_date:
                    warmup_done = True
                    print(f'[Replay] Warmup complete. {candle_count} candles processed.')
                    # Send initial stats
                    await ws.send_json({'type': 'warmup_done', 'data': {
                        'candles': candle_count,
                        'start': start_str,
                    }})

                if warmup_done:
                    # Build display candle based on selected timeframe
                    candle_data = {
                        'time': int(completed['timestamp'].timestamp()),
                        'open': completed['open'],
                        'high': completed['high'],
                        'low': completed['low'],
                        'close': completed['close'],
                        'volume': completed['volume'],
                    }

                    # Add EMA + BB data from M5
                    if len(builder.df_m5) >= 2:
                        m5 = builder.df_m5.iloc[-2]
                        candle_data['ema9'] = round(float(m5['ema9']), 3)
                        candle_data['ema21'] = round(float(m5['ema21']), 3)
                        candle_data['ema50'] = round(float(m5['ema50']), 3)
                        if not np.isnan(m5['upper_bb']):
                            candle_data['upper_bb'] = round(float(m5['upper_bb']), 3)
                            candle_data['lower_bb'] = round(float(m5['lower_bb']), 3)

                    await ws.send_json({'type': 'candle', 'data': candle_data})

                    # Send current time
                    await ws.send_json({'type': 'tick_time', 'data': {
                        'time': dt.strftime('%Y-%m-%d %H:%M:%S'),
                        'price': round(price, 2),
                    }})

                    # Send stats periodically
                    if candle_count % 5 == 0:
                        await ws.send_json({'type': 'stats', 'data': trade_mgr.get_stats()})

            # Speed control
            if speed != 'MAX' and warmup_done and tick_count % max(1, int(speed)) == 0:
                await asyncio.sleep(0.001)

        # Final stats
        await ws.send_json({'type': 'stats', 'data': trade_mgr.get_stats()})
        await ws.send_json({'type': 'done', 'data': {
            'ticks': tick_count,
            'candles': candle_count,
            'trades': trade_mgr.get_stats(),
        }})
        print(f'[Replay] Complete: {tick_count} ticks, {candle_count} candles')

    except WebSocketDisconnect:
        print('[WS] Client disconnected')
    except Exception as e:
        print(f'[WS] Error: {e}')
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=4005)
