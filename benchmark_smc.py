import pandas as pd
from smartmoneyconcepts import smc

# Basic params
TP_MULT = 1.5
SL_MULT = 1.5
SPREAD_OFFSET = 0.5

# Load 3 months of data
dfs = []
for m in ['202501', '202502', '202503']:
    d = pd.read_csv(f'data/csv/DAT_ASCII_XAUUSD_T_{m}.csv', names=['time', 'bid', 'ask', 'vol'])
    dfs.append(d)
df_ticks = pd.concat(dfs)
df_ticks['time'] = pd.to_datetime(df_ticks['time'], format='%Y%m%d %H%M%S%f')
df_ticks['price'] = (df_ticks['bid'] + df_ticks['ask']) / 2
df_ticks.set_index('time', inplace=True)

df_m5 = df_ticks['price'].resample('5Min').ohlc().dropna()
df_m5['atr'] = (df_m5['high'] - df_m5['low']).rolling(14).mean()
df_m5['ema9'] = df_m5['close'].ewm(span=9, adjust=False).mean()
df_m5['ema21'] = df_m5['close'].ewm(span=21, adjust=False).mean()
df_m5['ema50'] = df_m5['close'].ewm(span=50, adjust=False).mean()

print("Calculating FVG on 3 months data...")
fvgs = smc.fvg(df_m5.reset_index(drop=True))
df_m5['fvg'] = fvgs['FVG'].values
df_m5['fvg_top'] = fvgs['Top'].values
df_m5['fvg_bot'] = fvgs['Bottom'].values
df_m5['fvg_mit'] = fvgs['MitigatedIndex'].values

print("Evaluating SMC + Trend Filter...")
trades_smc = 0
wins_smc = 0

traded_fvgs = set()

for i in range(50, len(df_m5)-20):
    row = df_m5.iloc[i]
    prev = df_m5.iloc[i-1]
    
    bull = prev['ema9'] > prev['ema21'] > prev['ema50']
    bear = prev['ema9'] < prev['ema21'] < prev['ema50']
    
    signal = None
    lookback = df_m5.iloc[i-50:i]
    
    # Check for Bullish FVG tap WITH Bullish Trend
    if bull:
        valid_bulls = lookback[(lookback['fvg'] == 1) & ((lookback['fvg_mit'] == 0) | (lookback['fvg_mit'] > i))]
        for idx, fvg in valid_bulls.iterrows():
            if row['low'] <= fvg['fvg_top'] and idx not in traded_fvgs:
                signal = 'LONG'
                traded_fvgs.add(idx)
                break
                
    if not signal and bear:
        valid_bears = lookback[(lookback['fvg'] == -1) & ((lookback['fvg_mit'] == 0) | (lookback['fvg_mit'] > i))]
        for idx, fvg in valid_bears.iterrows():
            if row['high'] >= fvg['fvg_bot'] and idx not in traded_fvgs:
                signal = 'SHORT'
                traded_fvgs.add(idx)
                break

    if signal:
        trades_smc += 1
            
        tp = row['close'] + (row['atr'] * TP_MULT) - SPREAD_OFFSET if signal == 'LONG' else row['close'] - (row['atr'] * TP_MULT) + SPREAD_OFFSET
        sl = row['close'] - (row['atr'] * SL_MULT) + SPREAD_OFFSET if signal == 'LONG' else row['close'] + (row['atr'] * SL_MULT) - SPREAD_OFFSET
        
        win = False
        for j in range(i+1, min(i+100, len(df_m5))):
            f_row = df_m5.iloc[j]
            if signal == 'LONG':
                if f_row['high'] >= tp: win = True; break
                elif f_row['low'] <= sl: break
            else:
                if f_row['low'] <= tp: win = True; break
                elif f_row['high'] >= sl: break
                
        if win:
            wins_smc += 1

print("\n=== SMC FVG + EMA TREND (Q1 2025) ===")
print(f"TP/SL Ratio: {TP_MULT} ATR / {SL_MULT} ATR (1:1 R:R)")
print(f"Trades: {trades_smc}, Wins: {wins_smc}, WinRate: {(wins_smc/trades_smc)*100 if trades_smc else 0:.1f}%")

