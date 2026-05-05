import pandas as pd

df_ticks = pd.read_csv('data/csv/DAT_ASCII_XAUUSD_T_202604.csv', names=['time', 'bid', 'ask', 'vol'])
df_ticks['time'] = pd.to_datetime(df_ticks['time'], format='%Y%m%d %H%M%S%f')
df_ticks['price'] = (df_ticks['bid'] + df_ticks['ask']) / 2
df_ticks.set_index('time', inplace=True)

df_m5 = df_ticks['price'].resample('5Min').ohlc().dropna()
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
for i in range(50, len(df_m5)-20):
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
        limit_price = row['close'] - (0.15 * row['atr']) if signal == 'LONG' else row['close'] + (0.15 * row['atr'])
        signals.append({'index': i, 'signal': signal, 'atr': row['atr'], 'close': row['close'], 'limit_price': limit_price})

SPREAD = 0.5
wins = 0; losses = 0; expired = 0; net_profit = 0.0

for s in signals:
    i = s['index']
    signal = s['signal']
    limit_price = s['limit_price']
    
    executed = False
    exec_price = 0
    for j in range(i+1, min(i+4, len(df_m5))):
        l_row = df_m5.iloc[j]
        if signal == 'LONG' and l_row['low'] <= limit_price:
            executed = True; exec_price = limit_price; eval_start_idx = j; break
        elif signal == 'SHORT' and l_row['high'] >= limit_price:
            executed = True; exec_price = limit_price; eval_start_idx = j; break
            
    if not executed:
        expired += 1; continue
        
    tp_dist = (s['atr'] * 1.5) - SPREAD
    sl_dist = (s['atr'] * 1.5) + SPREAD
    tp = exec_price + tp_dist if signal == 'LONG' else exec_price - tp_dist
    sl = exec_price - sl_dist if signal == 'LONG' else exec_price + sl_dist
    
    win = False; loss = False
    for j in range(eval_start_idx+1, min(eval_start_idx+100, len(df_m5))):
        f_row = df_m5.iloc[j]
        if signal == 'LONG':
            if f_row['high'] >= tp: win = True; break
            elif f_row['low'] <= sl: loss = True; break
        else:
            if f_row['low'] <= tp: win = True; break
            elif f_row['high'] >= sl: loss = True; break

    if win: wins += 1; net_profit += tp_dist
    elif loss: losses += 1; net_profit -= sl_dist

total = wins + losses
win_rate = (wins / total) * 100 if total > 0 else 0

print(f"\n--- Normal Mode (15% Pullback Limit) | 1.5:1.5 R:R ---")
print(f"Signals: {len(signals)}")
print(f"Triggered: {total} ({expired} expired)")
print(f"Wins: {wins}")
print(f"Losses: {losses}")
print(f"WinRate: {win_rate:.1f}%")
print(f"Net Profit: ${net_profit:.2f} (1oz)")
