"""
Dry-run backtest module for the Analyzer service.
=================================================
Reuses the EXACT same indicator and strategy logic from main.py
against historical CSV tick data or pre-cached JSON files.

Usage:
  python dry_run.py ../../data/DAT_ASCII_XAUUSD_T_202603.csv
  python dry_run.py ../../data/chart_candles_202603.json
  python dry_run.py all      # run all CSVs in ../../data/

Outputs:
  dryrun_candles_<id>.json   — M1 OHLC for chart visualization
  dryrun_trades_<id>.json    — executed trades with metadata

These JSON files are in the SAME format as /data/chart_*.json,
so you can load them in /data/index.html for visual verification.
"""

import os
import sys
import time
import json
import numpy as np
import pandas as pd

# Reuse indicator functions from main.py (same directory)
from main import calc_ema, calc_rsi, calc_atr

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'data')
OUTPUT_DIR = DATA_DIR  # Write JSON next to the existing chart files

# ----- Exness 0.01 lot sizing -----
LOT_SIZE = 0.01
CONTRACT_SIZE = 100  # 1 lot = 100 oz
POSITION_OZ = LOT_SIZE * CONTRACT_SIZE  # 1.0 oz

COOLDOWN = pd.Timedelta(minutes=10)


# ===================== DATA LOADING =====================

def load_csv_ticks(csv_path):
    """Load and resample DAT_ASCII tick CSV to M1 and M5 DataFrames."""
    print(f"[DryRun] Reading tick data from {csv_path}...")
    start = time.time()

    df_tick = pd.read_csv(csv_path, header=None, names=['time_str', 'bid', 'ask', 'volume'])
    df_tick['time_str'] = df_tick['time_str'] + '000'
    df_tick['time'] = pd.to_datetime(df_tick['time_str'], format='%Y%m%d %H%M%S%f')
    df_tick['price'] = (df_tick['bid'] + df_tick['ask']) / 2.0
    df_tick.set_index('time', inplace=True)
    df_tick.sort_index(inplace=True)

    print(f"[DryRun] Parsed {len(df_tick)} ticks in {time.time() - start:.1f}s. Resampling...")

    def agg_tf(freq):
        df_agg = df_tick.resample(freq).agg({
            'price': ['first', 'max', 'min', 'last'],
            'volume': 'sum'
        })
        df_agg.columns = ['open', 'high', 'low', 'close', 'volume']
        df_agg.dropna(inplace=True)
        # Tick volume: count ticks per candle
        tick_vol = df_tick.resample(freq)['price'].count()
        df_agg['tickVolume'] = tick_vol.reindex(df_agg.index).fillna(0)
        # Use tickVolume as the volume proxy (raw volume is often 0)
        df_agg['volume'] = df_agg['tickVolume'].where(df_agg['tickVolume'] > 0, df_agg['volume']).replace(0, 1)
        return df_agg

    df_m1 = agg_tf('1min')
    df_m5 = agg_tf('5min')

    return df_m1, df_m5


def load_json_candles(json_path):
    """Load pre-exported chart_candles JSON and build M1 + M5."""
    print(f"[DryRun] Loading JSON candles from {json_path}...")
    with open(json_path) as f:
        candles = json.load(f)

    df_m1 = pd.DataFrame(candles)
    df_m1['time'] = pd.to_datetime(df_m1['time'], unit='s')
    df_m1.set_index('time', inplace=True)
    df_m1.sort_index(inplace=True)
    if 'volume' not in df_m1.columns:
        df_m1['volume'] = 1  # Placeholder

    # Resample to M5
    df_m5 = df_m1.resample('5min').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
    })
    df_m5.dropna(inplace=True)

    return df_m1, df_m5


def try_load_cached(dataset_id):
    """Try to load from pickle cache first (much faster)."""
    m1_path = os.path.join(DATA_DIR, f'xau_m1_cache_{dataset_id}.pkl')
    m5_path = os.path.join(DATA_DIR, f'xau_m5_cache_{dataset_id}.pkl')
    if os.path.exists(m1_path) and os.path.exists(m5_path):
        print(f"[DryRun] Loading from pickle cache ({dataset_id})...")
        df_m1 = pd.read_pickle(m1_path)
        df_m5 = pd.read_pickle(m5_path)
        # Ensure volume is never 0
        df_m1['volume'] = df_m1['volume'].replace(0, 1)
        df_m5['volume'] = df_m5['volume'].replace(0, 1)
        return df_m1, df_m5
    return None, None


