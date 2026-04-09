# XAU/USD Programmatic Trading Strategies (Exness Optimized)

This document specifies the **exact algorithmic conditions** implemented in `dry_run_xau.py`. Every trade in the backtest is generated strictly by these rules. No discretionary logic is used.

---

## Global Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| `COOLDOWN` | 10 minutes | Minimum time between successive entries of the same strategy |
| M5 ATR Guard | `m5_atr >= 0.05` | All strategies are **disabled** when ATR is near-zero (dead/weekend market) |
| Execution Price | M1 Close | Entry is at the Close of the M1 candle that satisfies all conditions |

---

## Strategy A: EMA Trend Pullback

**Concept**: Ride the prevailing M5 trend by entering on M1 pullbacks to the 21-EMA.

### Indicators Used
- M5: `EMA(9)`, `EMA(21)`, `EMA(50)`
- M1: `EMA(21)`, `RSI(14)`
- Risk: M5 `ATR(14)`

### Exact Entry Conditions

#### LONG
All of the following must be true simultaneously on the same M1 candle:

| # | Condition | Code Reference |
|---|-----------|----------------|
| 1 | M5 EMA alignment is bullish | `m5_ema9 > m5_ema21 > m5_ema50` |
| 2 | M1 candle low touches or pierces the M1 EMA21 | `m1_low <= m1_ema21` |
| 3 | M1 RSI(14) is at or belolasw 45 (pullback exhaustion) | `m1_rsi <= 45` |
| 4 | Cooldown elapsed since last EMA_PULLBACK entry | `time - last_ema_t > 10min` |
| 5 | M5 ATR is alive | `m5_atr >= 0.05` |

#### SHORT
All of the following must be true simultaneously on the same M1 candle:

| # | Condition | Code Reference |
|---|-----------|----------------|
| 1 | M5 EMA alignment is bearish | `m5_ema9 < m5_ema21 < m5_ema50` |
| 2 | M1 candle high touches or exceeds the M1 EMA21 | `m1_high >= m1_ema21` |
| 3 | M1 RSI(14) is at or above 55 (overbought in downtrend) | `m1_rsi >= 55` |
| 4 | Cooldown elapsed since last EMA_PULLBACK entry | `time - last_ema_t > 10min` |
| 5 | M5 ATR is alive | `m5_atr >= 0.05` |

### Risk Management
| Parameter | Value |
|-----------|-------|
| Take Profit | `entry ± (m5_atr × 3.0)` |
| Stop Loss | `entry ∓ (m5_atr × 1.2)` |
| Reward:Risk | 2.5:1 |

### Exit Resolution
Forward-scan up to 1000 M1 candles. Whichever threshold (TP or SL) is breached first by `m1_high` or `m1_low` determines the outcome.

---

## Strategy B: Bollinger Band Mean Reversion

**Concept**: Fade extreme M1 price extensions beyond the Bollinger Bands when RSI confirms exhaustion.

### Indicators Used
- M1: Bollinger Bands (`SMA(20)`, `StdDev × 2.0`), `RSI(14)`
- Risk: M5 `ATR(14)`

### Exact Entry Conditions

#### SHORT (Fade the top)
All of the following must be true simultaneously on the same M1 candle:

| # | Condition | Code Reference |
|---|-----------|----------------|
| 1 | M1 candle high breaks above the Upper Bollinger Band | `m1_high > m1_upper_bb` |
| 2 | M1 RSI(14) is extremely overbought | `m1_rsi >= 75` |
| 3 | Cooldown elapsed since last BB_REVERSION entry | `time - last_bb_t > 10min` |
| 4 | M5 ATR is alive | `m5_atr >= 0.05` |

#### LONG (Fade the bottom)
All of the following must be true simultaneously on the same M1 candle:

| # | Condition | Code Reference |
|---|-----------|----------------|
| 1 | M1 candle low breaks below the Lower Bollinger Band | `m1_low < m1_lower_bb` |
| 2 | M1 RSI(14) is extremely oversold | `m1_rsi <= 25` |
| 3 | Cooldown elapsed since last BB_REVERSION entry | `time - last_bb_t > 10min` |
| 4 | M5 ATR is alive | `m5_atr >= 0.05` |

### Risk Management
| Parameter | Value |
|-----------|-------|
| Take Profit | `entry ± (m5_atr × 2.0)` |
| Stop Loss | `entry ∓ (m5_atr × 1.5)` |
| Reward:Risk | 1.33:1 |

### Exit Resolution
Same forward-scan logic as Strategy A. First TP/SL hit within 1000 M1 candles resolves the trade.

---

## Strategy C: Institutional Breakout (Volume Anomaly)

**Concept**: Detect massive institutional order flow via volume anomalies on M5, then enter on a micro-pullback on M1 for a tight-risk momentum ride.

### Indicators Used
- M5: Volume, `Volume SMA(20)`, Bollinger Bands (`SMA(20)`, `StdDev × 2.0`), `EMA(9)`, `RSI(14)`
- M1: Price action (high/low relative to M5 EMA9)
- Risk: M5 `ATR(14)`

### Exact Entry Conditions

#### LONG
All of the following must be true simultaneously on the same M1 candle:

| # | Condition | Code Reference |
|---|-----------|----------------|
| 1 | M5 Volume surge exceeds 200% of its 20-period SMA | `m5_volume / m5_vol_sma20 > 2.0` |
| 2 | M5 close is above the M5 Upper Bollinger Band | `m5_close > m5_upper_bb` |
| 3 | M1 candle low touches or pierces the M5 EMA9 (micro-pullback) | `m1_low <= m5_ema9` |
| 4 | Cooldown elapsed since last INST_BREAKOUT entry | `time - last_inst_t > 10min` |
| 5 | M5 ATR is alive | `m5_atr >= 0.05` |

#### SHORT
All of the following must be true simultaneously on the same M1 candle:

| # | Condition | Code Reference |
|---|-----------|----------------|
| 1 | M5 Volume surge exceeds 200% of its 20-period SMA | `m5_volume / m5_vol_sma20 > 2.0` |
| 2 | M5 close is below the M5 Lower Bollinger Band | `m5_close < m5_lower_bb` |
| 3 | M1 candle high touches or exceeds the M5 EMA9 (micro-pullback) | `m1_high >= m5_ema9` |
| 4 | Cooldown elapsed since last INST_BREAKOUT entry | `time - last_inst_t > 10min` |
| 5 | M5 ATR is alive | `m5_atr >= 0.05` |

### Risk Management
| Parameter | Value |
|-----------|-------|
| Take Profit | `entry ± (m5_atr × 4.0)` |
| Stop Loss | `entry ∓ (m5_atr × 1.0)` |
| Reward:Risk | 4:1 |

### Exit Resolution
Same forward-scan logic. First TP/SL hit within 1000 M1 candles resolves the trade.

---

## Indicator Computation Reference

All indicators are computed using standard formulations:

| Indicator | Formula |
|-----------|---------|
| EMA(n) | Exponential Weighted Mean with `span=n, adjust=False` |
| RSI(14) | Wilder's smoothing via `ewm(alpha=1/14)` on gain/loss deltas |
| ATR(14) | Simple rolling mean of True Range over 14 periods |
| Bollinger Bands | `SMA(20) ± (StdDev(20) × 2.0)` |
| Volume SMA(20) | Simple rolling mean of volume over 20 periods |

## M5 Data Alignment

M5 indicator values are **shifted by 1 bar** (`df_m5.shift(1)`) and then forward-filled onto the M1 index. This prevents lookahead bias — each M1 candle only sees the *previously completed* M5 candle's indicators, never the current one.
