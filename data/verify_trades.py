"""
Verification script: Loads chart_trades.json and verifies every single trade
strictly follows the documented strategy conditions from XAUUSD_STRATEGIES.md.
"""
import json
import sys

def verify(dataset_id='202603'):
    fname = f"chart_trades_{dataset_id}.json"
    with open(fname) as f:
        trades = json.load(f)

    total = len(trades)
    passed = 0
    failed = 0
    errors = []

    for idx, t in enumerate(trades):
        m = t.get('meta', {})
        strat = t['strat']
        d = t['dir']
        ok = True
        reasons = []

        # Global: must have meta
        if not m:
            reasons.append("Missing meta snapshot")
            ok = False

        if strat == 'EMA_PULLBACK':
            # Condition 1: M5 EMA alignment
            if d == 'LONG':
                if not (m.get('m5_ema9', 0) > m.get('m5_ema21', 0) > m.get('m5_ema50', 0)):
                    reasons.append(f"M5 EMA not bullish: ema9={m.get('m5_ema9')} ema21={m.get('m5_ema21')} ema50={m.get('m5_ema50')}")
                    ok = False
            elif d == 'SHORT':
                if not (m.get('m5_ema9', 999) < m.get('m5_ema21', 999) < m.get('m5_ema50', 999)):
                    reasons.append(f"M5 EMA not bearish: ema9={m.get('m5_ema9')} ema21={m.get('m5_ema21')} ema50={m.get('m5_ema50')}")
                    ok = False

            # Condition 3: RSI filter
            if d == 'LONG' and m.get('m1_rsi', 100) > 45:
                reasons.append(f"M1 RSI not <= 45: rsi={m.get('m1_rsi')}")
                ok = False
            elif d == 'SHORT' and m.get('m1_rsi', 0) < 55:
                reasons.append(f"M1 RSI not >= 55: rsi={m.get('m1_rsi')}")
                ok = False

            # TP/SL multipliers
            if m.get('tp_mult') != 3.0:
                reasons.append(f"TP mult not 3.0: {m.get('tp_mult')}")
                ok = False
            if m.get('sl_mult') != 1.2:
                reasons.append(f"SL mult not 1.2: {m.get('sl_mult')}")
                ok = False

        elif strat == 'BB_REVERSION':
            # Condition: BB pierce + RSI extreme
            if d == 'SHORT':
                if m.get('m1_rsi', 0) < 75:
                    reasons.append(f"M1 RSI not >= 75: rsi={m.get('m1_rsi')}")
                    ok = False
            elif d == 'LONG':
                if m.get('m1_rsi', 100) > 25:
                    reasons.append(f"M1 RSI not <= 25: rsi={m.get('m1_rsi')}")
                    ok = False

            # TP/SL multipliers
            if m.get('tp_mult') != 2.0:
                reasons.append(f"TP mult not 2.0: {m.get('tp_mult')}")
                ok = False
            if m.get('sl_mult') != 1.5:
                reasons.append(f"SL mult not 1.5: {m.get('sl_mult')}")
                ok = False

        elif strat == 'INST_BREAKOUT':
            # Condition: Volume surge > 200%
            if m.get('m5_vol_ratio', 0) <= 2.0:
                reasons.append(f"Vol ratio not > 2.0: {m.get('m5_vol_ratio')}")
                ok = False

            # Condition: M5 close outside BB
            if d == 'LONG':
                if m.get('m5_close', 0) <= m.get('m5_upper_bb', 999):
                    reasons.append(f"M5 close not > upper BB: close={m.get('m5_close')} upper={m.get('m5_upper_bb')}")
                    ok = False
            elif d == 'SHORT':
                if m.get('m5_close', 999) >= m.get('m5_lower_bb', 0):
                    reasons.append(f"M5 close not < lower BB: close={m.get('m5_close')} lower={m.get('m5_lower_bb')}")
                    ok = False

            # TP/SL multipliers
            if m.get('tp_mult') != 4.0:
                reasons.append(f"TP mult not 4.0: {m.get('tp_mult')}")
                ok = False
            if m.get('sl_mult') != 1.0:
                reasons.append(f"SL mult not 1.0: {m.get('sl_mult')}")
                ok = False

        # Validate TP/SL prices match the formula
        entry = t['entry']
        m5_atr = m.get('m5_atr', m.get('m1_atr', 0))
        tp_m = m.get('tp_mult', 0)
        sl_m = m.get('sl_mult', 0)

        if tp_m > 0 and m5_atr > 0:
            expected_tp = entry + (m5_atr * tp_m) if d == 'LONG' else entry - (m5_atr * tp_m)
            if abs(t['tp'] - expected_tp) > 0.01:
                reasons.append(f"TP mismatch: expected={round(expected_tp,3)} got={t['tp']}")
                ok = False

            expected_sl = entry - (m5_atr * sl_m) if d == 'LONG' else entry + (m5_atr * sl_m)
            if abs(t['sl'] - expected_sl) > 0.01:
                reasons.append(f"SL mismatch: expected={round(expected_sl,3)} got={t['sl']}")
                ok = False

        # Validate exit status coherence
        if t['status'] == 'CLOSED_TP':
            if d == 'LONG' and t['exit'] < t['entry']:
                reasons.append(f"TP exit below entry for LONG")
                ok = False
            if d == 'SHORT' and t['exit'] > t['entry']:
                reasons.append(f"TP exit above entry for SHORT")
                ok = False
        elif t['status'] == 'CLOSED_SL':
            if d == 'LONG' and t['exit'] > t['entry']:
                reasons.append(f"SL exit above entry for LONG")
                ok = False
            if d == 'SHORT' and t['exit'] < t['entry']:
                reasons.append(f"SL exit below entry for SHORT")
                ok = False

        if ok:
            passed += 1
        else:
            failed += 1
            errors.append((idx, t['strat'], t['dir'], reasons))

    print("=" * 60)
    print("    STRATEGY COMPLIANCE VERIFICATION REPORT")
    print("=" * 60)
    print(f"Total Trades Audited: {total}")
    print(f"✅ PASSED: {passed}")
    print(f"❌ FAILED: {failed}")
    print()

    if errors:
        print("--- FAILURES ---")
        for idx, strat, d, reasons in errors[:20]:  # Show first 20
            print(f"  Trade #{idx} [{strat}] [{d}]:")
            for r in reasons:
                print(f"    ❌ {r}")
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more failures")
    else:
        print("🏆 All trades strictly comply with documented strategy conditions.")

    return 0 if failed == 0 else 1

if __name__ == "__main__":
    ds = sys.argv[1] if len(sys.argv) > 1 else 'all'
    
    if ds == 'all':
        total_fail = 0
        for d in ['202601', '202602', '202603']:
            print(f"\n--- Verifying dataset {d} ---")
            total_fail += verify(d)
        sys.exit(1 if total_fail > 0 else 0)
    else:
        sys.exit(verify(ds))
