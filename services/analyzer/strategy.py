"""
XAU/USD Strategy Engine — Single Source of Truth
=================================================
This module contains ALL strategy logic:
  - Indicator computation (EMA, RSI, ATR, Bollinger Bands)
  - M5 trend bias classification
  - Counter-trend filter (hard + soft blocks)
  - Strategy A: EMA Trend Pullback
  - Strategy B: Bollinger Band Mean Reversion
  - Strategy C: Institutional Breakout (Volume Anomaly)

RULES:
  - No I/O: never touches MongoDB, filesystem, or network
  - No side effects: never prints, logs, or sends notifications
  - Deterministic: same inputs → same outputs
  - Both main.py (live) and dry_run.py (backtest) import from here
"""

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd


# ===================== CONSTANTS =====================

COOLDOWN_SECS = 180        # 3 minutes
ATR_MIN = 0.05             # Dead-market guard
LOT_SIZE = 0.01
CONTRACT_SIZE = 100        # 1 lot = 100 troy oz
POSITION_OZ = LOT_SIZE * CONTRACT_SIZE  # 1.0 oz

# Strategy TP/SL multipliers (best: 3:1 R:R, no filter, +$851/mo)
EMA_TP_MULT = 1.5
EMA_SL_MULT = 0.5
BB_TP_MULT = 1.5
BB_SL_MULT = 0.5
INST_TP_MULT = 1.5
INST_SL_MULT = 0.5

# Trend filter thresholds
EMA9_SLOPE_THRESHOLD = 0.5  # USD per 15-min lookback


# ===================== DATA CLASSES =====================

@dataclass
class MarketSnapshot:
    """All indicator values needed to evaluate strategies on one M1 bar.

    This is the interface between data loading (which differs between live
    and dry-run) and strategy evaluation (which must be identical).
    """
    # M1 bar values
    m1_close: float
    m1_high: float
    m1_low: float
    m1_rsi: float
    m1_ema21: float
    m1_upper_bb: float
    m1_lower_bb: float
    m1_bb_sma: float
    m1_atr: float

    # M5 indicator values (from previously completed M5 candle)
    m5_atr: float
    m5_ema9: float
    m5_ema21: float
    m5_ema50: float
    m5_rsi: float
    m5_close: float
    m5_high: float
    m5_low: float
    m5_upper_bb: float
    m5_lower_bb: float
    m5_volume: float
    m5_vol_sma20: float

    # M5 slope lookback (3 bars back, for trend filter)
    m5_ema9_prev: Optional[float] = None   # EMA9 from 3 M5 bars ago
    m5_ema21_prev: Optional[float] = None  # EMA21 from 3 M5 bars ago
    has_slope_data: bool = False            # True if prev values are valid

    # Execution price (may differ from m1_close in live due to live tick)
    live_price: Optional[float] = None

    @property
    def entry_price(self) -> float:
        return self.live_price if self.live_price is not None else self.m1_close


@dataclass
class Signal:
    """Output of a strategy evaluation — a trade to open."""
    strategy: str       # 'EMA_PULLBACK' / 'BB_REVERSION' / 'INST_BREAKOUT'
    direction: str      # 'LONG' / 'SHORT'
    entry_price: float
    tp: float
    sl: float
    meta: dict


@dataclass
class CooldownState:
    """Mutable cooldown tracker. Passed into evaluate_strategies() and updated."""
    last_ema: float = 0.0    # timestamp (seconds) of last EMA_PULLBACK entry
    last_bb: float = 0.0     # timestamp (seconds) of last BB_REVERSION entry
    last_inst: float = 0.0   # timestamp (seconds) of last INST_BREAKOUT entry


# ===================== INDICATOR FUNCTIONS =====================

def calc_ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average with span, no adjustment."""
    return series.ewm(span=span, adjust=False).mean()


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI using Wilder's smoothing via ewm(alpha=1/period)."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range using simple rolling mean of True Range."""
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = ranges.max(axis=1)
    return true_range.rolling(period).mean()


