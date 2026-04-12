"""
XAU/USD Unified Dry-Run / Backtest
===================================
Replays the EXACT strategy logic from strategy.py against historical data.

Data sources:
  --csv FILE      Run against historical tick CSV (e.g. DAT_ASCII_XAUUSD_T_202603.csv)
  --csv all       Run against all available CSV datasets
  --csv compare   Run with/without trend filter comparison
  --mongo         Run against M1 candles in MongoDB (paper trading data)

Uses the identical evaluate_strategies() function as the live analyzer,
guaranteeing parity between backtest and production.
"""

import os
import sys
import time
import json
import argparse

import numpy as np
import pandas as pd
from dotenv import load_dotenv

# Import the single source of truth
from strategy import (
    attach_indicators, resample_m5, resample_ohlcv, evaluate_strategies,
    MarketSnapshot, CooldownState, POSITION_OZ, COOLDOWN_SECS,
)

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'data')


# ===================== DATA LOADING =====================

def load_from_csv(csv_path, context_tf='5min'):
    """Load tick-level CSV, resample to M1 and context TF, cache M1 as pickle."""
    dataset_id = None
    for tag in ['202601', '202602', '202603']:
        if tag in csv_path:
            dataset_id = tag
            break
    if dataset_id is None:
        dataset_id = os.path.splitext(os.path.basename(csv_path))[0]

    m1_path = os.path.join(DATA_DIR, f'xau_m1_cache_{dataset_id}.pkl')

    if os.path.exists(m1_path):
        print(f"Loading cached M1 from {m1_path}...")
        df_m1 = pd.read_pickle(m1_path)
    else:
        if not os.path.exists(csv_path):
            print(f"Error: {csv_path} not found.")
            return None, None, None

        print(f"Reading tick data from {csv_path}...")
        start = time.time()
        df_tick = pd.read_csv(csv_path, header=None, names=['time_str', 'bid', 'ask', 'volume'])
        df_tick['time_str'] = df_tick['time_str'] + '000'
        df_tick['time'] = pd.to_datetime(df_tick['time_str'], format='%Y%m%d %H%M%S%f')
        df_tick['price'] = (df_tick['bid'] + df_tick['ask']) / 2.0
        df_tick.set_index('time', inplace=True)
        df_tick.sort_index(inplace=True)
        print(f"Parsed {len(df_tick)} ticks in {time.time() - start:.1f}s. Resampling...")

        df_m1 = df_tick.resample('1min').agg({
            'price': ['first', 'max', 'min', 'last'],
            'volume': 'sum'
        })
        df_m1.columns = ['open', 'high', 'low', 'close', 'volume']
        df_m1.dropna(inplace=True)

        df_m1.to_pickle(m1_path)
        print("Cached M1.")

    # Resample to the requested context timeframe
    df_ctx = resample_ohlcv(df_m1, context_tf)
    tf_label = context_tf.upper().replace('MIN', 'M').replace('H', 'H')
    print(f"Context TF: {tf_label} ({len(df_ctx)} bars)")

    return df_m1, df_ctx, dataset_id


def load_from_mongo(context_tf='5min'):
    """Load M1 candles from MongoDB, resample to context TF."""
    from pymongo import MongoClient

    mongo_uri = os.getenv('MONGODB_URI', 'mongodb://localhost:27017/trading')
    symbol = os.getenv('SYMBOL', 'OANDA:XAU_USD')

    client = MongoClient(mongo_uri)
    db = client.get_default_database()

    docs = list(db.candles.find({
        'symbol': symbol, 'interval': '1m'
    }).sort('timestamp', 1))

    if not docs:
        print("No M1 candles found in MongoDB.")
        return None, None, 'mongo'

    df_m1 = pd.DataFrame(docs)
    df_m1['timestamp'] = pd.to_datetime(df_m1['timestamp'], utc=True)
    df_m1.set_index('timestamp', inplace=True)
    df_m1.sort_index(inplace=True)

    if 'tickVolume' in df_m1.columns:
        df_m1['volume'] = df_m1['tickVolume'].where(df_m1['tickVolume'] > 0, df_m1.get('volume', 1))
    df_m1['volume'] = df_m1['volume'].fillna(1).replace(0, 1)

    df_ctx = resample_ohlcv(df_m1, context_tf)

    # Also pull actual paper trades for comparison
    actual_trades = list(db.paper_trades.find({'status': 'CLOSED'}).sort('entryTime', 1))

    return df_m1, df_ctx, 'mongo', actual_trades


