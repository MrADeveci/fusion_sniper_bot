"""Pre-registered test of the H4-trend + ATR-trailing-exit WRAPPER (Step 2).

The criteria this script applies were committed to bt_trend_trail.txt BEFORE it was ever
run (commit "lab: pre-register trend+trail hypothesis test"). This script only executes
them. It appends results to that file; it does not edit the pre-registration.

Run: python tools/backtest_trend_trail.py
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

ROOT = r"C:\fusion_sniper_bot"
sys.path.insert(0, ROOT)
from tools.backtest_mom import (Costs, Gates, bootstrap_p, build_strategy,   # noqa: E402
                                metrics, prepare, run_one)
from tools.news_calendar import build_blackouts                              # noqa: E402

OUT = os.path.join(ROOT, "bt_trend_trail.txt")

VARIANTS = [
    ("a) ALWAYS", "always"),
    ("b) FIRST_OF_SESSION", "first_of_session"),
    ("c) LONG_ONLY", "long_only"),
]

# Binding, from the pre-registration.
ELIG_EXPECTANCY = 0.0
ELIG_BOOT_P = 0.05
OOS_BOOT_P = 0.05
OOS_PERM_P = 0.05
N_BOOT = 10000
N_PERM = 200
PRIMARY_SLIP = 1.5

HDR = f"{'':<24}{'trades':>7}{'win%':>7}{'net':>9}{'PF':>7}{'exp':>8}{'maxDD':>9}{'boot p':>9}"


def _fmt(m, p=None):
    pf = "inf" if m["pf"] == float("inf") else f"{m['pf']:.3f}"
    s = (f"{m['trades']:>7}{m['win']:>7.1f}{m['net']:>9.0f}{pf:>7}"
         f"{m['exp']:>8.2f}{m['dd']:>9.0f}")
    return s + (f"{p:>9.4f}" if p is not None else f"{'':>9}")


def side_and_year(trades):
    """Long/short split and per-year net -- mandatory reporting."""
    if not trades:
        return [], []
    df = pd.DataFrame(trades)
    df["year"] = pd.to_datetime(df["exit_time"]).dt.year
    sides = []
    for d in ("BUY", "SELL"):
        s = df[df.direction == d]
        if len(s):
            sides.append((d, len(s), float(s["net_pnl"].sum()),
                          100.0 * (s["net_pnl"] > 0).mean()))
        else:
            sides.append((d, 0, 0.0, 0.0))
    years = [(int(y), int(len(g)), float(g["net_pnl"].sum()))
             for y, g in df.groupby("year")]
    return sides, years


def emit_side_year(emit, trades, indent="      "):
    sides, years = side_and_year(trades)
    for d, n, net, wr in sides:
        emit(f"{indent}{d:<5} {n:>5} trades  net GBP {net:>9.0f}  win {wr:>5.1f}%")
    emit(f"{indent}per-year net: " + "  ".join(f"{y}: {net:+.0f} ({n})" for y, n, net in years))


def main():
    with open(os.path.join(ROOT, "config.json")) as f:
        cfg = json.load(f)
    strat = build_strategy(cfg)
    blackouts = build_blackouts()
    gates = Gates.live(cfg, blackouts)
    costs = Costs(swap=True, slip_mult=PRIMARY_SLIP)

    prep, h4_ce, h4_trend = prepare(strat, legacy=False)
    p_is = [p for p in prep if p["tag"] == "IS"][0]
    p_oos = [p for p in prep if p["tag"] == "OOS"][0]

    out = []

    def emit(line=""):
        print(line, flush=True)
        out.append(line)

    emit("")
    emit("=" * 96)
    emit("RESULTS  (appended after the pre-registration above; criteria applied as written)")
    emit("=" * 96)
    emit("")

    # ---------------- STAGE 1: IN-SAMPLE -----------------------------------
    emit("-" * 96)
    emit("STAGE 1 -- IN-SAMPLE 2021-01-01..2023-12-31, slippage 1.5x, full live gates")
    emit("-" * 96)
    emit(HDR)
    is_res = {}
    for label, mode in VARIANTS:
        bt = run_one(strat, p_is, h4_ce, h4_trend, costs, gates, entry_mode=mode)
        m = metrics(bt.trades)
        p, ci = bootstrap_p([t["net_pnl"] for t in bt.trades], n=N_BOOT)
        is_res[mode] = dict(label=label, m=m, p=p, ci=ci, trades=bt.trades)
        emit(f"{label:<24}" + _fmt(m, p))
    emit("")
    for label, mode in VARIANTS:
        r = is_res[mode]
        emit(f"  {label}")
        emit_side_year(emit, r["trades"])
        emit(f"      bootstrap 95% CI on net: [GBP {r['ci'][0]:.0f}, GBP {r['ci'][1]:.0f}]")
    emit("")

    # ---------------- ELIGIBILITY GATE (as pre-registered) ------------------
    emit("-" * 96)
    emit("ELIGIBILITY GATE (pre-registered): expectancy > 0 AND bootstrap P(net<=0) < 0.05")
    emit("-" * 96)
    eligible = []
    for label, mode in VARIANTS:
        r = is_res[mode]
        e1 = r["m"]["exp"] > ELIG_EXPECTANCY
        e2 = r["p"] < ELIG_BOOT_P
        ok = e1 and e2
        if ok:
            eligible.append(mode)
        emit(f"  {label:<24} expectancy {r['m']['exp']:>7.2f} [{'PASS' if e1 else 'FAIL'}]   "
             f"boot p {r['p']:.4f} [{'PASS' if e2 else 'FAIL'}]   -> "
             f"{'ELIGIBLE' if ok else 'NOT ELIGIBLE'}")
    emit("")

    if not eligible:
        emit("=" * 96)
        emit("OUTCOME: NO VARIANT PASSED THE IN-SAMPLE ELIGIBILITY GATE.")
        emit("=" * 96)
        emit("")
        emit("Per the pre-registered REJECTION BRANCH: H1 is REJECTED at the in-sample stage.")
        emit("The out-of-sample window 2024-2026 is NOT touched and remains unspent.")
        emit("No variant is promoted. No parameter is tuned. The experiment ends here.")
        emit("")
        emit("The H4-trend + ATR-trailing-exit wrapper does not show positive expectancy on")
        emit("2021-2023 under realistic conditions. Combined with Step 1 (the breakout entry")
        emit("adds nothing over random entries), there is no evidence of an edge anywhere in")
        emit("this strategy -- neither in the signal nor in the wrapper.")
        emit("")
        _sensitivity(emit, strat, p_is, h4_ce, h4_trend, gates, "IN-SAMPLE")
        _write(out)
        return

    # SELECTION: lowest bootstrap p; tie-break (< 0.01) on expectancy
    eligible.sort(key=lambda mo: (round(is_res[mo]["p"], 2), -is_res[mo]["m"]["exp"]))
    winner = eligible[0]
    emit(f"  ELIGIBLE: {', '.join(is_res[m]['label'] for m in eligible)}")
    emit(f"  SELECTED (lowest bootstrap p): {is_res[winner]['label']}")
    emit("  The other variants never touch OOS.")
    emit("")

    _sensitivity(emit, strat, p_is, h4_ce, h4_trend, gates, "IN-SAMPLE", only=winner)

    # ---------------- STAGE 2: OOS (one run) --------------------------------
    emit("-" * 96)
    emit(f"STAGE 2 -- OUT-OF-SAMPLE 2024-01-01..2026-06-05: {is_res[winner]['label']} ONLY")
    emit("-" * 96)
    bt = run_one(strat, p_oos, h4_ce, h4_trend, costs, gates, entry_mode=winner)
    m = metrics(bt.trades)
    pnls = [t["net_pnl"] for t in bt.trades]
    p, ci = bootstrap_p(pnls, n=N_BOOT)
    emit(HDR)
    emit(f"{is_res[winner]['label']:<24}" + _fmt(m, p))
    emit("")
    emit_side_year(emit, bt.trades, indent="  ")
    emit(f"  bootstrap ({N_BOOT:,}): P(net <= 0) = {p:.4f}   95% CI [GBP {ci[0]:.0f}, GBP {ci[1]:.0f}]")
    emit("")

    # permutation: SHUFFLE THE TREND FLAG (not the timing)
    emit(f"  permutation ({N_PERM} shuffles): the per-H4-bar trend series is SHUFFLED,")
    emit("  preserving its +1/-1/0 mix but destroying its relationship to price. Asks: does")
    emit("  trading the ACTUAL H4 trend beat a random direction with the same long/short mix?")
    rng = np.random.default_rng(23)
    nets = []
    for i in range(N_PERM):
        sh = h4_trend.copy()
        rng.shuffle(sh)
        b = run_one(strat, p_oos, h4_ce, sh, costs, gates, entry_mode=winner)
        nets.append(metrics(b.trades)["net"])
        if (i + 1) % 50 == 0:
            print(f"    permutation {i+1}/{N_PERM} ...", flush=True)
    nets = np.array(nets)
    perm_p = float((nets >= m["net"]).mean())
    emit(f"      shuffled-trend net GBP : mean {nets.mean():.0f}, sd {nets.std():.0f}, "
         f"median {np.median(nets):.0f}")
    emit(f"      range                  : [{nets.min():.0f}, {nets.max():.0f}]")
    emit(f"      ACTUAL trend net       : {m['net']:.0f} GBP")
    emit(f"      P(shuffled >= actual)  : {perm_p:.4f}")
    emit("")

    _sensitivity(emit, strat, p_oos, h4_ce, h4_trend, gates, "OUT-OF-SAMPLE", only=winner)

    # ---------------- VERDICT (pre-registered O1/O2/O3) ---------------------
    o1 = m["net"] > 0
    o2 = p < OOS_BOOT_P
    o3 = perm_p < OOS_PERM_P
    emit("=" * 96)
    emit("VERDICT -- pre-registered criteria O1/O2/O3, applied as written")
    emit("=" * 96)
    emit(f"  O1  net P&L > 0                        : {m['net']:.0f} GBP     [{'PASS' if o1 else 'FAIL'}]")
    emit(f"  O2  bootstrap P(net <= 0) < 0.05       : {p:.4f}        [{'PASS' if o2 else 'FAIL'}]")
    emit(f"  O3  permutation P(shuf >= actual)<0.05 : {perm_p:.4f}        [{'PASS' if o3 else 'FAIL'}]")
    emit("")
    if o1 and o2 and o3:
        emit("  H1 SUPPORTED: all three criteria pass.")
    else:
        emit("  H1 NOT SUPPORTED. Per the pre-registration there is no partial credit and no")
        emit("  'promising, needs tuning' verdict. The wrapper is not shown to have an edge.")
    emit("")
    _write(out)


def _sensitivity(emit, strat, p, h4_ce, h4_trend, gates, tag, only=None):
    emit("-" * 96)
    emit(f"SLIPPAGE SENSITIVITY ({tag}) -- reported only; MUST NOT influence selection")
    emit("-" * 96)
    emit(f"{'':<24}" + "".join(f"{s:>12}" for s in ("0.0x", "1.0x", "1.5x", "2.0x")))
    for label, mode in VARIANTS:
        if only and mode != only:
            continue
        row = []
        for sm in (0.0, 1.0, 1.5, 2.0):
            b = run_one(strat, p, h4_ce, h4_trend, Costs(swap=True, slip_mult=sm),
                        gates, entry_mode=mode)
            row.append(metrics(b.trades)["net"])
        emit(f"{label:<24}" + "".join(f"{v:>12.0f}" for v in row))
    emit("")


def _write(lines):
    with open(OUT, "a") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nappended -> {OUT}")


if __name__ == "__main__":
    main()
