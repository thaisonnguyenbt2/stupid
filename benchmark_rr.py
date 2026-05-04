import pandas as pd

print("Loading recent data...")
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

# Generate signals (Normal mode)
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
        signals.append({
            'index': i,
            'signal': signal,
            'atr': row['atr'],
            'close': row['close']
        })

print(f"Generated {len(signals)} signals in April 2026.")

# Test R:R combinations
SPREAD = 0.5
rrs_to_test = [
    (1.0, 1.0), (1.5, 1.0), (1.5, 1.5), (2.0, 1.0), (2.5, 1.5), (3.0, 1.0), (3.0, 1.5)
]

print("\n--- R:R Net Profit (in USD points, assuming 1oz position) ---")
for tp_m, sl_m in rrs_to_test:
    wins = 0
    losses = 0
    net_profit = 0.0
    
    for s in signals:
        i = s['index']
        signal = s['signal']
        atr = s['atr']
        price = s['close']
        
        tp_dist = (atr * tp_m) - SPREAD
        sl_dist = (atr * sl_m) + SPREAD
        
        tp = price + tp_dist if signal == 'LONG' else price - tp_dist
        sl = price - sl_dist if signal == 'LONG' else price + sl_dist
        
        win = False
        loss = False
        for j in range(i+1, min(i+100, len(df_m5))):
            f_row = df_m5.iloc[j]
            if signal == 'LONG':
                if f_row['high'] >= tp: win = True; break
                elif f_row['low'] <= sl: loss = True; break
            else:
                if f_row['low'] <= tp: win = True; break
                elif f_row['high'] >= sl: loss = True; break

        if win:
            wins += 1
            net_profit += tp_dist
        elif loss:
            losses += 1
            net_profit -= sl_dist

    total = wins + losses
    win_rate = (wins / total) * 100 if total > 0 else 0
    
    print(f"R:R {tp_m}:{sl_m} | WinRate: {win_rate:.1f}% | Net Profit: ${net_profit:.2f}")