# ===================== SNAPSHOT BUILDER =====================

def build_snapshot_for_bar(df_m1, df_m5, df_m5_shifted, i, m5_idx):
    """Build a MarketSnapshot for M1 bar at index position i.

    This mirrors main.py's build_snapshot() but works with positional indices
    for batch iteration.
    """
    m1 = df_m1.iloc[i]

    if pd.isna(m1['rsi']) or pd.isna(m1['ema21']):
        return None

    m5 = df_m5_shifted.iloc[m5_idx]

    if pd.isna(m5['atr']) or pd.isna(m5['ema9']) or pd.isna(m5['rsi']):
        return None

    # Slope lookback: find M5 bar 3 bars before the current shifted M5
    has_slope = False
    m5_ema9_prev = None
    m5_ema21_prev = None
    if m5_idx >= 3:
        m5_prev = df_m5.iloc[m5_idx - 3]
        if not pd.isna(m5_prev['ema9']) and not pd.isna(m5_prev['ema21']):
            has_slope = True
            m5_ema9_prev = m5_prev['ema9']
            m5_ema21_prev = m5_prev['ema21']

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
        live_price=None,  # dry-run uses m1_close
    )


# ===================== CORE DRY-RUN =====================

def run_dry_run(df_m1, df_ctx, dataset_id, trend_filter=True, spread_offset=0.0, context_tf='5min'):
    """Run the strategy engine over historical M1 + context TF data.

    Args:
        df_m1: M1 OHLCV DataFrame
        df_ctx: Context timeframe OHLCV DataFrame (M5, M15, etc.)
        dataset_id: Label for reporting
        context_tf: Label string for the context timeframe

    Returns (total_pnl, win_rate, trade_count, final_trades) or None.
    """
    tf_label = context_tf.upper().replace('MIN', 'M').replace('H', 'H')
    # Keep variable name df_m5 internally for snapshot builder compatibility
    df_m5 = df_ctx
    print(f"\n{'='*60}")
    print(f"  Dataset: {dataset_id} | Context: {tf_label}")
    print(f"{'='*60}")
    print(f"Data: M1({len(df_m1)} rows), {tf_label}({len(df_m5)} rows)")

    print("Computing indicators...")
    df_m1 = attach_indicators(df_m1)
    df_m5 = attach_indicators(df_m5)

    # Shift M5 by 1 to prevent lookahead (same as main.py)
    df_m5_shifted = df_m5.shift(1)

    # Reindex M5_shifted onto M1 index for fast lookup
    # For each M1 bar, find the corresponding M5_shifted bar
    m5_shifted_times = df_m5_shifted.index.values
    m1_times = df_m1.index.values

    print(f"Scanning {len(df_m1)} bars...")

    cooldowns = CooldownState()
    executed_orders = []

    for i in range(50, len(df_m1)):
        t = df_m1.index[i]

        # Convert timestamp to seconds for cooldown comparison
        if hasattr(t, 'timestamp'):
            now_secs = t.timestamp()
        else:
            now_secs = pd.Timestamp(t).timestamp()

        # Find M5 shifted index for this M1 bar
        m5_idx = np.searchsorted(m5_shifted_times, m1_times[i], side='right') - 1
        if m5_idx < 0 or m5_idx >= len(df_m5_shifted):
            continue

        # Build snapshot (same logic as main.py's build_snapshot)
        snap = build_snapshot_for_bar(df_m1, df_m5, df_m5_shifted, i, m5_idx)
        if snap is None:
            continue

        # Evaluate strategies — THE SAME FUNCTION AS LIVE
        signals = evaluate_strategies(snap, cooldowns, now_secs, spread_offset, trend_filter)

        for sig in signals:
            executed_orders.append({
                'strat': sig.strategy,
                'dir': sig.direction,
                'entry': sig.entry_price,
                'time': t,
                'tp': sig.tp,
                'sl': sig.sl,
                'status': 'PENDING',
                'meta': sig.meta,
            })

    print(f"Generated {len(executed_orders)} signals. Resolving TP/SL...")

    # ─── Forward-scan TP/SL resolution ───
    # Normalize to tz-naive for numpy operations
    df_naive = df_m1.copy()
    if df_naive.index.tz is not None:
        df_naive.index = df_naive.index.tz_localize(None)
    times = df_naive.index.values
    highs = df_naive['high'].values
    lows = df_naive['low'].values

    final_trades = []
    for tr in executed_orders:
        t_raw = tr['time']
        if hasattr(t_raw, 'tz') and t_raw.tz is not None:
            t_raw = t_raw.tz_localize(None)
        t_entry = pd.Timestamp(t_raw).to_datetime64()
        start_idx = np.searchsorted(times, t_entry)

        future_highs = highs[start_idx: start_idx + 1000]
        future_lows = lows[start_idx: start_idx + 1000]
        future_times = times[start_idx: start_idx + 1000]

        if len(future_highs) == 0:
            continue

        if tr['dir'] == 'LONG':
            tp_hits = future_highs >= tr['tp']
            sl_hits = future_lows <= tr['sl']
        else:
            tp_hits = future_lows <= tr['tp']
            sl_hits = future_highs >= tr['sl']

        tp_idx = np.argmax(tp_hits) if tp_hits.any() else len(future_highs)
        sl_idx = np.argmax(sl_hits) if sl_hits.any() else len(future_lows)

        if tp_idx == len(future_highs) and sl_idx == len(future_lows):
            tr['status'] = 'UNCLOSED'
            continue

        if tp_idx <= sl_idx:
            tr['exit'] = tr['tp']
            tr['exit_time'] = future_times[tp_idx]
            tr['pnl'] = abs(tr['exit'] - tr['entry']) * POSITION_OZ
            tr['status'] = 'CLOSED_TP'
        else:
            tr['exit'] = tr['sl']
            tr['exit_time'] = future_times[sl_idx]
            tr['pnl'] = -abs(tr['entry'] - tr['exit']) * POSITION_OZ
            tr['status'] = 'CLOSED_SL'

        t_naive = tr['time'].tz_localize(None) if hasattr(tr['time'], 'tz') and tr['time'].tz else tr['time']
        tr['duration_mins'] = int((tr['exit_time'] - pd.Timestamp(t_naive).to_datetime64()) / np.timedelta64(1, 'm'))
        final_trades.append(tr)

    unclosed = [o for o in executed_orders if o['status'] == 'UNCLOSED']
    df_res = pd.DataFrame(final_trades) if final_trades else pd.DataFrame()

    if len(df_res) == 0:
        print("No trades resolved.")
        return None

    # ─── Report ───
    overall_pnl = df_res['pnl'].sum()
    overall_wr = len(df_res[df_res['pnl'] > 0]) / len(df_res) * 100
    df_res['cum_pnl'] = df_res['pnl'].cumsum()
    max_dd = (df_res['cum_pnl'].cummax() - df_res['cum_pnl']).max()
    avg_win = df_res[df_res['pnl'] > 0]['pnl'].mean() if len(df_res[df_res['pnl'] > 0]) > 0 else 0
    avg_loss = df_res[df_res['pnl'] < 0]['pnl'].mean() if len(df_res[df_res['pnl'] < 0]) > 0 else 0
    avg_dur = df_res['duration_mins'].mean()
    pf = (df_res[df_res['pnl'] > 0]['pnl'].sum() / abs(df_res[df_res['pnl'] < 0]['pnl'].sum())) if df_res[df_res['pnl'] < 0]['pnl'].sum() != 0 else float('inf')
    wins = len(df_res[df_res['pnl'] > 0])
    losses = len(df_res) - wins

    print(f"""
{'='*60}
  🏆 DRY RUN RESULTS — {dataset_id}
{'='*60}
  Signals:     {len(executed_orders):>6}  (Closed: {len(final_trades)}, Unclosed: {len(unclosed)})
  Net PnL:     ${overall_pnl:>10.2f}
  Win Rate:    {overall_wr:>9.1f}%  ({wins}W / {losses}L)
  Profit Factor: {pf:>7.2f}
  Max Drawdown: ${max_dd:>9.2f}
  Avg Win:     ${avg_win:>10.2f}
  Avg Loss:    ${avg_loss:>10.2f}
  Avg Hold:    {avg_dur:>7.0f} min
""")

    # Strategy breakdown
    print("─── STRATEGY BREAKDOWN ─────────────────────────────────────")
    for strat in ['EMA_PULLBACK', 'BB_REVERSION', 'INST_BREAKOUT']:
        s_tr = df_res[df_res['strat'] == strat]
        if len(s_tr) > 0:
            s_wins = len(s_tr[s_tr['pnl'] > 0])
            s_wr = s_wins / len(s_tr) * 100
            s_pnl = s_tr['pnl'].sum()
            s_pf = (s_tr[s_tr['pnl'] > 0]['pnl'].sum() / abs(s_tr[s_tr['pnl'] < 0]['pnl'].sum())) if s_tr[s_tr['pnl'] < 0]['pnl'].sum() != 0 else float('inf')
            print(f"  {strat:17s} │ {s_wins}W/{len(s_tr)-s_wins}L │ WR: {s_wr:5.1f}% │ PnL: ${s_pnl:8.2f} │ PF: {s_pf:5.2f}")
            for d in ['LONG', 'SHORT']:
                d_tr = s_tr[s_tr['dir'] == d]
                if len(d_tr) > 0:
                    d_wins = len(d_tr[d_tr['pnl'] > 0])
                    d_wr = d_wins / len(d_tr) * 100
                    d_pnl = d_tr['pnl'].sum()
                    print(f"    └─ {d:5s}       │ {d_wins}W/{len(d_tr)-d_wins}L │ WR: {d_wr:5.1f}% │ PnL: ${d_pnl:8.2f}")
        else:
            print(f"  {strat:17s} │ No triggers")

    print("=" * 60)

    return overall_pnl, overall_wr, len(df_res), final_trades


