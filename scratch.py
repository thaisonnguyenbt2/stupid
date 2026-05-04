import pandas as pd
from smartmoneyconcepts import smc

df_ticks = pd.read_csv('data/csv/DAT_ASCII_XAUUSD_T_202501.csv', names=['time', 'bid', 'ask', 'vol'])
df_ticks['time'] = pd.to_datetime(df_ticks['time'], format='%Y%m%d %H%M%S%f')
df_ticks['price'] = (df_ticks['bid'] + df_ticks['ask']) / 2
df_ticks.set_index('time', inplace=True)
df_m5 = df_ticks['price'].resample('5Min').ohlc().dropna().reset_index(drop=True)

swing = smc.swing_highs_lows(df_m5)
ob = smc.ob(df_m5, swing)

obs = ob[ob['OB'].notna()]
print(obs.head())

