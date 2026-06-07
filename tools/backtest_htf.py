"""
Higher-timeframe SMC base experiment (LAB, offline, no trades).

Clean base, minimal knobs:
  - Bias timeframe : H4  (CHoCH structure bias)
  - Entry timeframe: M15 (FVG retest entries, ONLY in the H4 bias direction)
  - ATR for stops  : M15 (simple, period 14)
  - Exits          : FIXED ONLY. SL = 1.5 x ATR, TP = 3.0 x ATR (1:2 R). No scalp,
                     no breakeven, no trailing.
  - Session filter : enter only 07:00-18:00 UK time (Europe/London, DST aware).
  - Sizing         : risk-based, flat GBP risk per trade, lots = risk*gbpusd /
                     (1.5*ATR*contract_size), snapped to 0.01, min 0.01.
  - Costs          : 5.52 GBP/lot round-turn commission + per-bar spread.

REUSES modules/strategy.py FusionStrategy for CHoCH bias + FVG entries (not
reimplemented). Single position at a time. Data is resampled from data/XAUUSD_M1.csv;
the M15 resample is sanity checked against data/XAUUSD_M15.csv.

NOTE: with risk-based 1:2 sizing every loss is ~ -risk and every win ~ +2*risk gross,
so the gross result is essentially a function of win rate (breakeven ~33.3%).
"""
from __future__ import annotations

import os
import sys
import datetime as dt
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

ROOT = r"C:\fusion_sniper_bot"
sys.path.insert(0, ROOT)
from modules.strategy import FusionStrategy            # noqa: E402 (reused)
from tools.backtest import load_csv, simple_atr_series, M1_CSV, M15_CSV  # noqa: E402

import json

CONTRACT_SIZE = 100.0
POINT = 0.01
OFFSET_H = 2          # broker server = UTC + 2 (config broker_timezone_offset)
GBPUSD = 1.34         # constant; cancels out of gross for risk-based sizing
COMMISSION = 5.52     # GBP per lot per round turn (confirmed from deal history)
RISK_GBP = 50.0
SL_MULT = 1.5
TP_MULT = 3.0
SESSION_START_UK = 7
SESSION_END_UK = 18   # enter while 7 <= uk_hour < 18
W = 200               # M15 window passed to analyze


# ---- swappable position sizing ----
def position_size(signal, atr, cfg):
    stop_dist = SL_MULT * atr               # price distance of the stop
    if stop_dist <= 0:
        return 0.01
    lots = RISK_GBP * GBPUSD / (stop_dist * CONTRACT_SIZE)
    lots = round(lots / 0.01) * 0.01
    return max(0.01, round(lots, 2))