# ===================== INDICATORS =====================

def attach_indicators(df):
    """Compute all indicators on a DataFrame (same logic as main.py)."""
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


def attach_indicators_prefixed(df, prefix):
    """Compute indicators and rename columns with prefix for merging."""
    df = attach_indicators(df)
    rename = {}
    for c in df.columns:
        if c not in ['open', 'high', 'low', 'close', 'volume']:
            rename[c] = f"{c}_{prefix}"
        else:
            rename[c] = f"{c}_{prefix}"
    df.rename(columns=rename, inplace=True)
    return df


# ===================== STRATEGY ENGINE =====================

def run_backtest(df_m1, df_m5):
    """
    Execute all 3 strategies against historical data.
    Uses the EXACT same conditions as main.py (which matches XAUUSD_STRATEGIES.md).
    Returns list of trade dicts in the same format as data/dry_run_xau.py.
    """
    print(f"[DryRun] Computing indicators on {len(df_m1)} M1 and {len(df_m5)} M5 candles...")

    # Compute M1 indicators
    df_m1 = attach_indicators(df_m1)

    # Compute M5 indicators with prefix
    df_m5 = attach_indicators_prefixed(df_m5, 'm5')

    # M5 shifted by 1 bar to prevent lookahead, then forward-filled onto M1 index
    df_m5_shifted = df_m5.shift(1).copy()
    df_m5_shifted = df_m5_shifted.reindex(df_m1.index, method='ffill')

    # Join M5 onto M1
    df = df_m1.join(df_m5_shifted, lsuffix='', rsuffix='_m5_dup')
    df.dropna(inplace=True)

    print(f"[DryRun] Scanning {len(df)} aligned candles for strategy signals...")

    # Vectorized arrays for speed (identical to data/dry_run_xau.py)
    times = df.index.values
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values

    # M1 indicators
    m1_e21s = df['ema21'].values
    rsis = df['rsi'].values
    uppers = df['upper_bb'].values
    lowers = df['lower_bb'].values

    # M5 indicators
    m5_e9s = df['ema9_m5'].values
    m5_e21s = df['ema21_m5'].values
    m5_e50s = df['ema50_m5'].values
    m5_atrs = df['atr_m5'].values
    m5_vol = df['volume_m5'].values
    m5_volsma = df['vol_sma20_m5'].values
    m5_uppers = df['upper_bb_m5'].values
    m5_lowers = df['lower_bb_m5'].values
    m5_closes = df['close_m5'].values

    executed_orders = []
    last_ema_t = pd.Timestamp(0)
    last_bb_t = pd.Timestamp(0)
    last_inst_t = pd.Timestamp(0)

    for i in range(len(df)):
        t = pd.Timestamp(times[i])
        live_price = closes[i]

        # Global guard: M5 ATR must be alive
        if m5_atrs[i] < 0.05:
            continue

        # ==================== STRATEGY A: EMA Trend Pullback ====================
        if t - last_ema_t > COOLDOWN:
            m5_bull = m5_e9s[i] > m5_e21s[i] > m5_e50s[i]
            m5_bear = m5_e9s[i] < m5_e21s[i] < m5_e50s[i]

            direction = None
            if m5_bull and lows[i] <= m1_e21s[i] and rsis[i] <= 45:
                direction = 'LONG'
            elif m5_bear and highs[i] >= m1_e21s[i] and rsis[i] >= 55:
                direction = 'SHORT'

            if direction:
                last_ema_t = t
                tp_dist = m5_atrs[i] * 3.0
                sl_dist = m5_atrs[i] * 1.2
                tp = live_price + tp_dist if direction == 'LONG' else live_price - tp_dist
                sl = live_price - sl_dist if direction == 'LONG' else live_price + sl_dist

                meta = {
                    'rule': f"M5 EMA alignment ({'9>21>50 BULL' if m5_bull else '9<21<50 BEAR'}), M1 pullback to EMA21, M1 RSI reset",
                    'm1_close': round(live_price, 3),
                    'm1_ema21': round(m1_e21s[i], 3),
                    'm1_rsi': round(rsis[i], 2),
                    'm5_ema9': round(m5_e9s[i], 3),
                    'm5_ema21': round(m5_e21s[i], 3),
                    'm5_ema50': round(m5_e50s[i], 3),
                    'm5_atr': round(m5_atrs[i], 3),
                    'tp_mult': 3.0, 'sl_mult': 1.2,
                }
                executed_orders.append({
                    'strat': 'EMA_PULLBACK', 'dir': direction,
                    'entry': live_price, 'time': t,
                    'tp': tp, 'sl': sl, 'status': 'PENDING', 'meta': meta
                })

        # ==================== STRATEGY B: Bollinger Mean Reversion ====================
        if t - last_bb_t > COOLDOWN:
            direction = None
            bb_trigger = ''

            if highs[i] > uppers[i] and rsis[i] >= 75:
                direction = 'SHORT'
                bb_trigger = f"M1 High ({round(highs[i],3)}) > Upper BB ({round(uppers[i],3)}), RSI {round(rsis[i],2)} >= 75"
            elif lows[i] < lowers[i] and rsis[i] <= 25:
                direction = 'LONG'
                bb_trigger = f"M1 Low ({round(lows[i],3)}) < Lower BB ({round(lowers[i],3)}), RSI {round(rsis[i],2)} <= 25"

            if direction:
                last_bb_t = t
                tp_dist = m5_atrs[i] * 2.0
                sl_dist = m5_atrs[i] * 1.5
                tp = live_price + tp_dist if direction == 'LONG' else live_price - tp_dist
                sl = live_price - sl_dist if direction == 'LONG' else live_price + sl_dist

                meta = {
                    'rule': bb_trigger,
                    'm1_close': round(live_price, 3),
                    'm1_upper_bb': round(uppers[i], 3),
                    'm1_lower_bb': round(lowers[i], 3),
                    'm1_rsi': round(rsis[i], 2),
                    'm5_atr': round(m5_atrs[i], 3),
                    'tp_mult': 2.0, 'sl_mult': 1.5,
                }
                executed_orders.append({
                    'strat': 'BB_REVERSION', 'dir': direction,
                    'entry': live_price, 'time': t,
                    'tp': tp, 'sl': sl, 'status': 'PENDING', 'meta': meta
                })

        # ==================== STRATEGY C: Institutional Breakout ====================
        if t - last_inst_t > COOLDOWN:
            vol_ratio = (m5_vol[i] / m5_volsma[i]) if m5_volsma[i] > 0 else 0

            direction = None
            if vol_ratio > 2.0:
                if m5_closes[i] > m5_uppers[i] and lows[i] <= m5_e9s[i]:
                    direction = 'LONG'
                elif m5_closes[i] < m5_lowers[i] and highs[i] >= m5_e9s[i]:
                    direction = 'SHORT'

            if direction:
                last_inst_t = t
                tp_dist = m5_atrs[i] * 4.0
                sl_dist = m5_atrs[i] * 1.0
                tp = live_price + tp_dist if direction == 'LONG' else live_price - tp_dist
                sl = live_price - sl_dist if direction == 'LONG' else live_price + sl_dist

                meta = {
                    'rule': f"M5 Vol surge ({round(vol_ratio,1)}x > 2.0x SMA20), M5 close {'above Upper BB' if m5_closes[i] > m5_uppers[i] else 'below Lower BB'}, M1 pullback to M5 EMA9",
                    'm1_close': round(live_price, 3),
                    'm5_volume': round(m5_vol[i], 0),
                    'm5_vol_ratio': round(vol_ratio, 2),
                    'm5_close': round(m5_closes[i], 3),
                    'm5_ema9': round(m5_e9s[i], 3),
                    'm5_atr': round(m5_atrs[i], 3),
                    'tp_mult': 4.0, 'sl_mult': 1.0,
                }
                executed_orders.append({
                    'strat': 'INST_BREAKOUT', 'dir': direction,
                    'entry': live_price, 'time': t,
                    'tp': tp, 'sl': sl, 'status': 'PENDING', 'meta': meta
                })

    # ==================== FORWARD PATH RESOLUTION ====================
    print(f"[DryRun] {len(executed_orders)} signals. Resolving TP/SL forward...")

    final_trades = []
    for tr in executed_orders:
        t_entry = tr['time'].to_datetime64()
        start_idx = np.searchsorted(times, t_entry)
        future_highs = highs[start_idx:start_idx + 1000]
        future_lows = lows[start_idx:start_idx + 1000]
        future_times = times[start_idx:start_idx + 1000]

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

        exit_idx = tp_idx if tp_idx <= sl_idx else sl_idx

        if tp_idx <= sl_idx:
            tr['exit'] = tr['tp']
            tr['exit_time'] = future_times[exit_idx]
            tr['pnl'] = abs(tr['exit'] - tr['entry']) * POSITION_OZ
            tr['status'] = 'CLOSED_TP'
        else:
            tr['exit'] = tr['sl']
            tr['exit_time'] = future_times[exit_idx]
            tr['pnl'] = -abs(tr['entry'] - tr['exit']) * POSITION_OZ
            tr['status'] = 'CLOSED_SL'

        tr['duration_mins'] = int((tr['exit_time'] - tr['time']) / np.timedelta64(1, 'm'))
        final_trades.append(tr)

    return df_m1, final_trades


