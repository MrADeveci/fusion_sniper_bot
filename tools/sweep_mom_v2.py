"""
Robustness sweep for momentum Variant 2 (trailing). LAB, offline, no trades.

NOT an optimisation: vary ONE parameter at a time around its default, hold the rest at
default, and report NET expectancy/trade, NET profit factor and trade count for BOTH
in-sample (2021-2023) and out-of-sample (2024-2026-06). The point is to see whether the
positive net expectancy is a broad plateau or a fragile spike. Nothing is tuned or selected.

Defaults: breakout lookback 20, H4 EMA 50, trail distance 3.0xATR, SL 1.5xATR, trail
activation 1.0xATR.  Session London/NY 07:00-18:00 UK, risk GBP 50/trade, 5.52/lot + spread.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

ROOT = r"C:\fusion_sniper_bot"
sys.path.insert(0, ROOT)
from tools.backtest import load_csv, simple_atr_series, M1_CSV          # noqa: E402
from tools.backtest_htf import resample                                 # noqa: E402

OFFSET_H = 2
GBPUSD = 1.34
COMMISSION = 5.52
RISK_GBP = 50.0
CONTRACT = 100.0
POINT = 0.01

DEF = dict(lookback=20, ema=50, trail=3.0, sl=1.5, act=1.0)


def run_v2(win, prior_high, prior_low, h4_ce, h4_close, h4_ema, ema_min,
           sl_mult, trail_dist, trail_act):
    o, h, l, c, ep, sp, atr, uk = win["o"], win["h"], win["l"], win["c"], win["ep"], win["sp"], win["atr"], win["uk"]
    s0, e0 = win["start"], win["end"]
    N = len(o)
    pos = None
    pending = None
    nets = []
    for j in range(N):
        if pending is not None and pos is None:
            entry = round(float(o[j]), 2); a = pending["atr"]; d = pending["dir"]
            sl = round(entry - sl_mult * a, 2) if d == "BUY" else round(entry + sl_mult * a, 2)
            pos = {"dir": d, "entry": entry, "sl": sl, "atr": a, "lots": pending["lots"],
                   "sp": float(sp[j]), "run": float(h[j] if d == "BUY" else l[j])}
        pending = None

        if pos is not None:
            d = pos["dir"]; exit_px = None
            if d == "BUY":
                if l[j] <= pos["sl"]:
                    exit_px = o[j] if o[j] <= pos["sl"] else pos["sl"]
            else:
                if h[j] >= pos["sl"]:
                    exit_px = o[j] if o[j] >= pos["sl"] else pos["sl"]
            if exit_px is not None:
                move = (exit_px - pos["entry"]) if d == "BUY" else (pos["entry"] - exit_px)
                gross = move * CONTRACT * pos["lots"] / GBPUSD
                costs = COMMISSION * pos["lots"] + pos["sp"] * POINT * CONTRACT * pos["lots"] / GBPUSD
                nets.append(gross - costs); pos = None
            else:
                a = pos["atr"]
                if d == "BUY":
                    if h[j] > pos["run"]:
                        pos["run"] = float(h[j])
                    if pos["run"] >= pos["entry"] + trail_act * a:
                        cand = round(pos["run"] - trail_dist * a, 2)
                        if cand > pos["sl"]:
                            pos["sl"] = cand
                else:
                    if l[j] < pos["run"]:
                        pos["run"] = float(l[j])
                    if pos["run"] <= pos["entry"] - trail_act * a:
                        cand = round(pos["run"] + trail_dist * a, 2)
                        if cand < pos["sl"]:
                            pos["sl"] = cand

        if pos is None and pending is None and j + 1 < N:
            if not (s0 <= ep[j] <= e0):
                continue
            if not (7 <= uk[j + 1] < 18):
                continue
            a = atr[j]
            if a != a or j < 45:
                continue
            ph = prior_high[j]; pl = prior_low[j]
            if ph != ph or pl != pl:
                continue
            ev = int(ep[j + 1])
            hi = np.searchsorted(h4_ce, ev, side="right") - 1
            if hi < ema_min or h4_ema[hi] != h4_ema[hi]:
                continue
            up = h4_close[hi] > h4_ema[hi]; dn = h4_close[hi] < h4_ema[hi]
            direction = "BUY" if (up and c[j] > ph) else ("SELL" if (dn and c[j] < pl) else None)
            if direction is None:
                continue
            lots = max(0.01, round((RISK_GBP * GBPUSD / (sl_mult * a * CONTRACT)) / 0.01) * 0.01)
            pending = {"dir": direction, "atr": a, "lots": round(lots, 2)}

    if pos is not None:
        d = pos["dir"]; exit_px = c[N - 1]
        move = (exit_px - pos["entry"]) if d == "BUY" else (pos["entry"] - exit_px)
        gross = move * CONTRACT * pos["lots"] / GBPUSD
        costs = COMMISSION * pos["lots"] + pos["sp"] * POINT * CONTRACT * pos["lots"] / GBPUSD
        nets.append(gross - costs)
    return np.array(nets)


def metrics(nets):
    if len(nets) == 0:
        return float("nan"), float("nan"), 0
    pos = nets[nets > 0].sum(); neg = -nets[nets < 0].sum()
    pf = pos / neg if neg > 0 else float("inf")
    return nets.mean(), pf, len(nets)


def main():
    print("Loading + resampling ...", flush=True)
    m1 = load_csv(M1_CSV)
    m1 = m1[m1["time"] >= pd.Timestamp("2020-06-01")].set_index("time")
    m15 = resample(m1, "15min")
    h4 = resample(m1, "4h")
    idx = pd.DatetimeIndex(m15["time"]).tz_localize(timezone(timedelta(hours=OFFSET_H)))
    m15["ukhour"] = idx.tz_convert("Europe/London").hour
    h4_ce = h4["epoch"].to_numpy() + 4 * 3600
    h4_close = h4["close"].to_numpy(float)
    h4c_series = h4["close"]

    # window slices (with 45d buffer); arrays cached
    wins = {}
    for tag, s, e in [("IS", "2021-01-01", "2023-12-31"), ("OOS", "2024-01-01", "2026-06-05")]:
        s0 = int(datetime.fromisoformat(s).timestamp()) + OFFSET_H * 3600
        e0 = int((datetime.fromisoformat(e) + timedelta(days=1)).timestamp()) + OFFSET_H * 3600
        buf = s0 - 45 * 86400
        w = m15[(m15["epoch"] >= buf) & (m15["epoch"] <= e0 + 86400)].reset_index(drop=True)
        wins[tag] = dict(
            o=w["open"].to_numpy(float), h=w["high"].to_numpy(float),
            l=w["low"].to_numpy(float), c=w["close"].to_numpy(float),
            ep=w["epoch"].to_numpy(), sp=w["spread"].to_numpy(float),
            atr=simple_atr_series(w, 14), uk=w["ukhour"].to_numpy(),
            start=s0, end=e0, hi=w["high"], lo=w["low"])

    ema_cache = {}
    def get_ema(p):
        if p not in ema_cache:
            ema_cache[p] = h4c_series.ewm(span=p, adjust=False).mean().to_numpy(float)
        return ema_cache[p]

    prior_cache = {}
    def get_prior(tag, lb):
        key = (tag, lb)
        if key not in prior_cache:
            w = wins[tag]
            ph = w["hi"].rolling(lb).max().shift(1).to_numpy(float)
            pl = w["lo"].rolling(lb).min().shift(1).to_numpy(float)
            prior_cache[key] = (ph, pl)
        return prior_cache[key]

    def one(tag, lookback, ema, trail, sl, act):
        ph, pl = get_prior(tag, lookback)
        em = get_ema(ema)
        nets = run_v2(wins[tag], ph, pl, h4_ce, h4_close, em, ema, sl, trail, act)
        return metrics(nets)

    sweeps = [
        ("Breakout lookback", "lookback", [10, 15, 20, 30, 40]),
        ("H4 trend EMA", "ema", [30, 50, 100]),
        ("Trail distance (xATR)", "trail", [2.0, 2.5, 3.0, 3.5, 4.0]),
        ("Stop loss (xATR)", "sl", [1.0, 1.5, 2.0]),
        ("Trail activation (xATR)", "act", [0.5, 1.0, 1.5]),
    ]

    print(f"\nDefaults: {DEF}\n")
    for title, knob, values in sweeps:
        print(f"==== {title} (default {DEF[knob]}) ====")
        print(f"  {'value':>7} | {'IS netExp':>9} {'IS PF':>6} {'IS n':>5} | {'OOS netExp':>10} {'OOS PF':>7} {'OOS n':>6}")
        for v in values:
            p = dict(DEF); p[knob] = v
            ie, ipf, ino = one("IS", p["lookback"], p["ema"], p["trail"], p["sl"], p["act"])
            oe, opf, ono = one("OOS", p["lookback"], p["ema"], p["trail"], p["sl"], p["act"])
            star = " <-default" if v == DEF[knob] else ""
            print(f"  {str(v):>7} | {ie:>9.2f} {ipf:>6.3f} {ino:>5} | {oe:>10.2f} {opf:>7.3f} {ono:>6}{star}")
        print()


if __name__ == "__main__":
    main()
