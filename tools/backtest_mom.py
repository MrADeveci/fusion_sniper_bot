"""
Momentum breakout backtester (LAB, offline, no trades) -- REALISM REBUILD.

The previous benchmark validated the strategy under conditions the live bot does not
run. This engine models what actually happens:

  1. DST-CORRECT SERVER CLOCK (tools/btclock.py). The server keeps 17:00 New York at
     00:00 server, so it is UTC+2 in winter and UTC+3 while New York is on DST. The old
     engine assumed a fixed UTC+2, which shifted the session by an hour for ~8 months a
     year (it was really trading 06:00-17:00 UK, not 07:00-18:00). Window boundaries are
     timezone-explicit (Europe/London -> UTC epoch), not naive .timestamp() calls.

  2. SWAP. Charged per server-midnight held, triple on the broker's triple-swap day.
     Values read from mt5.symbol_info("XAUUSD") on ICMarketsSC-MT5-4 (see constants).

  3. SPREAD IN THE FILL + SLIPPAGE. CSV prices are BID. Longs fill/manage on the bid,
     shorts trigger and fill on the ASK (= bid + spread), so a SELL stop is hit when the
     ASK reaches it -- not the bid. Slippage is applied adverse to every market entry and
     every stop-out fill. Stop-outs that GAP still fill at the bar open beyond the stop.

  4. THE LIVE GATE SET, each individually switchable, mirroring main_bot.run()'s
     engine-agnostic loop: daily loss limit, daily profit target, extreme-ATR skip, trade
     cooldown, news blackouts. Gates block NEW ENTRIES only; management continues.

  5. SIGNIFICANCE. Bootstrap the OOS trade P&Ls (10k resamples) for P(net <= 0), plus a
     permutation baseline that keeps the exit machinery and randomises entry timing
     within the session.

PARAMETERS ARE FROZEN AT THE LIVE CONFIG (config.json STRATEGY.momentum) and are NOT
tuned to these results. --legacy reproduces the OLD engine (fixed UTC+2, no swap, no
slippage, no gates) so the cost of realism is attributable.

Run:
  python tools/backtest_mom.py                 # full realism, live gates, live params
  python tools/backtest_mom.py --legacy        # the old (unrealistic) benchmark
  python tools/backtest_mom.py --report        # everything + significance -> bt_realism.txt
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

ROOT = r"C:\fusion_sniper_bot"
sys.path.insert(0, ROOT)
from tools.backtest import load_csv, simple_atr_series, M1_CSV          # noqa: E402
from tools.backtest_htf import resample, sanity_check_m15               # noqa: E402
from tools.btclock import server_clock, uk_day_bounds_to_utc_epoch, utc_epoch_to_dt  # noqa: E402
from tools.news_calendar import build_blackouts, CALENDAR_NOTE          # noqa: E402
from modules.momentum_strategy import MomentumBreakoutStrategy          # noqa: E402

# ---------------------------------------------------------------------------
# Broker contract facts. Read from mt5.symbol_info("XAUUSD") on ICMarketsSC-MT5-4
# on 2026-06-05 (quote timestamp 1780703819), cached in data/symbol_info.json.
# ---------------------------------------------------------------------------
CONTRACT = 100.0          # trade_contract_size: 1.0 lot = 100 oz
POINT = 0.01              # point
SWAP_MODE = 1             # swap_mode = 1 => SYMBOL_SWAP_MODE_POINTS (swap quoted in POINTS)
SWAP_LONG_PTS = -54.015   # swap_long   (points/lot/night) -- longs PAY
SWAP_SHORT_PTS = 46.175   # swap_short  (points/lot/night) -- shorts are CREDITED
SWAP_3DAY_DOW = 3         # swap_rollover3days = 3 = ENUM_DAY_OF_WEEK WEDNESDAY
_PY_WED = 2               # ...which is weekday()==2 in Python

# points -> USD per lot per night: pts * point * contract_size
SWAP_LONG_USD = SWAP_LONG_PTS * POINT * CONTRACT      # -54.015 USD / lot / night
SWAP_SHORT_USD = SWAP_SHORT_PTS * POINT * CONTRACT    # +46.175 USD / lot / night

COMMISSION = 5.52         # GBP per lot, round turn (as in the validated engine)
GBPUSD = 1.34             # config STRATEGY.momentum.gbpusd

LEGACY_OFFSET_H = 2       # the old engine's fixed (wrong) server offset


# ---------------------------------------------------------------------------
# Cost + gate configuration
# ---------------------------------------------------------------------------
class Costs:
    def __init__(self, swap=True, slip_mult=1.5, ask_triggered_shorts=True,
                 spread_in_fill=True):
        self.swap = swap
        self.slip_mult = slip_mult          # slippage = slip_mult x spread, adverse
        self.ask_triggered_shorts = ask_triggered_shorts
        # True  = spread is IN the fill (BUY enters at ask, SELL exits at ask) -- realistic
        # False = fills are raw bid and the round-trip spread is charged as a cost line
        #         (what the old engine did; kept so --legacy reproduces it exactly)
        self.spread_in_fill = spread_in_fill

    @staticmethod
    def legacy():
        return Costs(swap=False, slip_mult=0.0, ask_triggered_shorts=False,
                     spread_in_fill=False)


class Gates:
    """The live gate set (main_bot.run()). Each switchable; None/0 = off."""

    def __init__(self, daily_loss=None, daily_profit=None, atr_max=None,
                 cooldown_s=0, blackouts=None):
        self.daily_loss = daily_loss        # RISK.max_daily_loss (GBP, positive number)
        self.daily_profit = daily_profit    # TRADING.daily_profit_target (GBP)
        self.atr_max = atr_max              # volatility_detection.atr_max_for_trading
        self.cooldown_s = cooldown_s        # volatility_detection.normal_cooldown_seconds
        self.blackouts = blackouts          # sorted np array of (start,end) UTC epochs

    @staticmethod
    def off():
        return Gates()

    @staticmethod
    def live(cfg, blackouts):
        risk = cfg.get("RISK", {})
        trading = cfg.get("TRADING", {})
        vol = trading.get("volatility_detection", {})
        return Gates(
            daily_loss=float(risk.get("max_daily_loss", 0)) or None,
            daily_profit=float(trading.get("daily_profit_target", 0)) or None,
            atr_max=(float(vol.get("atr_max_for_trading"))
                     if vol.get("skip_trading_when_atr_extreme") else None),
            cooldown_s=int(vol.get("normal_cooldown_seconds", 0)),
            blackouts=blackouts,
        )


def _in_blackout(blackouts, ep) -> bool:
    if blackouts is None or len(blackouts) == 0:
        return False
    i = np.searchsorted(blackouts[:, 0], ep, side="right") - 1
    return bool(i >= 0 and ep <= blackouts[i, 1])


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class MomBacktester:
    def __init__(self, strat: MomentumBreakoutStrategy, costs: Costs = None,
                 gates: Gates = None, legacy=False):
        self.strat = strat
        self.costs = costs or Costs()
        self.gates = gates or Gates.off()
        self.legacy = legacy
        self.trades = []
        self._daily_real = {}                 # UTC date -> realised net GBP
        self.blocked = {"daily_loss": 0, "daily_profit": 0, "atr": 0,
                        "news": 0, "cooldown": 0}
        self._last_entry_ep = None

    # -- swap ---------------------------------------------------------------
    def _swap_gbp(self, direction, lots, entry_srv_ep, exit_srv_ep):
        """Charge one rollover per SERVER midnight crossed; triple on the broker's
        triple-swap day. No rollover is charged on Sat/Sun -- weekend financing is what
        the triple day exists for."""
        if not self.costs.swap:
            return 0.0, 0, 0
        d0, d1 = entry_srv_ep // 86400, exit_srv_ep // 86400
        n1 = n3 = 0
        for d in range(int(d0) + 1, int(d1) + 1):
            wd = datetime.utcfromtimestamp(d * 86400).weekday()   # server-date weekday
            if wd >= 5:                                           # Sat/Sun: no rollover
                continue
            if wd == _PY_WED:
                n3 += 1
            else:
                n1 += 1
        per_lot = SWAP_LONG_USD if direction == "BUY" else SWAP_SHORT_USD
        usd = per_lot * lots * (n1 + 3 * n3)
        return usd / GBPUSD, n1, n3

    # -- entry gates --------------------------------------------------------
    def _gate_blocked(self, ep_utc, atr_val):
        g = self.gates
        day = datetime.fromtimestamp(int(ep_utc), tz=timezone.utc).strftime("%Y-%m-%d")
        real = self._daily_real.get(day, 0.0)
        if g.daily_loss is not None and real <= -abs(g.daily_loss):
            self.blocked["daily_loss"] += 1
            return True
        if g.daily_profit is not None and real >= g.daily_profit:
            self.blocked["daily_profit"] += 1
            return True
        if g.atr_max is not None and atr_val > g.atr_max:
            self.blocked["atr"] += 1
            return True
        if _in_blackout(g.blackouts, ep_utc):
            self.blocked["news"] += 1
            return True
        if (g.cooldown_s and self._last_entry_ep is not None
                and ep_utc - self._last_entry_ep < g.cooldown_s):
            self.blocked["cooldown"] += 1
            return True
        return False

    # -- main loop ----------------------------------------------------------
    def run(self, win, prior_high, prior_low, h4_ce, h4_trend, h4_min,
            start_ep, end_ep, random_mask=None, entry_mode="breakout"):
        """entry_mode:
             breakout         -- the Step 1 strategy: M15 close breaks the prior-N extreme
             always           -- (a) enter EVERY in-session bar while flat, in the H4 direction
             first_of_session -- (b) as (a) but at most ONE entry per session day
             long_only        -- (c) as (a) but longs only
           The dumb modes exist to test the H4-trend + trailing-exit WRAPPER on its own.
        """
        s = self.strat
        entry_days = set()
        o, h, l, c = win["o"], win["h"], win["l"], win["c"]
        ep, ep_srv, sp, atr, uk = win["ep"], win["ep_srv"], win["sp"], win["atr"], win["uk"]
        slipm = self.costs.slip_mult
        asks = self.costs.ask_triggered_shorts
        N = len(o)
        pos = None
        pending = None

        for j in range(N):
            spread = float(sp[j]) * POINT          # bar spread in price units
            slip = slipm * spread                  # adverse slippage per fill

            # (a) fill pending at this bar's open
            if pending is not None and pos is None:
                d = pending["dir"]
                buy_spread = spread if self.costs.spread_in_fill else 0.0
                if d == "BUY":
                    entry = round(float(o[j]) + buy_spread + slip, s.digits)  # pay the ask
                else:
                    entry = round(float(o[j]) - slip, s.digits)               # sell the bid
                a = pending["atr"]
                sl = s.initial_stop(entry, a, d)
                # run extreme is tracked in the basis the stop lives in:
                #   BUY  stop is a BID level  -> track bid highs
                #   SELL stop is an ASK level -> track ask lows (bid + spread)
                run0 = float(h[j]) if d == "BUY" else float(l[j]) + (spread if asks else 0.0)
                pos = {"dir": d, "entry": entry, "sl": sl, "orig_sl": sl, "atr": a,
                       "lots": pending["lots"], "entry_ep": int(ep[j]), "sp": float(sp[j]),
                       "entry_srv": int(ep_srv[j]), "session": pending["session"], "run": run0}
                self._last_entry_ep = int(ep[j])
            pending = None

            # (b) manage the open position
            if pos is not None:
                d = pos["dir"]
                exit_px = None
                cause = None
                if d == "BUY":
                    if l[j] <= pos["sl"]:                       # bid touched the stop
                        raw = o[j] if o[j] <= pos["sl"] else pos["sl"]   # gap -> fill at open
                        exit_px = raw - slip
                        cause = "Trail" if pos["sl"] > pos["orig_sl"] + 1e-9 else "SL"
                else:
                    hi_ask = float(h[j]) + (spread if asks else 0.0)
                    op_ask = float(o[j]) + (spread if asks else 0.0)
                    if hi_ask >= pos["sl"]:                     # ASK touched the stop
                        raw = op_ask if op_ask >= pos["sl"] else pos["sl"]
                        exit_px = raw + slip
                        cause = "Trail" if pos["sl"] < pos["orig_sl"] - 1e-9 else "SL"
                if exit_px is not None:
                    self._close(pos, exit_px, cause, int(ep[j]), int(ep_srv[j]))
                    pos = None
                else:
                    if d == "BUY":
                        pos["run"] = max(pos["run"], float(h[j]))
                    else:
                        pos["run"] = min(pos["run"], float(l[j]) + (spread if asks else 0.0))
                    pos["sl"] = s.update_trailing_stop(d, pos["entry"], pos["sl"],
                                                       pos["run"], pos["atr"])

            # (c) signal at close of j -> entry at j+1, only when flat
            if pos is None and pending is None and j + 1 < N:
                if not (start_ep <= ep[j] <= end_ep):
                    continue
                if not s.in_session(uk[j + 1]):
                    continue
                a = atr[j]
                if a != a or j < 45:
                    continue
                hi = np.searchsorted(h4_ce, int(ep_srv[j + 1]), side="right") - 1
                if hi < h4_min:
                    continue
                trend = int(h4_trend[hi])
                if random_mask is not None:
                    # permutation baseline: same session, same exits, RANDOM entry timing
                    if not random_mask[j] or trend == 0:
                        continue
                    direction = "BUY" if trend > 0 else "SELL"
                elif entry_mode == "breakout":
                    ph, pl = prior_high[j], prior_low[j]
                    if ph != ph or pl != pl:
                        continue
                    direction = s.decide_entry(trend, float(c[j]), float(ph), float(pl))
                    if direction is None:
                        continue
                else:
                    # deliberately dumb entries: direction is the H4 trend, nothing else
                    if trend == 0:
                        continue
                    if entry_mode == "long_only" and trend < 0:
                        continue
                    if entry_mode == "first_of_session":
                        day = datetime.fromtimestamp(
                            int(ep[j + 1]), tz=timezone.utc).strftime("%Y-%m-%d")
                        if day in entry_days:
                            continue
                    direction = "BUY" if trend > 0 else "SELL"
                if self._gate_blocked(int(ep[j + 1]), float(a)):
                    continue
                lots = s.lots_for_risk(self.strat.risk_flat_gbp, a, contract_size=CONTRACT)
                pending = {"dir": direction, "atr": a, "lots": lots,
                           "session": _session(uk[j + 1])}
                if entry_mode == "first_of_session":
                    entry_days.add(datetime.fromtimestamp(
                        int(ep[j + 1]), tz=timezone.utc).strftime("%Y-%m-%d"))

        if pos is not None:
            self._close(pos, float(c[N - 1]), "EOD", int(ep[N - 1]), int(ep_srv[N - 1]))

    def _close(self, pos, exit_price, cause, exit_ep, exit_srv):
        d = pos["dir"]
        # realism: spread is already inside the fills (BUY entered at ask, SELL exits at ask)
        # legacy : fills are raw bid, so charge the round-trip spread as a cost line
        move = (exit_price - pos["entry"]) if d == "BUY" else (pos["entry"] - exit_price)
        gross = move * CONTRACT * pos["lots"] / GBPUSD
        commission = COMMISSION * pos["lots"]
        if not self.costs.spread_in_fill:
            commission += pos["sp"] * POINT * CONTRACT * pos["lots"] / GBPUSD
        swap, n1, n3 = self._swap_gbp(d, pos["lots"], pos["entry_srv"], exit_srv)
        net = gross - commission + swap
        self.trades.append({
            "entry_time": utc_epoch_to_dt(pos["entry_ep"]).replace(tzinfo=None),
            "exit_time": utc_epoch_to_dt(exit_ep).replace(tzinfo=None),
            "direction": d, "entry_price": pos["entry"], "exit_price": round(exit_price, 2),
            "lots": pos["lots"], "atr": round(pos["atr"], 4),
            "stop": pos["orig_sl"], "exit_reason": cause,
            "gross_pnl": round(gross, 2), "commission": round(commission, 2),
            "swap": round(swap, 2), "nights": n1 + n3, "triple_nights": n3,
            "net_pnl": round(net, 2), "session": pos["session"],
        })
        day = utc_epoch_to_dt(exit_ep).strftime("%Y-%m-%d")
        self._daily_real[day] = self._daily_real.get(day, 0.0) + self.trades[-1]["net_pnl"]


def _session(ukhour):
    if 7 <= ukhour < 12:
        return "london"
    if 12 <= ukhour < 18:
        return "new_york"
    return "other"


# ---------------------------------------------------------------------------
# Metrics + significance
# ---------------------------------------------------------------------------
def metrics(trades):
    if not trades:
        return dict(trades=0, win=0.0, net=0.0, pf=0.0, exp=0.0, dd=0.0,
                    gross=0.0, comm=0.0, swap=0.0, overnight=0)
    df = pd.DataFrame(trades)
    pnl = df["net_pnl"]
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    gl = -losses.sum()
    eq = pnl.cumsum()
    return dict(
        trades=len(df),
        win=100.0 * len(wins) / len(df),
        net=float(pnl.sum()),
        pf=float(wins.sum() / gl) if gl > 0 else float("inf"),
        exp=float(pnl.mean()),
        dd=float((eq - eq.cummax()).min()),
        gross=float(df["gross_pnl"].sum()),
        comm=float(df["commission"].sum()),
        swap=float(df["swap"].sum()) if "swap" in df else 0.0,
        overnight=int((df["nights"] > 0).sum()) if "nights" in df else 0,
    )


def bootstrap_p(pnls, n=10000, seed=7):
    """P(net P&L <= 0) under resampling the trade P&Ls with replacement."""
    if len(pnls) == 0:
        return 1.0, (0.0, 0.0)
    rng = np.random.default_rng(seed)
    arr = np.asarray(pnls, dtype=float)
    sums = arr[rng.integers(0, len(arr), size=(n, len(arr)))].sum(axis=1)
    p = float((sums <= 0).mean())
    return p, (float(np.percentile(sums, 2.5)), float(np.percentile(sums, 97.5)))


def _fmt(m):
    pf = "inf" if m["pf"] == float("inf") else f"{m['pf']:.3f}"
    return (f"{m['trades']:>7}{m['win']:>7.1f}{m['net']:>10.0f}{pf:>8}"
            f"{m['exp']:>8.2f}{m['dd']:>10.0f}")


HDR = f"{'':<26}{'trades':>7}{'win%':>7}{'net':>10}{'PF':>8}{'exp':>8}{'maxDD':>10}"


# ---------------------------------------------------------------------------
# Data prep
# ---------------------------------------------------------------------------
WINDOWS = [("IN-SAMPLE  2021-01-01..2023-12-31", "2021-01-01", "2023-12-31", "IS"),
           ("OUT-SAMPLE 2024-01-01..2026-06-05", "2024-01-01", "2026-06-05", "OOS")]


def prepare(strat, legacy=False):
    print("Loading + resampling M1 ...", flush=True)
    m1 = load_csv(M1_CSV)
    m1 = m1[m1["time"] >= pd.Timestamp("2020-06-01")].set_index("time")
    m15 = resample(m1, "15min")
    h4 = resample(m1, "4h")
    sanity_check_m15(m15)

    # DST-correct clock. m15["epoch"] from the loader is the SERVER epoch.
    utc_ep, uk_hour, offs = server_clock(m15["time"])
    m15 = m15.assign(utc_epoch=utc_ep, ukhour=uk_hour)
    uniq, cnt = np.unique(offs, return_counts=True)
    print(f"[clock] server offset distribution: "
          + ", ".join(f"UTC+{u}: {c} bars ({100.0*c/len(offs):.1f}%)" for u, c in zip(uniq, cnt)))

    if legacy:
        idx = pd.DatetimeIndex(m15["time"]).tz_localize(
            timezone(pd.Timedelta(hours=LEGACY_OFFSET_H).to_pytimedelta()))
        m15 = m15.assign(ukhour=idx.tz_convert("Europe/London").hour,
                         utc_epoch=m15["epoch"] - LEGACY_OFFSET_H * 3600)

    h4_ema = strat.ema_series(h4["close"]).to_numpy(float)
    h4_close = h4["close"].to_numpy(float)
    h4_trend = np.where(h4_close > h4_ema, 1, np.where(h4_close < h4_ema, -1, 0))
    h4_ce = h4["epoch"].to_numpy() + 4 * 3600          # server epoch of H4 close

    prepared = []
    for label, s, e, tag in WINDOWS:
        if legacy:
            start_ep = int(datetime.fromisoformat(s).timestamp())
            end_ep = int(datetime.fromisoformat(e).timestamp()) + 86400
        else:
            start_ep = uk_day_bounds_to_utc_epoch(s)
            end_ep = uk_day_bounds_to_utc_epoch(e, end=True)
        buf = start_ep - 45 * 86400
        w = m15[(m15["utc_epoch"] >= buf) & (m15["utc_epoch"] <= end_ep + 86400)].reset_index(drop=True)
        win = dict(o=w["open"].to_numpy(float), h=w["high"].to_numpy(float),
                   l=w["low"].to_numpy(float), c=w["close"].to_numpy(float),
                   ep=w["utc_epoch"].to_numpy(), ep_srv=w["epoch"].to_numpy(),
                   sp=w["spread"].to_numpy(float), atr=simple_atr_series(w, 14),
                   uk=w["ukhour"].to_numpy())
        ph = w["high"].rolling(strat.lookback).max().shift(1).to_numpy(float)
        pl = w["low"].rolling(strat.lookback).min().shift(1).to_numpy(float)
        prepared.append(dict(label=label, tag=tag, win=win, ph=ph, pl=pl,
                             start=start_ep, end=end_ep))
    return prepared, h4_ce, h4_trend


def run_one(strat, p, h4_ce, h4_trend, costs, gates, legacy=False, random_mask=None,
            entry_mode="breakout"):
    bt = MomBacktester(strat, costs, gates, legacy=legacy)
    bt.run(p["win"], p["ph"], p["pl"], h4_ce, h4_trend, strat.h4_ema,
           p["start"], p["end"], random_mask=random_mask, entry_mode=entry_mode)
    return bt


# ---------------------------------------------------------------------------
# Permutation baseline: same session + same exit machinery, RANDOM entry timing
# ---------------------------------------------------------------------------
def permutation_test(strat, p, h4_ce, h4_trend, costs, gates, n_trades, n_perm=200, seed=11):
    win = p["win"]
    eligible = np.zeros(len(win["o"]), dtype=bool)
    uk, atr, ep = win["uk"], win["atr"], win["ep"]
    for j in range(len(eligible) - 1):
        if not (p["start"] <= ep[j] <= p["end"]):
            continue
        if j < 45 or atr[j] != atr[j]:
            continue
        if strat.in_session(uk[j + 1]):
            eligible[j] = True
    n_elig = int(eligible.sum())
    rate = n_trades / max(n_elig, 1)          # match the real entry rate
    rng = np.random.default_rng(seed)
    nets, counts = [], []
    for i in range(n_perm):
        mask = eligible & (rng.random(len(eligible)) < rate)
        bt = run_one(strat, p, h4_ce, h4_trend, costs, gates, random_mask=mask)
        m = metrics(bt.trades)
        nets.append(m["net"])
        counts.append(m["trades"])
        if (i + 1) % 50 == 0:
            print(f"    permutation {i+1}/{n_perm} ...", flush=True)
    return np.array(nets), np.array(counts), n_elig


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def build_strategy(cfg):
    """Parameters FROZEN at the live config. Nothing here is tuned to the results."""
    return MomentumBreakoutStrategy(dict(cfg["STRATEGY"]["momentum"]))


def report(args):
    out = []

    def emit(line=""):
        print(line, flush=True)
        out.append(line)

    with open(os.path.join(ROOT, "config.json")) as f:
        cfg = json.load(f)
    strat = build_strategy(cfg)
    blackouts = build_blackouts()

    emit("=" * 96)
    emit("MOMENTUM BREAKOUT -- REALISM REBUILD")
    emit("=" * 96)
    emit(f"generated              : {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    emit("parameterisation       : FROZEN at live config.json STRATEGY.momentum, NOT re-tuned")
    emit(f"  trail_atr_mult       : {strat.trail_mult}  <- the live bot builds the strategy from")
    emit("                          config.json, so 3.0 is what actually runs. The module")
    emit("                          default was 3.5 and never applied live; corrected to 3.0.")
    emit(f"  sl_atr_mult          : {strat.sl_mult}")
    emit(f"  h4_ema / lookback    : {strat.h4_ema} / {strat.lookback}")
    emit(f"  session (UK)         : {strat.sess_start:02d}:00-{strat.sess_end:02d}:00")
    emit(f"  sizing               : {strat.sizing_mode}, risk GBP {strat.risk_flat_gbp}/trade")
    emit("")
    emit("broker facts (mt5.symbol_info XAUUSD, ICMarketsSC-MT5-4, read 2026-06-05):")
    emit(f"  swap_mode            : {SWAP_MODE} (POINTS)")
    emit(f"  swap_long            : {SWAP_LONG_PTS} pts = {SWAP_LONG_USD:+.3f} USD/lot/night")
    emit(f"  swap_short           : {SWAP_SHORT_PTS} pts = {SWAP_SHORT_USD:+.3f} USD/lot/night")
    emit(f"  swap_rollover3days   : {SWAP_3DAY_DOW} (WEDNESDAY) -> 3x on the Tue->Wed rollover")
    emit(f"  commission           : GBP {COMMISSION}/lot round turn")
    emit("")
    emit("news blackouts:")
    for ln in CALENDAR_NOTE.strip().splitlines():
        emit("  " + ln)
    emit("")

    emit("=" * 96)
    emit("1. WHAT THE OLD BENCHMARK CLAIMED  (fixed UTC+2, no swap, no slippage, no gates)")
    emit("=" * 96)
    prep_l, h4_ce_l, h4_tr_l = prepare(strat, legacy=True)
    emit(HDR)
    legacy_m = {}
    for p in prep_l:
        bt = run_one(strat, p, h4_ce_l, h4_tr_l, Costs.legacy(), Gates.off(), legacy=True)
        m = metrics(bt.trades)
        legacy_m[p["tag"]] = m
        emit(f"{p['label']:<26}" + _fmt(m))
    emit("")

    prep, h4_ce, h4_tr = prepare(strat, legacy=False)
    costs = Costs(swap=True, slip_mult=args.slip)
    gates_live = Gates.live(cfg, blackouts)

    emit("=" * 96)
    emit("2. THE SAME STRATEGY UNDER REALISTIC CONDITIONS")
    emit("=" * 96)
    emit(f"   slippage = {args.slip}x spread on every entry and every stop-out fill")
    emit("")
    emit(HDR)
    real_bt, gated_bt = {}, {}
    for p in prep:
        bt = run_one(strat, p, h4_ce, h4_tr, costs, Gates.off())
        real_bt[p["tag"]] = bt
        emit(f"{'  DST clock + swap + slip':<26}" + _fmt(metrics(bt.trades)) + "   " + p["tag"])
    for p in prep:
        bt = run_one(strat, p, h4_ce, h4_tr, costs, gates_live)
        gated_bt[p["tag"]] = bt
        emit(f"{'  + FULL LIVE GATES':<26}" + _fmt(metrics(bt.trades)) + "   " + p["tag"])
    emit("")

    emit("   attribution (net GBP):")
    emit(f"   {'window':<8}{'old claim':>12}{'realism':>12}{'+gates':>12}{'delta':>12}")
    for tag in ("IS", "OOS"):
        a = legacy_m[tag]["net"]
        b = metrics(real_bt[tag].trades)["net"]
        c = metrics(gated_bt[tag].trades)["net"]
        emit(f"   {tag:<8}{a:>12.0f}{b:>12.0f}{c:>12.0f}{c - a:>12.0f}")
    emit("")

    oos = metrics(real_bt["OOS"].trades)
    df = pd.DataFrame(real_bt["OOS"].trades)
    emit("   OOS overnight / swap detail (realism costs, gates off):")
    emit(f"     trades held overnight : {oos['overnight']} of {oos['trades']} "
         f"({100.0 * oos['overnight'] / max(oos['trades'], 1):.1f}%)")
    emit(f"     total swap charged    : GBP {oos['swap']:.2f}")
    if len(df):
        bl = df[df.direction == "BUY"]
        sl = df[df.direction == "SELL"]
        emit(f"       longs  : GBP {bl['swap'].sum():.2f} over {int(bl['nights'].sum())} nights (cost)")
        emit(f"       shorts : GBP {sl['swap'].sum():.2f} over {int(sl['nights'].sum())} nights (credit)")
        emit(f"     triple-swap nights    : {int(df['triple_nights'].sum())}")
    emit(f"     total commission      : GBP {oos['comm']:.2f}")
    emit("")

    emit("=" * 96)
    emit("3. GATE ABLATION (OOS, realism costs, one gate at a time)")
    emit("=" * 96)
    p_oos = [p for p in prep if p["tag"] == "OOS"][0]
    vol = cfg["TRADING"]["volatility_detection"]
    ablation = [
        ("no gates", Gates.off()),
        ("daily loss only (60)", Gates(daily_loss=float(cfg["RISK"]["max_daily_loss"]))),
        ("daily profit only (20)", Gates(daily_profit=float(cfg["TRADING"]["daily_profit_target"]))),
        ("extreme ATR only (>20)", Gates(atr_max=float(vol["atr_max_for_trading"]))),
        ("cooldown only (60s)", Gates(cooldown_s=int(vol["normal_cooldown_seconds"]))),
        ("news only", Gates(blackouts=blackouts)),
        ("ALL LIVE GATES", gates_live),
    ]
    emit(HDR)
    for name, g in ablation:
        bt = run_one(strat, p_oos, h4_ce, h4_tr, costs, g)
        emit(f"{'  ' + name:<26}" + _fmt(metrics(bt.trades)))
        b = bt.blocked
        if sum(b.values()):
            emit(" " * 26 + "   blocked: " + ", ".join(f"{k}={v}" for k, v in b.items() if v))
    emit("")

    emit("=" * 96)
    emit("4. SLIPPAGE SENSITIVITY (OOS, full live gates)")
    emit("=" * 96)
    emit(HDR)
    for sm in (0.0, 1.0, 1.5, 2.0):
        bt = run_one(strat, p_oos, h4_ce, h4_tr, Costs(swap=True, slip_mult=sm), gates_live)
        tag = "   <- default" if sm == 1.5 else ""
        emit(f"{'  slippage ' + str(sm) + 'x spread':<26}" + _fmt(metrics(bt.trades)) + tag)
    emit("")

    emit("=" * 96)
    emit(f"5. SIGNIFICANCE (full live gates, slippage {args.slip}x)")
    emit("=" * 96)
    for tag in ("IS", "OOS"):
        tr = gated_bt[tag].trades
        m = metrics(tr)
        pnls = [t["net_pnl"] for t in tr]
        p, ci = bootstrap_p(pnls, n=args.boot)
        pf = "inf" if m["pf"] == float("inf") else f"{m['pf']:.3f}"
        emit(f"  {tag}: {m['trades']} trades | net GBP {m['net']:.0f} | PF {pf} | "
             f"expectancy GBP {m['exp']:.2f} | maxDD GBP {m['dd']:.0f}")
        emit(f"      bootstrap ({args.boot:,} resamples): P(net <= 0) = {p:.4f}")
        emit(f"      95% CI on net: [GBP {ci[0]:.0f}, GBP {ci[1]:.0f}]")
    emit("")

    if args.perm > 0:
        emit(f"  permutation baseline (OOS): {args.perm} runs. Entry TIMING randomised within the")
        emit("  session; same H4-trend direction filter, same exits, same gates, same costs.")
        emit("  Asks: does the BREAKOUT trigger beat entering at a random in-session time in")
        emit("  the trend direction?")
        m_oos = metrics(gated_bt["OOS"].trades)
        nets, counts, n_elig = permutation_test(
            strat, p_oos, h4_ce, h4_tr, costs, gates_live,
            n_trades=m_oos["trades"], n_perm=args.perm)
        pct = float((nets >= m_oos["net"]).mean())
        emit(f"      eligible in-session bars : {n_elig:,}")
        emit(f"      random-entry net GBP     : mean {nets.mean():.0f}, sd {nets.std():.0f}, "
             f"median {np.median(nets):.0f}")
        emit(f"      random-entry trades/run  : mean {counts.mean():.0f}")
        emit(f"      random-entry range       : [{nets.min():.0f}, {nets.max():.0f}]")
        emit(f"      ACTUAL strategy net      : {m_oos['net']:.0f} GBP")
        emit(f"      P(random >= actual)      : {pct:.4f}")
        emit("")

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--legacy", action="store_true", help="reproduce the OLD (unrealistic) engine")
    ap.add_argument("--report", action="store_true", help="full matrix + significance -> bt_realism.txt")
    ap.add_argument("--slip", type=float, default=1.5, help="slippage as a multiple of spread")
    ap.add_argument("--boot", type=int, default=10000, help="bootstrap resamples")
    ap.add_argument("--perm", type=int, default=200, help="permutation runs (0 = skip)")
    args = ap.parse_args()

    with open(os.path.join(ROOT, "config.json")) as f:
        cfg = json.load(f)
    strat = build_strategy(cfg)

    if args.legacy:
        prep, h4_ce, h4_tr = prepare(strat, legacy=True)
        print(HDR)
        for p in prep:
            bt = run_one(strat, p, h4_ce, h4_tr, Costs.legacy(), Gates.off(), legacy=True)
            print(f"{p['label']:<26}" + _fmt(metrics(bt.trades)))
        return

    if args.report:
        lines = report(args)
        path = os.path.join(ROOT, "bt_realism.txt")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        print(f"\nwritten -> {path}")
        return

    prep, h4_ce, h4_tr = prepare(strat, legacy=False)
    gates = Gates.live(cfg, build_blackouts())
    print(HDR)
    for p in prep:
        bt = run_one(strat, p, h4_ce, h4_tr, Costs(slip_mult=args.slip), gates)
        print(f"{p['label']:<26}" + _fmt(metrics(bt.trades)))


if __name__ == "__main__":
    main()