# ===================== RESULTS & EXPORT =====================

def print_results(final_trades, dataset_label):
    """Print backtest results summary."""
    if not final_trades:
        print(f"[DryRun] No trades resolved for {dataset_label}.")
        return 0, 0, 0

    df_res = pd.DataFrame(final_trades)
    overall_pnl = df_res['pnl'].sum()
    overall_wr = len(df_res[df_res['pnl'] > 0]) / len(df_res) * 100
    df_res['cum_pnl'] = df_res['pnl'].cumsum()
    max_dd = (df_res['cum_pnl'].cummax() - df_res['cum_pnl']).max()

    print(f"\n{'='*60}")
    print(f"  DRY RUN RESULTS — {dataset_label}")
    print(f"{'='*60}")
    print(f"  Net PnL:      ${overall_pnl:.2f}")
    print(f"  Win Rate:     {overall_wr:.1f}%")
    print(f"  Max Drawdown: ${max_dd:.2f}")
    print(f"  Total Trades: {len(df_res)}")
    print()

    for strat in ['EMA_PULLBACK', 'BB_REVERSION', 'INST_BREAKOUT']:
        s_tr = df_res[df_res['strat'] == strat]
        if len(s_tr) > 0:
            s_wr = len(s_tr[s_tr['pnl'] > 0]) / len(s_tr) * 100
            s_pnl = s_tr['pnl'].sum()
            avg_dur = s_tr['duration_mins'].mean()
            print(f"  {strat:15s} | WR: {s_wr:5.1f}% | Net: ${s_pnl:8.2f} | Hold: {avg_dur:4.0f}m | Trades: {len(s_tr)}")
        else:
            print(f"  {strat:15s} | No triggers")

    print(f"{'='*60}")
    return overall_pnl, overall_wr, len(df_res)


