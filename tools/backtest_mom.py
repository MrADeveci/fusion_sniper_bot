"""
Momentum breakout backtester (LAB, offline, no trades). REFACTORED for v5.0: entries,
stops, the trailing exit and risk-based sizing now come from the shared single source of
truth modules/momentum_strategy.MomentumBreakoutStrategy, so this backtester and the live
bot make the same decisions.

Validated "Variant 2" exit: SL 1.5xATR, no fixed TP, ratcheting ATR trailing stop, in the
H4-EMA(50) trend direction, London/NY 07:00-18:00 UK, risk-based sizing, real costs.

Usage:
  python tools/backtest_mom.py --trail 3.0     # parity vs the recorded 3.0 benchmark
  python tools/backtest_mom.py --trail 3.5     # live-default trail
"""
from __future__ import annotations

import os
import sys
import argparse
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

ROOT = r"C:\fusion_sniper_bot"
sys.path.insert(0, ROOT)
from tools.backtest import load_csv, simple_atr_series, M1_CSV          # noqa: E402
from tools.backtest_htf import resample, sanity_check_m15              # noqa: E402
from modules.momentum_strategy import MomentumBreakoutStrategy         # noqa: E402 (single source of truth)

OFFSET_H = 2
GBPUSD = 1.34
COMMISSION = 5.52
RISK_FLAT_GBP = 50.0
CONTRACT = 100.0
POINT = 0.01


def _day_key(epoch):   # broker wall-clock date (offset already embedded in epoch)
    return datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%d")


def _week_key(epoch):  # ISO year-week, Monday start (matches week_start_day=monday)
    iso = datetime.utcfromtimestamp(epoch).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


class MomBacktester:
    def __init__(self, strat: MomentumBreakoutStrategy,
                 daily_profit_cap=None, weekly_profit_cap=None):
        self.strat = strat
        self.trades = []
        # Optional realised-NET PROFIT caps. None => OFF, which makes the entry gate a
        # no-op so the validated (caps-off) behaviour is reproduced byte-for-byte. No
        # daily-loss limit is modelled here, matching the validated engine.
        self.daily_profit_cap = daily_profit_cap
        self.weekly_profit_cap = weekly_profit_cap
        self._daily_real = {}
        self._weekly_real = {}
        self.skipped = 0

    def _entry_capped(self, entry_epoch):
        """True if a profit cap is active for this day/week (pauses NEW entries only)."""
        if self.daily_profit_cap is None and self.weekly_profit_cap is None:
            return False
        if (self.daily_profit_cap is not None
                and self._daily_real.get(_day_key(entry_epoch), 0.0) >= self.daily_profit_cap):
            return True
        if (self.weekly_profit_cap is not None
                and self._weekly_real.get(_week_key(entry_epoch), 0.0) >= self.weekly_profit_cap):
            return True
        return False

    def run(self, win, prior_high, prior_low, h4_ce, h4_trend, h4_min, start_epoch, end_epoch):
        s = self.strat
        o, h, l, c, ep, sp, atr, uk = (win["o"], win["h"], win["l"], win["c"],
                                       win["ep"], win["sp"], win["atr"], win["uk"])
        N = len(o)
        pos = None
        pending = None
        for j in range(N):
            # (a) fill pending at this bar open (module computes stop + lots)
            if pending is not None and pos is None:
                entry = round(float(o[j]), s.digits)
                a = pending["atr"]
                sl = s.initial_stop(entry, a, pending["dir"])
                pos = {"dir": pending["dir"], "entry": entry, "sl": sl, "orig_sl": sl,
                       "atr": a, "lots": pending["lots"], "sp": float(sp[j]),
                       "entry_epoch": int(ep[j]), "session": pending["session"],
                       "run": float(h[j] if pending["dir"] == "BUY" else l[j])}
            pending = None

            # (b) manage open position (shared trailing via module)
            if pos is not None:
                d = pos["dir"]; exit_px = None; cause = None
                if d == "BUY":
                    if l[j] <= pos["sl"]:
                        exit_px = o[j] if o[j] <= pos["sl"] else pos["sl"]
                        cause = "Trail" if pos["sl"] > pos["orig_sl"] + 1e-9 else "SL"
                else:
                    if h[j] >= pos["sl"]:
                        exit_px = o[j] if o[j] >= pos["sl"] else pos["sl"]
                        cause = "Trail" if pos["sl"] < pos["orig_sl"] - 1e-9 else "SL"
                if exit_px is not None:
                    self._close(pos, exit_px, cause, int(ep[j]))
                    pos = None
                else:
                    if d == "BUY":
                        pos["run"] = max(pos["run"], float(h[j]))
                    else:
                        pos["run"] = min(pos["run"], float(l[j]))
                    pos["sl"] = s.update_trailing_stop(d, pos["entry"], pos["sl"], pos["run"], pos["atr"])

            # (c) signal eval at close of bar j -> entry at j+1 (only when flat)
            if pos is None and pending is None and j + 1 < N:
                if not (start_epoch <= ep[j] <= end_epoch):
                    continue
                if not s.in_session(uk[j + 1]):
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
                direction = s.decide_entry(int(h4_trend[hi]), float(c[j]), float(ph), float(pl))
                if direction is None:
                    continue
                if self._entry_capped(int(ep[j + 1])):     # profit-cap gate (no-op when caps off)
                    self.skipped += 1
                    continue
                lots = s.lots_for_risk(RISK_FLAT_GBP, a, contract_size=CONTRACT)
                pending = {"dir": direction, "atr": a, "lots": lots,
                           "session": _session(uk[j + 1])}

        if pos is not None:
            self._close(pos, c[N - 1], "EOD", int(ep[N - 1]))

    def _close(self, pos, exit_price, cause, exit_epoch):
        d = pos["dir"]
        move = (exit_price - pos["entry"]) if d == "BUY" else (pos["entry"] - exit_price)
        gross = move * CONTRACT * pos["lots"] / GBPUSD
        commission = COMMISSION * pos["lots"]
        spread_cost = pos["sp"] * POINT * CONTRACT * pos["lots"] / GBPUSD
        costs = commission + spread_cost
        self.trades.append({
            "entry_time": datetime.utcfromtimestamp(pos["entry_epoch"] - OFFSET_H * 3600),
            "exit_time": datetime.utcfromtimestamp(exit_epoch - OFFSET_H * 3600),
            "direction": d, "entry_price": pos["entry"], "exit_price": round(exit_price, 2),
            "lots": pos["lots"], "atr": round(pos["atr"], 4),
            "stop": pos["orig_sl"], "exit_reason": cause,
            "gross_pnl": round(gross, 2), "costs": round(costs, 2),
            "net_pnl": round(gross - costs, 2), "session": pos["session"],
        })
        net = self.trades[-1]["net_pnl"]
        self._daily_real[_day_key(exit_epoch)] = self._daily_real.get(_day_key(exit_epoch), 0.0) + net
        self._weekly_real[_week_key(exit_epoch)] = self._weekly_real.get(_week_key(exit_epoch), 0.0) + net


