import sys; sys.path.insert(0, "/app")
from pymongo import MongoClient
import pandas as pd
from datetime import datetime, timezone
from strategy import attach_indicators, resample_m5, COOLDOWN_SECS

db = MongoClient("mongodb://trading-db:27017/trading").get_database()

# Load ALL available candles
docs = list(db.candles.find({
    "symbol": "OANDA:XAU_USD", "interval": "1m"
}).sort("timestamp", 1))

df = pd.DataFrame(docs)
df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
df.set_index("timestamp", inplace=True)
df.sort_index(inplace=True)
if "tickVolume" in df.columns:
    df["volume"] = df["tickVolume"].where(df["tickVolume"] > 0, df.get("volume", 1))
df["volume"] = df["volume"].fillna(1).replace(0, 1)
df = attach_indicators(df)
df5 = resample_m5(df)
df5 = attach_indicators(df5)

print(f"Total M1 candles: {len(df)} | M5 bars: {len(df5)}")
print(f"Period: {df.index[0]} to {df.index[-1]}")

SPREAD = 0.50

# Test all 3 slots
slots = [
    ("A(3:1)", 3.0, 1.0),
    ("B(1.5:1)", 1.5, 1.0),
    ("C(1:1)", 1.0, 1.0),
]

for slot_label, tp_m, sl_m in slots:
    print(f"\n{'='*80}")
    print(f"=== REVERSED MODE — {slot_label} (TP {tp_m}×ATR / SL {sl_m}×ATR) ===")
    print(f"{'='*80}")
    
    trades = []
    last_t = 0
    
    for i in range(2, len(df5)):
        m5 = df5.iloc[i-1]
        bar_time = df5.index[i-1]
        ts = bar_time.timestamp()
        if ts - last_t <= COOLDOWN_SECS: continue
        e9 = m5["ema9"]; e21 = m5["ema21"]; e50 = m5["ema50"]
        rsi = m5["rsi"]; hi = m5["high"]; lo = m5["low"]; atr = m5["atr"]
        if atr < 0.05: continue
        bull = e9 > e21 > e50; bear = e9 < e21 < e50
        d = None
        if bull and lo <= e21 and rsi <= 55: d = "SHORT"
        elif bear and hi >= e21 and rsi >= 45: d = "LONG"
        if not d: continue
        last_t = ts
        m1 = df[df.index >= bar_time]
        if len(m1) == 0: continue
        price = float(m1.iloc[0]["close"])
        
        tp_dist = atr * tp_m - SPREAD
        sl_dist = atr * sl_m + SPREAD
        if d == "SHORT":
            tp = price - tp_dist; sl = price + sl_dist
        else:
            tp = price + tp_dist; sl = price - sl_dist
        
        future = df[df.index > bar_time]
        result = "OPEN"; pnl = 0
        for _, c in future.iterrows():
            if d == "SHORT":
                if c["low"] <= tp: result = "WIN"; pnl = tp_dist; break
                if c["high"] >= sl: result = "LOSS"; pnl = -sl_dist; break
            else:
                if c["high"] >= tp: result = "WIN"; pnl = tp_dist; break
                if c["low"] <= sl: result = "LOSS"; pnl = -sl_dist; break
        
        trades.append({"time": bar_time, "dir": d, "result": result, "pnl": pnl})
    
    # Daily breakdown
    by_day = {}
    for t in trades:
        day = t["time"].strftime("%Y-%m-%d (%a)")
        if day not in by_day:
            by_day[day] = {"w": 0, "l": 0, "o": 0, "pnl": 0, "shorts": 0, "longs": 0}
        if t["result"] == "WIN": by_day[day]["w"] += 1
        elif t["result"] == "LOSS": by_day[day]["l"] += 1
        else: by_day[day]["o"] += 1
        by_day[day]["pnl"] += t["pnl"]
        if t["dir"] == "SHORT": by_day[day]["shorts"] += 1
        else: by_day[day]["longs"] += 1
    
    total_pnl = 0
    losing_days = 0
    winning_days = 0
    
    print(f"\n  {'Date':<20} {'W':>4} {'L':>4} {'WR':>5} {'S':>3} {'L':>3} {'PnL':>10} {'Cumul':>10}")
    print(f"  {'-'*70}")
    
    for day in sorted(by_day.keys()):
        v = by_day[day]
        ct = v["w"] + v["l"]
        wr = f"{v['w']/ct*100:.0f}%" if ct > 0 else "N/A"
        total_pnl += v["pnl"]
        marker = " ❌" if v["pnl"] < 0 else " ✅"
        if v["pnl"] < 0: losing_days += 1
        else: winning_days += 1
        print(f"  {day:<20} {v['w']:>4} {v['l']:>4} {wr:>5} {v['shorts']:>3} {v['longs']:>3} ${v['pnl']:>9.2f} ${total_pnl:>9.2f}{marker}")
    
    total_trades = len(trades)
    total_wins = sum(1 for t in trades if t["result"] == "WIN")
    total_losses = sum(1 for t in trades if t["result"] == "LOSS")
    total_closed = total_wins + total_losses
    total_days = len(by_day)
    
    print(f"\n  SUMMARY:")
    print(f"  Total trades: {total_trades} | W:{total_wins} L:{total_losses} | WR: {total_wins/total_closed*100:.0f}%")
    print(f"  Total P/L: ${total_pnl:.2f}")
    print(f"  Winning days: {winning_days} | Losing days: {losing_days} | Win day rate: {winning_days/total_days*100:.0f}%")
    print(f"  Avg daily P/L: ${total_pnl/total_days:.2f}")
    if losing_days > 0:
        worst = min(by_day.values(), key=lambda v: v["pnl"])
        best = max(by_day.values(), key=lambda v: v["pnl"])
        print(f"  Worst day: ${worst['pnl']:.2f} | Best day: ${best['pnl']:.2f}")