def export_json(df_m1, final_trades, dataset_id):
    """Export candles + trades as JSON for dashboard visualization."""
    # Candles
    df_export = df_m1[['open', 'high', 'low', 'close']].copy()
    df_export = df_export.reset_index()
    time_col = df_export.columns[0]
    df_export[time_col] = (pd.to_datetime(df_export[time_col]).astype(np.int64) // 10**9)
    df_export = df_export.rename(columns={time_col: 'time'})
    df_export.sort_values('time', inplace=True)
    df_export = df_export[['time', 'open', 'high', 'low', 'close']]

    candles_path = os.path.join(DATA_DIR, f"chart_candles_{dataset_id}.json")
    df_export.to_json(candles_path, orient='records')

    # Trades
    trades_export = []
    for tr in final_trades:
        t_entry = int(pd.Timestamp(tr['time']).timestamp())
        t_exit = int(pd.Timestamp(tr['exit_time']).timestamp()) if 'exit_time' in tr else 0
        trades_export.append({
            'strat': tr['strat'], 'dir': tr['dir'],
            'entry': round(tr['entry'], 3), 'time': t_entry,
            'tp': round(tr['tp'], 3), 'sl': round(tr['sl'], 3),
            'status': tr['status'],
            'exit': round(tr.get('exit', 0), 3), 'exit_time': t_exit,
            'pnl': round(tr.get('pnl', 0), 2),
            'duration_mins': tr.get('duration_mins', 0),
            'meta': tr.get('meta', {}),
        })

    trades_path = os.path.join(DATA_DIR, f"chart_trades_{dataset_id}.json")
    with open(trades_path, "w") as f:
        json.dump(trades_export, f)

    print(f"Exported: {candles_path}, {trades_path}")


# ===================== ENTRY POINTS =====================

def run_csv_mode(csv_arg, trend_filter=True, context_tf='5min'):
    """Run against CSV tick data files."""
    DATASETS = {
        '202601': os.path.join(DATA_DIR, 'DAT_ASCII_XAUUSD_T_202601.csv'),
        '202602': os.path.join(DATA_DIR, 'DAT_ASCII_XAUUSD_T_202602.csv'),
        '202603': os.path.join(DATA_DIR, 'DAT_ASCII_XAUUSD_T_202603.csv'),
    }

    if csv_arg == 'all':
        summary = []
        for ds_id, path in DATASETS.items():
            df_m1, df_ctx, dataset_id = load_from_csv(path, context_tf)
            if df_m1 is not None:
                result = run_dry_run(df_m1, df_ctx, dataset_id, trend_filter, context_tf=context_tf)
                if result:
                    pnl, wr, count, trades = result
                    summary.append((dataset_id, pnl, wr, count))
                    export_json(df_m1, trades, dataset_id)

        if summary:
            print(f"\n{'#'*60}")
            print("  CROSS-DATASET COMPARISON (0.01 lot / 1 oz)")
            print(f"{'#'*60}")
            total = 0
            for ds, pnl, wr, count in summary:
                print(f"  {ds}  |  WR: {wr:5.1f}%  |  Net PnL: ${pnl:8.2f}  |  Trades: {count}")
                total += pnl
            print(f"  {'TOTAL':6s}  |  Combined Net PnL: ${total:.2f}")
            print(f"{'#'*60}")

    elif csv_arg == 'compare':
        datasets = list(DATASETS.keys())
        print("\n" + "=" * 80)
        print("  RUNNING WITHOUT TREND FILTER")
        print("=" * 80)
        no_filter = []
        for ds_id in datasets:
            df_m1, df_ctx, did = load_from_csv(DATASETS[ds_id], context_tf)
            if df_m1 is not None:
                result = run_dry_run(df_m1, df_ctx, did, trend_filter=False, context_tf=context_tf)
                if result: no_filter.append((did, result[0], result[1], result[2]))

        print("\n" + "=" * 80)
        print("  RUNNING WITH TREND FILTER")
        print("=" * 80)
        with_filter = []
        for ds_id in datasets:
            df_m1, df_ctx, did = load_from_csv(DATASETS[ds_id], context_tf)
            if df_m1 is not None:
                result = run_dry_run(df_m1, df_ctx, did, trend_filter=True, context_tf=context_tf)
                if result: with_filter.append((did, result[0], result[1], result[2]))

        # Build comparison table
        nf_dict = {d: (p, w, c) for d, p, w, c in no_filter}
        wf_dict = {d: (p, w, c) for d, p, w, c in with_filter}
        print("\n╔" + "═" * 78 + "╗")
        print("║" + "  M5 TREND FILTER — BEFORE vs AFTER COMPARISON".center(78) + "║")
        print("╠" + "═" * 78 + "╣")
        total_b, total_a = 0, 0
        for d in datasets:
            if d in nf_dict and d in wf_dict:
                bp, bw, bc = nf_dict[d]
                ap, aw, ac = wf_dict[d]
                delta = ap - bp
                total_b += bp; total_a += ap
                sign = "+" if delta >= 0 else ""
                line = f"  {d}    │  ${bp:8.2f} WR:{bw:4.1f}% #{bc:4d} │  ${ap:8.2f} WR:{aw:4.1f}% #{ac:4d} │ {sign}${delta:.2f}"
                print("║" + line.ljust(78) + "║")
        print("╠" + "═" * 78 + "╣")
        td = total_a - total_b
        sign = "+" if td >= 0 else ""
        line = f"  TOTAL    │  ${total_b:8.2f}              │  ${total_a:8.2f}              │ {sign}${td:.2f}"
        print("║" + line.ljust(78) + "║")
        print("╚" + "═" * 78 + "╝")

    else:
        # Single file
        if csv_arg in DATASETS:
            csv_path = DATASETS[csv_arg]
        else:
            csv_path = csv_arg
        df_m1, df_ctx, dataset_id = load_from_csv(csv_path, context_tf)
        if df_m1 is not None:
            result = run_dry_run(df_m1, df_ctx, dataset_id, trend_filter, context_tf=context_tf)
            if result:
                export_json(df_m1, result[3], dataset_id)


def run_mongo_mode(context_tf='5min'):
    """Run against MongoDB paper trading candles."""
    result = load_from_mongo(context_tf)
    if result is None:
        return
    df_m1, df_ctx, dataset_id, actual_trades = result

    if df_m1 is None:
        return

    print(f"  Loaded {len(df_m1)} M1 candles from MongoDB")
    print(f"  Range: {df_m1.index[0]} → {df_m1.index[-1]}")

    result = run_dry_run(df_m1, df_ctx, dataset_id, trend_filter=True,
                         spread_offset=float(os.getenv('SPREAD_OFFSET', '0.0')),
                         context_tf=context_tf)

    if result is None:
        return

    sim_pnl, sim_wr, sim_count, final_trades = result

    # Compare with actual paper trades
    if actual_trades:
        actual_pnl = sum(t.get('pnl', 0) for t in actual_trades)
        actual_wins = sum(1 for t in actual_trades if t.get('pnl', 0) > 0)
        actual_wr = (actual_wins / len(actual_trades) * 100) if actual_trades else 0

        print(f"""
─── ACTUAL PAPER TRADING COMPARISON ────────────────────────
  Actual Trades: {len(actual_trades):>6}
  Actual PnL:    ${actual_pnl:>10.2f}
  Actual WR:     {actual_wr:>9.1f}%  ({actual_wins}W / {len(actual_trades)-actual_wins}L)
  ──────────────────────
  PnL Delta:     ${sim_pnl - actual_pnl:>+10.2f}  (sim - actual)
""")
        if abs(sim_pnl - actual_pnl) > 5:
            print("  📊 Discrepancy is expected due to:")
            print("     - Live uses real-time tick prices vs M1 bar close")
            print("     - main.py checks idx=-2 (last completed) vs dry-run checks every bar")
            print("     - Spread/slippage in live execution")

    # Trade log
    print("─── TRADE LOG ──────────────────────────────────────────────")
    print(f"  {'#':>3} │ {'Strategy':17s} │ {'Dir':5s} │ {'Entry':>10s} │ {'Exit':>10s} │ {'PnL':>8s} │ {'Hold':>6s}")
    print("  " + "─" * 80)
    cum = 0
    for i, tr in enumerate(final_trades):
        cum += tr['pnl']
        emoji = '✅' if tr['pnl'] > 0 else '❌'
        print(f"  {i+1:>3} │ {tr['strat']:17s} │ {tr['dir']:5s} │ ${tr['entry']:>9.3f} │ ${tr['exit']:>9.3f} │ ${tr['pnl']:>+7.2f} │ {tr['duration_mins']:>4d}m {emoji} cum:${cum:+.2f}")


# ===================== MAIN =====================

def main():
    parser = argparse.ArgumentParser(description='XAU/USD Strategy Dry-Run')
    parser.add_argument('--csv', type=str, help='CSV file path, dataset ID (202601/202602/202603), "all", or "compare"')
    parser.add_argument('--mongo', action='store_true', help='Run against MongoDB paper trading data')
    parser.add_argument('--no-trend-filter', action='store_true', help='Disable trend filter')
    parser.add_argument('--context-tf', type=str, default='5min',
                        help='Context timeframe: 5min (default), 15min, 30min, 1h')

    args = parser.parse_args()

    if not args.csv and not args.mongo:
        parser.print_help()
        print("\nExamples:")
        print("  python dry_run.py --csv 202603")
        print("  python dry_run.py --csv 202603 --context-tf 15min")
        print("  python dry_run.py --csv all --context-tf 15min")
        print("  python dry_run.py --csv compare")
        print("  python dry_run.py --mongo")
        print("  python dry_run.py --mongo --context-tf 15min")
        return

    if args.csv:
        run_csv_mode(args.csv, trend_filter=not args.no_trend_filter, context_tf=args.context_tf)
    elif args.mongo:
        run_mongo_mode(context_tf=args.context_tf)


if __name__ == '__main__':
    main()
