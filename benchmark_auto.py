import pandas as pd

print("Loading recent data (April 2026)...")
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

# Calculate EMA Spread and Smoothed Spread
df_m5['ema_spread_raw'] = abs(df_m5['ema9'] - df_m5['ema50']) / df_m5['atr']
df_m5['ema_spread_smooth'] = df_m5['ema_spread_raw'].rolling(48).mean()

SPREAD = 0.5

def run_sim(up_thresh, down_thresh):
    signals = []
    current_mode = 'NORMAL'
    mode_flips = 0
    
    for i in range(50, len(df_m5)-20):
        row = df_m5.iloc[i]
        prev = df_m5.iloc[i-1]
        
        # Determine mode
        smooth_spread = row['ema_spread_smooth']
        if pd.isna(smooth_spread):
            continue
            
        if smooth_spread > up_thresh and current_mode != 'NORMAL':
            current_mode = 'NORMAL'
            mode_flips += 1
        elif smooth_spread < down_thresh and current_mode != 'REVERSE':
            current_mode = 'REVERSE'
            mode_flips += 1
            
        bull = prev['ema9'] > prev['ema21'] > prev['ema50']
        bear = prev['ema9'] < prev['ema21'] < prev['ema50']
        
        signal = None
        if bull and row['low'] <= prev['ema21'] and row['rsi'] <= 55:
            signal = 'LONG'
        elif bear and row['high'] >= prev['ema21'] and row['rsi'] >= 45:
            signal = 'SHORT'
            
        if signal:
            exec_dir = signal
            if current_mode == 'REVERSE':
                exec_dir = 'SHORT' if signal == 'LONG' else 'LONG'
                pullback_pct = 0.30
            else:
                pullback_pct = 0.15
                
            limit_price = row['close'] - (pullback_pct * row['atr']) if exec_dir == 'LONG' else row['close'] + (pullback_pct * row['atr'])
            signals.append({'index': i, 'dir': exec_dir, 'atr': row['atr'], 'close': row['close'], 'limit_price': limit_price})

    wins = 0; losses = 0; expired = 0; net_profit = 0.0
    for s in signals:
        i = s['index']
        executed = False
        exec_price = 0
        for j in range(i+1, min(i+4, len(df_m5))):
            l_row = df_m5.iloc[j]
            if s['dir'] == 'LONG' and l_row['low'] <= s['limit_price']:
                executed = True; exec_price = s['limit_price']; eval_start_idx = j; break
            elif s['dir'] == 'SHORT' and l_row['high'] >= s['limit_price']:
                executed = True; exec_price = s['limit_price']; eval_start_idx = j; break
                
        if not executed:
            expired += 1; continue
            
        tp_dist = (s['atr'] * 1.5) - SPREAD
        sl_dist = (s['atr'] * 1.5) + SPREAD
        tp = exec_price + tp_dist if s['dir'] == 'LONG' else exec_price - tp_dist
        sl = exec_price - sl_dist if s['dir'] == 'LONG' else exec_price + sl_dist
        
        win = False; loss = False
        for j in range(eval_start_idx+1, min(eval_start_idx+100, len(df_m5))):
            f_row = df_m5.iloc[j]
            if s['dir'] == 'LONG':
                if f_row['high'] >= tp: win = True; break
                elif f_row['low'] <= sl: loss = True; break
            else:
                if f_row['low'] <= tp: win = True; break
                elif f_row['high'] >= sl: loss = True; break

        if win: wins += 1; net_profit += tp_dist
        elif loss: losses += 1; net_profit -= sl_dist
    
    total = wins + losses
    wr = (wins / total * 100) if total > 0 else 0
    print(f"[{up_thresh:.2f}/{down_thresh:.2f}] Flips: {mode_flips:3d} | Signals: {len(signals):3d} | Trigs: {total:3d} | W: {wins:3d} L: {losses:3d} | WR: {wr:4.1f}% | PnL: ${net_profit:7.2f}")

print("\n--- Auto-Switch Parameter Sweep (48-bar SMA) ---")
run_sim(0.40, 0.40)
run_sim(0.50, 0.40)
run_sim(0.50, 0.50)
run_sim(0.60, 0.40)
run_sim(0.70, 0.30)
run_sim(0.80, 0.40)