def export_json(df_m1, final_trades, dataset_id):
    """Export JSON files in the same format as data/dry_run_xau.py for chart visualization."""
    # Candles JSON
    df_export = df_m1[['open', 'high', 'low', 'close']].copy().reset_index()
    df_export.columns = ['time'] + list(df_export.columns[1:])
    df_export['time'] = (df_export['time'].astype(np.int64) // 10**9)
    df_export.sort_values('time', inplace=True)

    candles_path = os.path.join(OUTPUT_DIR, f"dryrun_candles_{dataset_id}.json")
    df_export[['time', 'open', 'high', 'low', 'close']].to_json(candles_path, orient='records')

    # Trades JSON
    trades_export = []
    for tr in final_trades:
        trades_export.append({
            'strat': tr['strat'],
            'dir': tr['dir'],
            'entry': round(tr['entry'], 3),
            'time': int(pd.Timestamp(tr['time']).timestamp()),
            'tp': round(tr['tp'], 3),
            'sl': round(tr['sl'], 3),
            'status': tr['status'],
            'exit': round(tr['exit'], 3),
            'exit_time': int(pd.Timestamp(tr['exit_time']).timestamp()),
            'pnl': round(tr['pnl'], 2),
            'duration_mins': tr['duration_mins'],
            'meta': tr.get('meta', {})
        })

    trades_path = os.path.join(OUTPUT_DIR, f"dryrun_trades_{dataset_id}.json")
    with open(trades_path, 'w') as f:
        json.dump(trades_export, f)

    print(f"[DryRun] Exported: {candles_path}")
    print(f"[DryRun] Exported: {trades_path}")
    return candles_path, trades_path


# ===================== MAIN CLI =====================

DATASETS = {
    '202601': 'DAT_ASCII_XAUUSD_T_202601.csv',
    '202602': 'DAT_ASCII_XAUUSD_T_202602.csv',
    '202603': 'DAT_ASCII_XAUUSD_T_202603.csv',
}


def run_single(source_path):
    """Run a single backtest from a CSV or JSON file."""
    basename = os.path.basename(source_path)

    # Determine dataset ID from filename
    dataset_id = None
    for did, fname in DATASETS.items():
        if fname in basename:
            dataset_id = did
            break

    if dataset_id is None:
        # Try to extract from filename pattern
        import re
        m = re.search(r'(\d{6})', basename)
        dataset_id = m.group(1) if m else basename.replace('.', '_')

    # Try pickle cache first
    df_m1, df_m5 = try_load_cached(dataset_id)

    if df_m1 is None:
        if source_path.endswith('.csv'):
            df_m1, df_m5 = load_csv_ticks(source_path)
        elif source_path.endswith('.json'):
            df_m1, df_m5 = load_json_candles(source_path)
        else:
            print(f"[DryRun] Unsupported file: {source_path}")
            return None

    df_m1, final_trades = run_backtest(df_m1, df_m5)
    pnl, wr, count = print_results(final_trades, dataset_id)
    export_json(df_m1, final_trades, dataset_id)
    return pnl, wr, count


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python dry_run.py <csv_or_json_path>")
        print("  python dry_run.py all")
        print()
        print("Examples:")
        print("  python dry_run.py ../../data/DAT_ASCII_XAUUSD_T_202603.csv")
        print("  python dry_run.py ../../data/chart_candles_202603.json")
        print("  python dry_run.py all")
        sys.exit(1)

    arg = sys.argv[1]

    if arg == 'all':
        print("\n" + "#" * 60)
        print("  ANALYZER DRY RUN — ALL DATASETS")
        print("#" * 60)
        summary = []
        for did, fname in sorted(DATASETS.items()):
            fpath = os.path.join(DATA_DIR, fname)
            if not os.path.exists(fpath):
                print(f"[DryRun] Skipping {fname} (not found)")
                continue
            result = run_single(fpath)
            if result:
                summary.append((did, *result))

        if summary:
            print("\n" + "#" * 60)
            print("  CROSS-DATASET COMPARISON (0.01 lot / 1 oz)")
            print("#" * 60)
            total_pnl = 0
            for d, pnl, wr, count in summary:
                print(f"  {d}  |  WR: {wr:5.1f}%  |  Net PnL: ${pnl:8.2f}  |  Trades: {count}")
                total_pnl += pnl
            print(f"  {'TOTAL':6s}  |  Combined Net PnL: ${total_pnl:.2f}")
            print("#" * 60)
    else:
        # Single file path
        source = arg
        if not os.path.isabs(source):
            source = os.path.join(os.getcwd(), source)
        if not os.path.exists(source):
            # Try relative to DATA_DIR
            source = os.path.join(DATA_DIR, os.path.basename(arg))
        if not os.path.exists(source):
            print(f"[DryRun] File not found: {arg}")
            sys.exit(1)
        run_single(source)


if __name__ == '__main__':
    main()