def _session(ukhour):
    if 7 <= ukhour < 12:
        return "london"
    if 12 <= ukhour < 18:
        return "new_york"
    return "other"


def summarize(trades, label):
    print(f"\n================ {label} ================")
    if not trades:
        print("No trades."); return None
    df = pd.DataFrame(trades)
    out = {"trades": len(df)}
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
        if which == "NET":
            out.update(net_pf=pf, net_sum=pnl.sum())
    return out


# 4 PROFIT-CAP structures (loss controls identical = none, matching validated engine)
CAP_CONFIGS = [
    ("A both (D20+W65)", 20.0, 65.0),
    ("B daily only (D20)", 20.0, None),
    ("C weekly only (W65)", None, 65.0),
    ("D none [=validated]", None, None),
]


def cfg_metrics(trades):
    df = pd.DataFrame(trades)
    pnl = df["net_pnl"]
    wins = pnl[pnl > 0]; losses = pnl[pnl < 0]
    gl = -losses.sum()
    pf = (wins.sum() / gl) if gl > 0 else float("inf")
    eq = pnl.cumsum()
    dd = float((eq - eq.cummax()).min())
    df = df.assign(month=pd.to_datetime(df["exit_time"]).dt.to_period("M").astype(str))
    by_m = df.groupby("month")["net_pnl"].sum()
    return dict(trades=len(df), win=100.0 * len(wins) / len(df), net=float(pnl.sum()),
                pf=pf, exp=float(pnl.mean()), dd=dd,
                pm=int((by_m > 0).sum()), lm=int((by_m <= 0).sum()),
                worst=(by_m.idxmin(), float(by_m.min())), best=(by_m.idxmax(), float(by_m.max())),
                nmo=len(by_m))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trail", type=float, default=3.5, help="trailing stop xATR (default 3.5 live)")
    ap.add_argument("--caps-compare", action="store_true",
                    help="run the 4 profit-cap structures (A/B/C/D) through this same engine")
    args = ap.parse_args()

    params = dict(h4_ema=50, breakout_lookback=20, atr_period=14, sl_atr_mult=1.5,
                  trail_atr_mult=args.trail, trail_activation_atr=1.0,
                  session_start_uk=7, session_end_uk=18,
                  sizing_mode="flat", risk_flat_gbp=RISK_FLAT_GBP, gbpusd=GBPUSD, price_digits=2)
    strat = MomentumBreakoutStrategy(params)
    print(f"MomentumBreakoutStrategy: trail={strat.trail_mult} sl={strat.sl_mult} "
          f"ema={strat.h4_ema} lookback={strat.lookback}")

    print("Loading + resampling M1 ...", flush=True)
    m1 = load_csv(M1_CSV)
    m1 = m1[m1["time"] >= pd.Timestamp("2020-06-01")].set_index("time")
    m15 = resample(m1, "15min")
    h4 = resample(m1, "4h")
    sanity_check_m15(m15)

    idx = pd.DatetimeIndex(m15["time"]).tz_localize(timezone(timedelta(hours=OFFSET_H)))
    m15 = m15.assign(ukhour=idx.tz_convert("Europe/London").hour)

    # H4 trend via the module's EMA definition (shared)
    h4_ema = strat.ema_series(h4["close"]).to_numpy(float)
    h4_close = h4["close"].to_numpy(float)
    h4_trend = np.where(h4_close > h4_ema, 1, np.where(h4_close < h4_ema, -1, 0))
    h4_ce = h4["epoch"].to_numpy() + 4 * 3600

    WINDOWS = [("IN-SAMPLE 2021-01-01..2023-12-31", "2021-01-01", "2023-12-31", "IS"),
               ("OUT-OF-SAMPLE 2024-01-01..2026-06-05", "2024-01-01", "2026-06-05", "OOS")]

    # prepare each window's arrays once (shared across all cap configs)
    prepared = []
    for label, s, e, tag in WINDOWS:
        start_epoch = int(datetime.fromisoformat(s).timestamp()) + OFFSET_H * 3600
        end_epoch = int((datetime.fromisoformat(e) + timedelta(days=1)).timestamp()) + OFFSET_H * 3600
        buf = start_epoch - 45 * 86400
        w = m15[(m15["epoch"] >= buf) & (m15["epoch"] <= end_epoch + 86400)].reset_index(drop=True)
        win = dict(o=w["open"].to_numpy(float), h=w["high"].to_numpy(float),
                   l=w["low"].to_numpy(float), c=w["close"].to_numpy(float),
                   ep=w["epoch"].to_numpy(), sp=w["spread"].to_numpy(float),
                   atr=simple_atr_series(w, 14), uk=w["ukhour"].to_numpy())
        ph = w["high"].rolling(strat.lookback).max().shift(1).to_numpy(float)
        pl = w["low"].rolling(strat.lookback).min().shift(1).to_numpy(float)
        prepared.append((label, tag, win, ph, pl, start_epoch, end_epoch))

    if not args.caps_compare:
        # DEFAULT validated path — caps OFF, identical to the recorded benchmark
        for label, tag, win, ph, pl, start_epoch, end_epoch in prepared:
            print(f"\nRunning {label} | M15={len(win['o'])}", flush=True)
            bt = MomBacktester(strat)
            bt.run(win, ph, pl, h4_ce, h4_trend, strat.h4_ema, start_epoch, end_epoch)
            outdir = f"results_MOM_v2_{tag}"
            os.makedirs(os.path.join(ROOT, outdir), exist_ok=True)
            pd.DataFrame(bt.trades).to_csv(os.path.join(ROOT, outdir, "trades.csv"), index=False)
            summarize(bt.trades, f"Variant 2 (module, trail {strat.trail_mult}) | {label}")
        return

    # CAPS-COMPARE path — 4 structures through the SAME engine
    res = {}
    for label, tag, win, ph, pl, start_epoch, end_epoch in prepared:
        for clabel, dcap, wcap in CAP_CONFIGS:
            bt = MomBacktester(strat, daily_profit_cap=dcap, weekly_profit_cap=wcap)
            bt.run(win, ph, pl, h4_ce, h4_trend, strat.h4_ema, start_epoch, end_epoch)
            res[(tag, clabel)] = (cfg_metrics(bt.trades), bt.skipped)

    hdr = (f"{'config':<22}{'trades':>7}{'win%':>6}{'net':>9}{'PF':>7}{'exp':>7}"
           f"{'maxDD':>9}{'+mo/-mo':>9}{'skip':>6}")
    for label, tag, *_ in prepared:
        print(f"\n================ {label} (trail {strat.trail_mult}) ================")
        print(hdr)
        for clabel, _, _ in CAP_CONFIGS:
            m, sk = res[(tag, clabel)]
            print(f"{clabel:<22}{m['trades']:>7}{m['win']:>6.1f}{m['net']:>9.0f}{m['pf']:>7.3f}"
                  f"{m['exp']:>7.2f}{m['dd']:>9.0f}{str(m['pm'])+'/'+str(m['lm']):>9}{sk:>6}")
        print("  worst / best month:")
        for clabel, _, _ in CAP_CONFIGS:
            m, _ = res[(tag, clabel)]
            print(f"    {clabel:<22} worst {m['worst'][0]} {m['worst'][1]:>8.0f}   "
                  f"best {m['best'][0]} {m['best'][1]:>8.0f}   ({m['nmo']} mo)")

    # correctness gate: D (none) must reproduce the validated Variant 2
    print("\n================ GATE: config D (none) vs validated Variant 2 ================")
    GATE = {"IS": (956, 1355.46), "OOS": (779, 2548.45)}   # from logs/parity_mom.txt (trail 3.0)
    for tag in ("IS", "OOS"):
        m, _ = res[(tag, "D none [=validated]")]
        gt, gn = GATE[tag]
        ok = (m["trades"] == gt) and (abs(m["net"] - gn) < 1.0)
        print(f"  {tag}: D none -> {m['trades']} trades / £{m['net']:.2f}   "
              f"expected {gt} / £{gn:.2f}   {'PASS' if ok else 'FAIL'}"
              + ("" if strat.trail_mult == 3.0 else "  (gate defined at trail 3.0)"))


if __name__ == "__main__":
    main()