def attach_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all indicators on an OHLCV DataFrame (M1 or M5).

    Expects columns: open, high, low, close, volume
    Adds: ema9, ema21, ema50, rsi, atr, bb_sma, bb_std, upper_bb, lower_bb, vol_sma20
    """
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


def resample_ohlcv(df: pd.DataFrame, freq: str = '5min') -> pd.DataFrame:
    """Resample OHLCV data to a higher timeframe.

    Args:
        df: Source DataFrame with OHLCV columns and DatetimeIndex
        freq: Pandas frequency string (e.g. '5min', '15min', '30min', '1h')
    """
    df_out = df.resample(freq).agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    })
    df_out.dropna(inplace=True)
    return df_out


def resample_m5(df_m1: pd.DataFrame) -> pd.DataFrame:
    """Convenience alias: resample M1 → M5."""
    return resample_ohlcv(df_m1, '5min')


# ===================== TREND FILTER =====================

def compute_trend_bias(
    m5_ema9: float, m5_ema21: float, m5_close: float,
    m5_ema9_prev: Optional[float], m5_ema21_prev: Optional[float],
) -> str:
    """Classify M5 trend as BULL_STRONG, BEAR_STRONG, or NEUTRAL.

    Uses EMA slope over ~3 M5 bars (15 min) plus price position
    relative to EMAs.
    """
    if m5_ema9_prev is None or m5_ema21_prev is None:
        return 'NEUTRAL'

    ema9_slope = m5_ema9 - m5_ema9_prev
    ema21_slope = m5_ema21 - m5_ema21_prev

    # Strong bearish: EMAs falling, price below both, EMA9 < EMA21
    if (ema9_slope < 0 and ema21_slope < 0 and
        m5_close < m5_ema9 and m5_close < m5_ema21 and
        m5_ema9 < m5_ema21):
        return 'BEAR_STRONG'

    # Strong bullish: EMAs rising, price above both, EMA9 > EMA21
    if (ema9_slope > 0 and ema21_slope > 0 and
        m5_close > m5_ema9 and m5_close > m5_ema21 and
        m5_ema9 > m5_ema21):
        return 'BULL_STRONG'

    return 'NEUTRAL'


def is_counter_trend(
    direction: str,
    m5_trend_bias: str,
    m5_close: float,
    m5_ema21: float,
    ema9_slope: Optional[float],
) -> bool:
    """Return True if this direction fights the M5 trend.

    Hard block only: if M5 trend is strongly opposite to direction.
    (Soft block removed — too aggressive for M15 context, blocks valid
    pullback entries for 15+ min when a single bar closes below EMA21.)
    """
    if m5_trend_bias == 'BEAR_STRONG' and direction == 'LONG':
        return True
    if m5_trend_bias == 'BULL_STRONG' and direction == 'SHORT':
        return True

    return False


# ===================== STRATEGY EVALUATION =====================

def evaluate_strategies(
    snap: MarketSnapshot,
    cooldowns: CooldownState,
    now: float,
    spread_offset: float = 0.0,
    trend_filter: bool = True,
) -> List[Signal]:
    """Evaluate all 3 strategies on the given market snapshot.

    Args:
        snap: Current market state (M1 + M5 indicators)
        cooldowns: Mutable cooldown state (updated in-place when signal fires)
        now: Current timestamp in seconds (time.time() for live, bar time for dry-run)
        spread_offset: USD spread compensation (applied to TP/SL distances)
        trend_filter: If False, skip the counter-trend filter entirely

    Returns:
        List of Signal objects (0 to 3 signals possible per bar)
    """
    signals: List[Signal] = []

    # Global guard: M5 ATR must be alive
    if snap.m5_atr < ATR_MIN:
        return signals

    price = snap.entry_price

    # ─── Compute trend bias ───
    m5_trend_bias = 'NEUTRAL'
    ema9_slope = None

    if snap.has_slope_data:
        m5_trend_bias = compute_trend_bias(
            snap.m5_ema9, snap.m5_ema21, snap.m5_close,
            snap.m5_ema9_prev, snap.m5_ema21_prev,
        )
        ema9_slope = snap.m5_ema9 - snap.m5_ema9_prev

    def check_counter_trend(direction: str) -> bool:
        if not trend_filter:
            return False
        return is_counter_trend(
            direction, m5_trend_bias,
            snap.m5_close, snap.m5_ema21, ema9_slope,
        )

    # ─── Helper to compute TP/SL ───
    def calc_tp_sl(direction, tp_mult, sl_mult):
        tp_dist = snap.m5_atr * tp_mult - spread_offset
        sl_dist = snap.m5_atr * sl_mult - spread_offset
        if direction == 'LONG':
            return price + tp_dist, price - sl_dist
        else:
            return price - tp_dist, price + sl_dist

    # ==================== STRATEGY A: EMA Trend Pullback ====================
    # M5-only trigger: M5 trend alignment + M5 pullback to M5 EMA21 + M5 RSI reset
    if now - cooldowns.last_ema > COOLDOWN_SECS:
        m5_bull = snap.m5_ema9 > snap.m5_ema21 > snap.m5_ema50
        m5_bear = snap.m5_ema9 < snap.m5_ema21 < snap.m5_ema50

        full_dir = None
        if m5_bull and snap.m5_low <= snap.m5_ema21 and snap.m5_rsi <= 55:
            full_dir = 'LONG'
        elif m5_bear and snap.m5_high >= snap.m5_ema21 and snap.m5_rsi >= 45:
            full_dir = 'SHORT'

        if full_dir and not check_counter_trend(full_dir):
            cooldowns.last_ema = now
            tp, sl = calc_tp_sl(full_dir, EMA_TP_MULT, EMA_SL_MULT)
            meta = {
                'rule': f"M5 EMA {'9>21>50 BULL' if m5_bull else '9<21<50 BEAR'}, M5 pullback to EMA21, M5 RSI reset",
                'm1_rsi': round(snap.m1_rsi, 2),
                'm5_rsi': round(snap.m5_rsi, 2),
                'm5_ema9': round(snap.m5_ema9, 3),
                'm5_ema21': round(snap.m5_ema21, 3),
                'm5_ema50': round(snap.m5_ema50, 3),
                'm5_atr': round(snap.m5_atr, 3),
                'tp_mult': EMA_TP_MULT,
                'sl_mult': EMA_SL_MULT,
                'm5_trend': m5_trend_bias,
            }
            signals.append(Signal(
                strategy='EMA_PULLBACK', direction=full_dir,
                entry_price=price, tp=tp, sl=sl, meta=meta,
            ))

    # ==================== STRATEGY B: Bollinger Mean Reversion (M5) ====================
    if now - cooldowns.last_bb > COOLDOWN_SECS:
        full_dir = None
        bb_trigger = ''
        if snap.m5_high > snap.m5_upper_bb and snap.m5_rsi >= 70:
            full_dir = 'SHORT'
            bb_trigger = f"M5 High ({snap.m5_high:.3f}) > M5 Upper BB ({snap.m5_upper_bb:.3f}), M5 RSI {snap.m5_rsi:.1f} >= 70"
        elif snap.m5_low < snap.m5_lower_bb and snap.m5_rsi <= 30:
            full_dir = 'LONG'
            bb_trigger = f"M5 Low ({snap.m5_low:.3f}) < M5 Lower BB ({snap.m5_lower_bb:.3f}), M5 RSI {snap.m5_rsi:.1f} <= 30"

        if full_dir and not check_counter_trend(full_dir):
            cooldowns.last_bb = now
            tp, sl = calc_tp_sl(full_dir, BB_TP_MULT, BB_SL_MULT)
            meta = {
                'rule': bb_trigger,
                'm5_rsi': round(snap.m5_rsi, 2),
                'm5_upper_bb': round(snap.m5_upper_bb, 3),
                'm5_lower_bb': round(snap.m5_lower_bb, 3),
                'm5_atr': round(snap.m5_atr, 3),
                'tp_mult': BB_TP_MULT,
                'sl_mult': BB_SL_MULT,
                'm5_trend': m5_trend_bias,
            }
            signals.append(Signal(
                strategy='BB_REVERSION', direction=full_dir,
                entry_price=price, tp=tp, sl=sl, meta=meta,
            ))

    # ==================== STRATEGY C: Institutional Breakout ====================
    if now - cooldowns.last_inst > COOLDOWN_SECS:
        vol_sma = snap.m5_vol_sma20 if snap.m5_vol_sma20 > 0 else 1
        vol_ratio = snap.m5_volume / vol_sma

        full_dir = None
        if vol_ratio > 2.0:
            if snap.m5_close > snap.m5_upper_bb and snap.m1_low <= snap.m5_ema9:
                full_dir = 'LONG'
            elif snap.m5_close < snap.m5_lower_bb and snap.m1_high >= snap.m5_ema9:
                full_dir = 'SHORT'

        if full_dir and not check_counter_trend(full_dir):
            cooldowns.last_inst = now
            tp, sl = calc_tp_sl(full_dir, INST_TP_MULT, INST_SL_MULT)
            meta = {
                'rule': f"M5 Vol surge ({vol_ratio:.1f}x > 2.0x), M5 close {'above Upper BB' if full_dir == 'LONG' else 'below Lower BB'}, M1 pullback to M5 EMA9",
                'm5_volume': round(snap.m5_volume, 0),
                'm5_vol_ratio': round(vol_ratio, 2),
                'm5_close': round(snap.m5_close, 3),
                'm5_ema9': round(snap.m5_ema9, 3),
                'm5_atr': round(snap.m5_atr, 3),
                'tp_mult': INST_TP_MULT,
                'sl_mult': INST_SL_MULT,
                'm5_trend': m5_trend_bias,
            }
            signals.append(Signal(
                strategy='INST_BREAKOUT', direction=full_dir,
                entry_price=price, tp=tp, sl=sl, meta=meta,
            ))

    return signals