def resample(m1, rule):
    agg = {"open": "first", "high": "max", "low": "min", "close": "last",
           "spread": "mean", "tick_volume": "sum"}
    r = m1.resample(rule, label="left", closed="left").agg(agg).dropna(subset=["open"])
    r = r.reset_index()
    r["epoch"] = (r["time"].astype("int64") // 10**9).astype(np.int64)
    return r


def sanity_check_m15(m15_resampled):
    broker = load_csv(M15_CSV)[["time", "open", "high", "low", "close"]]
    mine = m15_resampled[["time", "open", "high", "low", "close"]]
    merged = mine.merge(broker, on="time", suffixes=("_me", "_bk"))
    merged = merged[merged["time"] >= pd.Timestamp("2020-06-01")]
    n = len(merged)
    dc = (merged["close_me"] - merged["close_bk"]).abs()
    dh = (merged["high_me"] - merged["high_bk"]).abs()
    dl = (merged["low_me"] - merged["low_bk"]).abs()
    print(f"[sanity] M15 resample vs broker M15: {n} overlapping bars")
    print(f"[sanity] close diff: mean={dc.mean():.4f} max={dc.max():.4f} | "
          f"high max={dh.max():.4f} low max={dl.max():.4f} | "
          f"bars within 0.05: {100.0*(dc<=0.05).mean():.1f}%")


class HTFEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.strategy = FusionStrategy(cfg)
        self._last_h4_idx = None
        self.last_bias = "NEUTRAL"
        self.trades = []

    @staticmethod
    def _atr_as_of(eval_epoch, m15_close_epoch, atr):
        idx = np.searchsorted(m15_close_epoch, eval_epoch, side="right") - 1
        if idx < 0:
            return None, -1
        a = atr[idx]
        return (None if np.isnan(a) else float(a)), idx

    def run(self, m15, h4, start_epoch, end_epoch):
        o = m15["open"].to_numpy(float); h = m15["high"].to_numpy(float)
        l = m15["low"].to_numpy(float); c = m15["close"].to_numpy(float)
        ep = m15["epoch"].to_numpy(); sp = m15["spread"].to_numpy(float)
        tser = m15["time"].to_numpy()
        atr = simple_atr_series(m15, 14)
        m15_close_epoch = ep + 15 * 60
        ukhour = m15["ukhour"].to_numpy()
        N = len(m15)

        h4_close_epoch = h4["epoch"].to_numpy() + 4 * 3600
        h4_rec = h4[["epoch", "open", "high", "low", "close"]]

        pos = None          # open position dict or None
        pending = None

        for j in range(N):
            # (a) fill pending at this bar's open
            if pending is not None and pos is None:
                entry = round(o[j], 2)
                a = pending["atr"]
                if pending["dir"] == "BUY":
                    sl = round(entry - SL_MULT * a, 2); tp = round(entry + TP_MULT * a, 2)
                else:
                    sl = round(entry + SL_MULT * a, 2); tp = round(entry - TP_MULT * a, 2)
                pos = {"dir": pending["dir"], "entry": entry, "sl": sl, "tp": tp,
                       "lots": pending["lots"], "atr": a, "entry_epoch": int(ep[j]),
                       "spread_pts": sp[j], "session": pending["session"]}
            pending = None

            # (b) manage open position on this bar (SL before TP)
            if pos is not None:
                exit_px = None; reason = None
                if pos["dir"] == "BUY":
                    if l[j] <= pos["sl"]:
                        exit_px = o[j] if o[j] <= pos["sl"] else pos["sl"]; reason = "SL"
                    elif h[j] >= pos["tp"]:
                        exit_px = o[j] if o[j] >= pos["tp"] else pos["tp"]; reason = "TP"
                else:
                    if h[j] >= pos["sl"]:
                        exit_px = o[j] if o[j] >= pos["sl"] else pos["sl"]; reason = "SL"
                    elif l[j] <= pos["tp"]:
                        exit_px = o[j] if o[j] <= pos["tp"] else pos["tp"]; reason = "TP"
                if exit_px is not None:
                    self._close(pos, exit_px, reason, int(ep[j]))
                    pos = None

            # (c) signal eval at close of bar j -> entry at j+1 (only when flat)
            if pos is None and pending is None and j + 1 < N:
                eval_epoch = int(ep[j + 1])
                if not (start_epoch <= ep[j] <= end_epoch):
                    continue
                if not (SESSION_START_UK <= ukhour[j + 1] < SESSION_END_UK):
                    continue
                a, aidx = self._atr_as_of(eval_epoch, m15_close_epoch, atr)
                if a is None or j < W:
                    continue
                # H4 structure bias, recomputed on a new closed H4 bar
                h4_idx = np.searchsorted(h4_close_epoch, eval_epoch, side="right") - 1
                if h4_idx >= 50 and h4_idx != self._last_h4_idx:
                    self._last_h4_idx = h4_idx
                    lo = max(0, h4_idx - 250 + 1)
                    recs = h4_rec.iloc[lo:h4_idx + 1].to_dict("records")
                    try:
                        info = self.strategy.compute_structure_bias_from_rates(recs)
                        self.last_bias = info.get("bias", "NEUTRAL") if isinstance(info, dict) else "NEUTRAL"
                    except Exception as e:
                        print("bias err:", e); self.last_bias = "NEUTRAL"
                if self.last_bias not in ("BULL", "BEAR"):
                    continue
                s = j - W + 1
                win = pd.DataFrame({"time": tser[s:j + 1], "open": o[s:j + 1],
                                    "high": h[s:j + 1], "low": l[s:j + 1], "close": c[s:j + 1]})
                signal = self.strategy.analyze(win, bias=self.last_bias)
                if not signal:
                    continue
                # strict direction match
                want = "BULL" if signal["type"] == "BUY" else "BEAR"
                if self.last_bias != want:
                    continue
                lots = position_size(signal, a, self.cfg)
                pending = {"dir": signal["type"], "atr": a, "lots": lots,
                           "session": self._session(ukhour[j + 1])}

        # close any dangling position at last bar
        if pos is not None:
            self._close(pos, c[N - 1], "EOD", int(ep[N - 1]))

    @staticmethod
    def _session(ukhour):
        if 7 <= ukhour < 12:
            return "london"
        if 12 <= ukhour < 18:
            return "new_york"
        return "other"

    def _close(self, pos, exit_price, reason, exit_epoch):
        d = pos["dir"]
        move = (exit_price - pos["entry"]) if d == "BUY" else (pos["entry"] - exit_price)
        gross = move * CONTRACT_SIZE * pos["lots"] / GBPUSD
        commission = COMMISSION * pos["lots"]
        spread_cost = pos["spread_pts"] * POINT * CONTRACT_SIZE * pos["lots"] / GBPUSD
        costs = commission + spread_cost
        self.trades.append({
            "entry_time": datetime.utcfromtimestamp(pos["entry_epoch"] - OFFSET_H * 3600),
            "exit_time": datetime.utcfromtimestamp(exit_epoch - OFFSET_H * 3600),
            "direction": d, "entry_price": pos["entry"], "exit_price": round(exit_price, 2),
            "lots": pos["lots"], "atr": round(pos["atr"], 4),
            "stop": pos["sl"], "target": pos["tp"], "exit_reason": reason,
            "gross_pnl": round(gross, 2), "costs": round(costs, 2),
            "net_pnl": round(gross - costs, 2), "session": pos["session"],
        })


def summarize(trades, label):
    print(f"\n================ {label} ================")
    if not trades:
        print("No trades."); return
    df = pd.DataFrame(trades)
    print(f"Trades: {len(df)}")
    for which, key in (("GROSS", "gross_pnl"), ("NET", "net_pnl")):
        pnl = df[key]; wins = pnl[pnl > 0]; losses = pnl[pnl < 0]
        gw = wins.sum(); gl = -losses.sum()
        pf = gw / gl if gl > 0 else float("inf")
        eq = pnl.cumsum(); dd = (eq - eq.cummax()).min()
        print(f"  --- {which} ---")
        print(f"    win rate     : {100.0*len(wins)/len(df):.1f}%  ({len(wins)}W/{len(losses)}L)")
        print(f"    avg win/loss : {wins.mean() if len(wins) else 0:.2f} / {losses.mean() if len(losses) else 0:.2f}")
        print(f"    profit factor: {pf:.3f}")
        print(f"    expectancy/tr: {pnl.mean():.2f}")
        print(f"    max drawdown : {dd:.2f}")
        print(f"    net P&L      : {pnl.sum():.2f}")
    print("  --- by exit_reason (net) ---")
    print(df.groupby("exit_reason")["net_pnl"].agg(["count", "sum", "mean"]).to_string())
    print("  --- by month (net) ---")
    bym = df.assign(month=df["entry_time"].dt.to_period("M")).groupby("month")["net_pnl"].agg(["count", "sum"])
    print(bym.to_string())


def main():
    with open(os.path.join(ROOT, "config.json")) as f:
        cfg = json.load(f)

    print("Loading + resampling M1 ...", flush=True)
    m1 = load_csv(M1_CSV)
    m1 = m1[m1["time"] >= pd.Timestamp("2020-06-01")].set_index("time")
    m15 = resample(m1, "15min")
    h4 = resample(m1, "4h")
    print(f"M15 bars: {len(m15)} | H4 bars: {len(h4)}")
    sanity_check_m15(m15)

    # precompute UK hour (DST aware): server(+2) -> Europe/London
    idx = pd.DatetimeIndex(m15["time"]).tz_localize(timezone(timedelta(hours=OFFSET_H)))
    m15 = m15.assign(ukhour=idx.tz_convert("Europe/London").hour)

    windows = [
        ("IN-SAMPLE 2021-01-01..2023-12-31", "2021-01-01", "2023-12-31", "results_HTF_IS"),
        ("OUT-OF-SAMPLE 2024-01-01..2026-06-05", "2024-01-01", "2026-06-05", "results_HTF_OOS"),
    ]
    for label, s, e, outdir in windows:
        start_epoch = int(datetime.fromisoformat(s).timestamp()) + OFFSET_H * 3600
        end_epoch = int((datetime.fromisoformat(e) + timedelta(days=1)).timestamp()) + OFFSET_H * 3600
        buf = start_epoch - 45 * 86400
        m15w = m15[(m15["epoch"] >= buf) & (m15["epoch"] <= end_epoch + 86400)].reset_index(drop=True)
        h4w = h4[(h4["epoch"] >= buf - 30 * 86400) & (h4["epoch"] <= end_epoch + 86400)].reset_index(drop=True)
        print(f"\nRunning {label} | M15={len(m15w)} H4={len(h4w)}", flush=True)
        eng = HTFEngine(cfg)
        eng.run(m15w, h4w, start_epoch, end_epoch)
        os.makedirs(os.path.join(ROOT, outdir), exist_ok=True)
        pd.DataFrame(eng.trades).to_csv(os.path.join(ROOT, outdir, "trades.csv"), index=False)
        summarize(eng.trades, label)


if __name__ == "__main__":
    main()
