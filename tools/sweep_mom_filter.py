"""
Stress the single load-bearing axis: the H4 trend filter, on momentum Variant 2.
LAB, offline, no trades. Not an optimisation: map the surface, tune nothing.

Variant 2 held at default: M15 20-bar breakout, SL 1.5xATR, trailing 3.0xATR (act 1.0),
London/NY 07:00-18:00 UK, risk GBP 50, 5.52/lot + spread.

  Part 1: sweep the H4 trend EMA period: 40,45,50,55,60,70,80 (filter = H4 close vs that EMA).
  Part 2: alternative filters at default trail/SL/lookback:
          (a) H4 close vs its 200-EMA
          (b) H4 50-EMA vs H4 200-EMA (classic regime filter)
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

ROOT = r"C:\fusion_sniper_bot"
sys.path.insert(0, ROOT)
from tools.backtest import load_csv, simple_atr_series, M1_CSV          # noqa: E402
from tools.backtest_htf import resample                                 # noqa: E402

OFFSET_H = 2; GBPUSD = 1.34; COMMISSION = 5.52; RISK_GBP = 50.0
CONTRACT = 100.0; POINT = 0.01
LOOKBACK = 20; SL = 1.5; TRAIL = 3.0; ACT = 1.0     # Variant 2 defaults


def run(win, prior_high, prior_low, h4_ce, h4_trend, h4_min):
    o, h, l, c, ep, sp, atr, uk = (win["o"], win["h"], win["l"], win["c"],
                                   win["ep"], win["sp"], win["atr"], win["uk"])
    s0, e0 = win["start"], win["end"]
    N = len(o); pos = None; pending = None; nets = []
    for j in range(N):
        if pending is not None and pos is None:
            entry = round(float(o[j]), 2); a = pending["atr"]; d = pending["dir"]
            sl = round(entry - SL * a, 2) if d == "BUY" else round(entry + SL * a, 2)
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
                    if pos["run"] >= pos["entry"] + ACT * a:
                        cand = round(pos["run"] - TRAIL * a, 2)
                        if cand > pos["sl"]:
                            pos["sl"] = cand
                else:
                    if l[j] < pos["run"]:
                        pos["run"] = float(l[j])
                    if pos["run"] <= pos["entry"] - ACT * a:
                        cand = round(pos["run"] + TRAIL * a, 2)
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
            hi = np.searchsorted(h4_ce, int(ep[j + 1]), side="right") - 1
            if hi < h4_min:
                continue
            tr = h4_trend[hi]
            direction = "BUY" if (tr > 0 and c[j] > ph) else ("SELL" if (tr < 0 and c[j] < pl) else None)
            if direction is None:
                continue
            lots = max(0.01, round((RISK_GBP * GBPUSD / (SL * a * CONTRACT)) / 0.01) * 0.01)
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
    p = nets[nets > 0].sum(); n = -nets[nets < 0].sum()
    return nets.mean(), (p / n if n > 0 else float("inf")), len(nets)


def main():
    print("Loading + resampling ...", flush=True)
    m1 = load_csv(M1_CSV)
    m1 = m1[m1["time"] >= pd.Timestamp("2020-06-01")].set_index("time")
    m15 = resample(m1, "15min"); h4 = resample(m1, "4h")
    idx = pd.DatetimeIndex(m15["time"]).tz_localize(timezone(timedelta(hours=OFFSET_H)))
    m15["ukhour"] = idx.tz_convert("Europe/London").hour
    h4_ce = h4["epoch"].to_numpy() + 4 * 3600
    h4_close = h4["close"].to_numpy(float)

    def ema(p):
        return h4["close"].ewm(span=p, adjust=False).mean().to_numpy(float)

    wins = {}
    for tag, s, e in [("IS", "2021-01-01", "2023-12-31"), ("OOS", "2024-01-01", "2026-06-05")]:
        s0 = int(datetime.fromisoformat(s).timestamp()) + OFFSET_H * 3600
        e0 = int((datetime.fromisoformat(e) + timedelta(days=1)).timestamp()) + OFFSET_H * 3600
        buf = s0 - 45 * 86400
        w = m15[(m15["epoch"] >= buf) & (m15["epoch"] <= e0 + 86400)].reset_index(drop=True)
        ph = w["high"].rolling(LOOKBACK).max().shift(1).to_numpy(float)
        pl = w["low"].rolling(LOOKBACK).min().shift(1).to_numpy(float)
        wins[tag] = dict(o=w["open"].to_numpy(float), h=w["high"].to_numpy(float),
                         l=w["low"].to_numpy(float), c=w["close"].to_numpy(float),
                         ep=w["epoch"].to_numpy(), sp=w["spread"].to_numpy(float),
                         atr=simple_atr_series(w, 14), uk=w["ukhour"].to_numpy(),
                         start=s0, end=e0, ph=ph, pl=pl)

    def evaluate(trend, h4_min):
        out = {}
        for tag in ("IS", "OOS"):
            nets = run(wins[tag], wins[tag]["ph"], wins[tag]["pl"], h4_ce, trend, h4_min)
            out[tag] = metrics(nets)
        return out

    print("\n#### Part 1: H4 trend = close vs EMA(period) ####")
    print(f"  {'EMA':>5} | {'IS netExp':>9} {'IS PF':>6} {'IS n':>5} | {'OOS netExp':>10} {'OOS PF':>7} {'OOS n':>6}")
    for p in [40, 45, 50, 55, 60, 70, 80]:
        trend = np.sign(h4_close - ema(p))
        r = evaluate(trend, p)
        star = " <-prev default" if p == 50 else ""
        print(f"  {p:>5} | {r['IS'][0]:>9.2f} {r['IS'][1]:>6.3f} {r['IS'][2]:>5} | "
              f"{r['OOS'][0]:>10.2f} {r['OOS'][1]:>7.3f} {r['OOS'][2]:>6}{star}")

    print("\n#### Part 2: alternative trend filters ####")
    print(f"  {'filter':<22} | {'IS netExp':>9} {'IS PF':>6} {'IS n':>5} | {'OOS netExp':>10} {'OOS PF':>7} {'OOS n':>6}")
    e200 = ema(200); e50 = ema(50)
    for name, trend in [("(a) close vs EMA200", np.sign(h4_close - e200)),
                        ("(b) EMA50 vs EMA200", np.sign(e50 - e200))]:
        r = evaluate(trend, 200)
        print(f"  {name:<22} | {r['IS'][0]:>9.2f} {r['IS'][1]:>6.3f} {r['IS'][2]:>5} | "
              f"{r['OOS'][0]:>10.2f} {r['OOS'][1]:>7.3f} {r['OOS'][2]:>6}")


if __name__ == "__main__":
    main()
