import sys, os
sys.path.insert(0, os.path.abspath("services/backtester"))
from server import run_dryrun, _dryrun_state
import threading, time

run_dryrun("2026-04-05", "2026-04-05", "reverse", "ALL")

trades = _dryrun_state['result']['trades']
shorts = [t for t in trades if t['direction'] == 'SHORT' and t['slot'] == 'A']
print(f"Total shorts in Slot A: {len(shorts)}")
for i in range(1, len(shorts)):
    dist = shorts[i]['limit_price'] - shorts[i-1]['limit_price']
    print(f"Trade {i} -> {i+1}: limit1={shorts[i-1]['limit_price']}, limit2={shorts[i]['limit_price']}, dist={dist:.2f}, atr={shorts[i]['atr']}, req={shorts[i]['atr']*0.30:.2f}")
