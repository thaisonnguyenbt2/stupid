import sys
import pandas as pd
from pymongo import MongoClient
import os
sys.path.insert(0, '/home/ubuntu/trading/services/analyzer')
from main import SYMBOL
from strategy import calc_ema, calc_atr

client = MongoClient('mongodb://xau-trading-db:27017/')
db = client['trading']

print("Loading M1 candles since May 4th...")
candles = list(db['candles_OANDA:XAU_USD'].find({
    'timestamp': {'$gte': '2026-05-04T00:00:00Z'}
}).sort('timestamp', 1))

if not candles:
    print("No candles found for May 4th onwards.")
    sys.exit(0)

df_m1 = pd.DataFrame(candles)
df_m1['time'] = pd.to_datetime(df_m1['timestamp'])
df_m1.set_index('time', inplace=True)
df_m1['close'] = df_m1['close'].astype(float)
df_m1['high'] = df_m1['high'].astype(float)
df_m1['low'] = df_m1['low'].astype(float)
df_m1['open'] = df_m1['open'].astype(float)

df_m5 = df_m1.resample('5min').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
df_m5['ema9'] = df_m5['close'].ewm(span=9, adjust=False).mean()
df_m5['ema21'] = df_m5['close'].ewm(span=21, adjust=False).mean()
df_m5['ema50'] = df_m5['close'].ewm(span=50, adjust=False).mean()
df_m5['atr'] = (df_m5['high'] - df_m5['low']).rolling(14).mean()

delta = df_m5['close'].diff()
gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
rs = gain / loss
df_m5['rsi'] = 100 - (100 / (1 + rs))

signals = []
for i in range(50, len(df_m5)-1):
    row = df_m5.iloc[i]
    prev = df_m5.iloc[i-1]
    
    bull = prev['ema9'] > prev['ema21'] > prev['ema50']
    bear = prev['ema9'] < prev['ema21'] < prev['ema50']
    
    signal = None
    if bull and row['low'] <= prev['ema21'] and row['rsi'] <= 55:
        signal = 'LONG'
    elif bear and row['high'] >= prev['ema21'] and row['rsi'] >= 45:
        signal = 'SHORT'
        
    if signal:
        # REVERSE MODE
        exec_dir = 'SHORT' if signal == 'LONG' else 'LONG'
        limit_price = row['close'] - (0.30 * row['atr']) if exec_dir == 'LONG' else row['close'] + (0.30 * row['atr'])
        signals.append({'index': i, 'dir': exec_dir, 'atr': row['atr'], 'close': row['close'], 'limit': limit_price})

SPREAD = 0.5
wins = 0; losses = 0; expired = 0; pnl = 0.0

for s in signals:
    i = s['index']
    executed = False
    exec_price = 0
    # check trigger (within 3 bars = 15m)
    for j in range(i+1, min(i+4, len(df_m5))):
        l_row = df_m5.iloc[j]
        if s['dir'] == 'LONG' and l_row['low'] <= s['limit']:
            executed = True; exec_price = s['limit']; eval_start = j; break
        elif s['dir'] == 'SHORT' and l_row['high'] >= s['limit']:
            executed = True; exec_price = s['limit']; eval_start = j; break
            
    if not executed:
        expired += 1; continue
        
    # 1:3 R:R -> TP 1.0, SL 3.0
    tp_dist = (s['atr'] * 1.0) - SPREAD
    sl_dist = (s['atr'] * 3.0) + SPREAD
    tp = exec_price + tp_dist if s['dir'] == 'LONG' else exec_price - tp_dist
    sl = exec_price - sl_dist if s['dir'] == 'LONG' else exec_price + sl_dist
    
    win = False; loss = False
    for j in range(eval_start+1, min(eval_start+200, len(df_m5))):
        f_row = df_m5.iloc[j]
        if s['dir'] == 'LONG':
            if f_row['high'] >= tp: win = True; break
            elif f_row['low'] <= sl: loss = True; break
        else:
            if f_row['low'] <= tp: win = True; break
            elif f_row['high'] >= sl: loss = True; break

    if win: wins += 1; pnl += tp_dist
    elif loss: losses += 1; pnl -= sl_dist

total = wins + losses
wr = (wins / total * 100) if total > 0 else 0
print(f"--- REVERSE 1:3 (May 4 - May 5) ---")
print(f"Signals: {len(signals)} | Executed: {total} | Expired: {expired}")
print(f"Wins: {wins} | Losses: {losses} | WR: {wr:.1f}%")
print(f"Total PnL: ${pnl:.2f}")

