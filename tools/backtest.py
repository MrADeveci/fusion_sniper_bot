"""
Fusion Sniper Bot - Offline Backtester
======================================
Bar-by-bar backtest that faithfully reproduces how main_bot.py trades, so we can
measure the strategy's real expectancy.

This is OFFLINE analysis over CSV data. It does NOT connect to MT5 and places NO
orders. It REUSES:
  - modules/strategy.py  (FusionStrategy)            -> all signal generation
  - modules/risk_manager.py (calculate_atr_based_stops) -> SL/TP placement

A SINGLE FusionStrategy instance is kept for the whole run so its internal
FVG / structure-bias state persists across bars, exactly like the live bot.

Where the live bot relies on MT5 (datetime.now(), tick prices, position objects)
we mirror the *pure* logic and cite the main_bot.py lines we based it on. The
helper gates below (trading hours, session, close-reason) are re-implementations
of small pure functions in main_bot.py because that class cannot be instantiated
offline (its __init__ connects to MT5).

MODELLED: entry M1 window + M15 bias/ATR series, structure-bias + strict_bias gate,
closed-candle gating, max_positions + risk-free stacking, trade cooldown, trading
hours, scalp/volatility mode + extreme-ATR skip, ALL exits (ATR SL/TP, smart
breakeven, chandelier trailing, scalp quick-profit), and the daily profit/loss +
weekly profit/loss pause (on realised NET P&L).

DELIBERATELY NOT MODELLED (documented so the numbers are not mistaken for a full
simulation):
  - news filter avoidance and swap-avoidance windows
  - the equity/floating-drawdown branch of the daily loss check (loss_limit_by_equity);
    we use realised NET P&L only, since positions close within a bar here
  - broker minimum stop-distance adjustment (open_trade lines 1318-1336; this
    broker reports trade_stops_level 0 for XAUUSD, so the adjustment is a no-op)
  - slippage / requotes / partial fills

KEY ASSUMPTIONS (affect absolute P&L, not the engine logic):
  - GBP P&L = USD price-P&L / gbpusd (constant, default 1.34 ~ Jan 2026)
  - VPS wall clock (used by the live daily reset & session tag) assumed == UTC;
    server/CSV time = UTC + BROKER.broker_timezone_offset (2h)

Run:
  python tools/backtest.py --start 2025-01-01 --end 2026-06-05
  python tools/backtest.py --validate            (Jan 12-22 2026, SMC off)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# Make modules/ importable
ROOT = r"C:\fusion_sniper_bot"
sys.path.insert(0, ROOT)

from modules.strategy import FusionStrategy          # noqa: E402  (reused, not reimplemented)
from modules.risk_manager import RiskManager         # noqa: E402  (reused for calculate_atr_based_stops)

DATA_DIR = os.path.join(ROOT, "data")
M1_CSV = os.path.join(DATA_DIR, "XAUUSD_M1.csv")
M15_CSV = os.path.join(DATA_DIR, "XAUUSD_M15.csv")

CONTRACT_SIZE = 100.0    # XAUUSD: 1.0 lot = 100 oz (confirmed via symbol_info)
POINT = 0.01             # gold point
PIP_SIZE = 0.1           # main_bot.py __init__ lines 194-196 (XAUUSD pip = 0.1)
CLOSE_TOL = PIP_SIZE * 2 # determine_close_reason tolerance, main_bot.py line 1263


# ----------------------------------------------------------------------------
# Position sizing (single swappable function, per spec)
# ----------------------------------------------------------------------------
def position_size(signal, atr, cfg):
    """Baseline: live fixed lot size. Swap this body for risk-based sizing later
    without touching the engine."""
    return float(cfg["TRADING"]["lot_size"])


# ----------------------------------------------------------------------------
# Pure re-implementations of small main_bot.py helpers (cited)
# ----------------------------------------------------------------------------
def within_trading_hours(ts_utc, th):
    """Mirror of main_bot.is_within_trading_hours (lines 590-647) as a pure fn.

    The live code uses datetime.now() (VPS clock ~ UTC). Note monday_open_hour is
    only used for *messages* there, never as an actual gate, so the real open
    window is: Monday 00:00 -> Friday friday_close_hour, Sat & Sun fully closed
    (given saturday_closed & sunday_closed both true)."""
    wd = ts_utc.weekday()
    h = ts_utc.hour
    if wd == 5 and th["saturday_closed"]:
        return False
    if wd == 6 and th["sunday_closed"]:
        return False
    if wd == 4 and h >= th["friday_close_hour"] and (th["saturday_closed"] or th["sunday_closed"]):
        return False
    return True


def current_session(ts_utc):
    """Mirror of main_bot.get_current_session (lines 1245-1258). Uses UTC hour."""
    h = ts_utc.hour
    if 0 <= h < 9:
        return "asia"
    elif 8 <= h < 16:
        return "london"
    elif 13 <= h < 21:
        return "new_york"
    return "unknown"


def determine_close_reason(exit_price, sl_price, tp_price, direction):
    """Exact mirror of main_bot.determine_close_reason (lines 1260-1280).

    NB: in the live bot the sl/tp passed here are the ORIGINAL values stored at
    entry (tracked_positions is not updated on SL modify), which is why a trailed
    SL exit lands between original sl and tp and is labelled 'Trailing Stop'."""
    if sl_price > 0 and abs(exit_price - sl_price) < CLOSE_TOL:
        return "Stop Loss hit"
    if tp_price > 0 and abs(exit_price - tp_price) < CLOSE_TOL:
        return "Take Profit hit"
    if direction == "BUY":
        if sl_price > 0 and exit_price > sl_price and exit_price < tp_price:
            return "Trailing Stop"
    else:
        if sl_price > 0 and exit_price < sl_price and exit_price > tp_price:
            return "Trailing Stop"
    return "Manual close"


# ----------------------------------------------------------------------------
# Data loading
# ----------------------------------------------------------------------------
def load_csv(path):
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"])          # server time (broker = UTC+offset)
    df["epoch"] = (df["time"].astype("int64") // 10**9).astype(np.int64)
    return df


def simple_atr_series(df, period=14):
    """Mirror of main_bot.calculate_atr (lines 478-509): SIMPLE mean of the last
    `period` true ranges (NOT Wilder). ATR[j] is 'as of the close of bar j',
    computed from closed bars only. Returns np array aligned to df rows."""
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    close = df["close"].to_numpy(float)
    prev_close = np.empty_like(close)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    tr[0] = np.nan  # first bar has no real prev close
    atr = pd.Series(tr).rolling(period).mean().to_numpy()  # mean of last `period` TRs
    return atr


# ----------------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------------
class Backtest:
    def __init__(self, cfg, args):
        self.cfg = cfg
        self.args = args
        self.gbpusd = float(args.gbpusd)
        self.commission_per_lot = float(args.commission)

        # ---- strategy & risk (REUSED) ----
        self.strategy = FusionStrategy(cfg)            # single instance, state persists
        self.risk = RiskManager(cfg)                   # for calculate_atr_based_stops

        t = cfg["TRADING"]
        self.lot_size = float(t["lot_size"])
        self.max_positions = int(t.get("max_positions", 1))
        self.window = int(t.get("order_execution", {}).get("market_data_bars", 250))

        # exits
        self.use_be = bool(t.get("use_smart_breakeven", True))
        self.be_trigger = float(t.get("breakeven_profit_multiple", 1.2))
        self.be_lock = float(t.get("breakeven_lock_profit_multiple", 0.3))
        self.use_trail = bool(t.get("use_trailing_stop", True))
        self.trail_dist = float(t.get("trailing_stop_atr_multiple", 2.0))
        self.trail_act = float(t.get("min_profit_for_trail_activation", 1.5))

        # volatility / scalp
        v = t.get("volatility_detection", {})
        self.vol_enabled = bool(v.get("enabled", False))
        self.atr_period = int(v.get("atr_period", 14))
        self.atr_scalp_threshold = float(v.get("atr_scalp_threshold", 2.0))
        self.scalp_target = float(v.get("scalp_profit_target_gbp", 26.82))
        self.scalp_cd = int(v.get("scalp_cooldown_seconds", 30))
        self.normal_cd = int(v.get("normal_cooldown_seconds", 60))
        self.skip_extreme = bool(v.get("skip_trading_when_atr_extreme", False))
        self.atr_max = v.get("atr_max_for_trading", None)
        # allow disabling scalp exit for diagnostics
        if args.no_scalp:
            self.vol_enabled_exit = False
        else:
            self.vol_enabled_exit = self.vol_enabled

        # trading hours
        th = t.get("trading_hours", {})
        self.th = {
            "saturday_closed": th.get("saturday_closed", True),
            "sunday_closed": th.get("sunday_closed", True),
            "friday_close_hour": th.get("friday_close_hour", 23),
        }

        # daily / weekly pause (check_daily_profit 783-1021, check_weekly_limits 1023-1135).
        # NOTE: gates NEW entries only; open positions are still managed. We use cumulative
        # *realised* NET P&L per day/week (live also has a floating-equity drawdown branch,
        # loss_limit_by_equity, which we do NOT model since positions close within a bar here).
        self.daily_target = float(t.get("daily_profit_target", 0))
        r = cfg.get("RISK", {})
        self.max_daily_loss = float(r.get("max_daily_loss", 0))
        self.weekly_enabled = bool(r.get("weekly_limits_enabled", False))
        self.max_weekly_profit = float(r.get("max_weekly_profit", 0))
        self.max_weekly_loss = float(r.get("max_weekly_loss", 0))
        self.cur_date = None; self.day_net = 0.0; self.paused_today = False
        self.cur_week = None; self.week_net = 0.0; self.paused_week = False

        # bias gate (mirror main_bot __init__ lines 88-94)
        smc = (cfg.get("STRATEGY", {}) or {}).get("SMC", {}) or {}
        self.strict_bias = bool(smc.get("strict_bias", True))

        self.offset_h = int(cfg.get("BROKER", {}).get("broker_timezone_offset", 0))

        # state
        self.last_trade_epoch = None
        self.last_trade_type = None
        self.last_market_bias = "NEUTRAL"
        self._last_m15_idx = None

        self.trades = []

    # ---- helpers ----
    def utc(self, server_epoch):
        return datetime.utcfromtimestamp(server_epoch - self.offset_h * 3600)

    def in_cooldown(self, eval_epoch):
        if self.last_trade_epoch is None:
            return False
        cd = self.scalp_cd if (self.vol_enabled and self.last_trade_type == "scalp") else self.normal_cd
        return (eval_epoch - self.last_trade_epoch) < cd

    def can_open_additional(self, open_positions):
        """Mirror main_bot._can_open_additional_position (lines 757-781)."""
        n = len(open_positions)
        if n == 0:
            return True
        if self.max_positions <= 1:
            return False
        if n >= self.max_positions:
            return False
        # stack a 2nd only if all existing are risk-free (SL at breakeven or better)
        return all(self._risk_free(p) for p in open_positions)

    @staticmethod
    def _risk_free(p):
        if p["sl"] <= 0:
            return False
        return p["sl"] >= p["entry"] if p["dir"] == "BUY" else p["sl"] <= p["entry"]

    def pnl_gbp(self, direction, entry, exit_price, lots):
        move = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
        usd = move * CONTRACT_SIZE * lots
        return usd / self.gbpusd

    def scalp_level(self, direction, entry, lots):
        # price move that yields scalp_target GBP of gross floating profit
        move = self.scalp_target * self.gbpusd / (CONTRACT_SIZE * lots)
        return entry + move if direction == "BUY" else entry - move

    # ---- per-bar position management (mirrors manage_positions 1410-1515) ----
    def manage_bar(self, pos, bar, mode_is_scalp):
        o, h, l = bar["open"], bar["high"], bar["low"]
        d = pos["dir"]
        scalp_on = self.vol_enabled_exit and mode_is_scalp

        if d == "BUY":
            # 1) SL first (conservative: SL before any profit exit)
            if l <= pos["sl"]:
                px = o if o <= pos["sl"] else pos["sl"]   # gap-through fill at open
                return px, "sl"
            # 2) scalp quick-profit (live priority over TP; check_quick_profit_exit 545-588)
            if scalp_on:
                lvl = self.scalp_level(d, pos["entry"], pos["lots"])
                if h >= lvl:
                    return (o if o >= lvl else lvl), "scalp"
            # 3) take profit
            if h >= pos["tp"]:
                return (o if o >= pos["tp"] else pos["tp"]), "tp"
            # 4) no exit -> update BE then trailing for subsequent bars
            self._update_stops(pos, h)
            return None, None
        else:  # SELL
            if h >= pos["sl"]:
                px = o if o >= pos["sl"] else pos["sl"]
                return px, "sl"
            if scalp_on:
                lvl = self.scalp_level(d, pos["entry"], pos["lots"])
                if l <= lvl:
                    return (o if o <= lvl else lvl), "scalp"
            if l <= pos["tp"]:
                return (o if o <= pos["tp"] else pos["tp"]), "tp"
            self._update_stops(pos, l)
            return None, None

    def _update_stops(self, pos, extreme):
        """BE then trailing, using the bar's favourable extreme. Mirrors
        apply_atr_breakeven (1450-1485) + apply_chandelier_trailing (1487-1515).
        Applied AFTER this bar's exit checks (deferred to next bar) to avoid
        intrabar look-ahead."""
        atr, entry, d = pos["atr"], pos["entry"], pos["dir"]
        if self.use_be and not pos["be_applied"] and atr > 0:
            if d == "BUY" and extreme >= entry + atr * self.be_trigger:
                ns = entry + atr * self.be_lock
                if ns > pos["sl"]:
                    pos["sl"] = ns; pos["be_applied"] = True
            elif d == "SELL" and extreme <= entry - atr * self.be_trigger:
                ns = entry - atr * self.be_lock
                if ns < pos["sl"]:
                    pos["sl"] = ns; pos["be_applied"] = True
        if self.use_trail and atr > 0:
            if d == "BUY" and extreme >= entry + atr * self.trail_act:
                cand = extreme - atr * self.trail_dist
                if cand > pos["sl"] and cand < extreme:
                    pos["sl"] = cand
            elif d == "SELL" and extreme <= entry - atr * self.trail_act:
                cand = extreme + atr * self.trail_dist
                if cand < pos["sl"] and cand > extreme:
                    pos["sl"] = cand

    def close_position(self, pos, exit_price, cause, exit_bar):
        reason = ("Quick scalp profit" if cause == "scalp"
                  else determine_close_reason(exit_price, pos["orig_sl"], pos["orig_tp"], pos["dir"]))
        gross = self.pnl_gbp(pos["dir"], pos["entry"], exit_price, pos["lots"])
        spread_cost = pos["spread_pts"] * POINT * CONTRACT_SIZE * pos["lots"] / self.gbpusd
        commission = self.commission_per_lot * pos["lots"]
        costs = spread_cost + commission
        self.trades.append({
            "entry_time": self.utc(pos["entry_epoch"]),
            "exit_time": self.utc(exit_bar["epoch"]),
            "direction": pos["dir"],
            "entry_price": round(pos["entry"], 3),
            "exit_price": round(exit_price, 3),
            "lots": pos["lots"],
            "atr": round(pos["atr"], 4),
            "stop": round(pos["orig_sl"], 3),
            "target": round(pos["orig_tp"], 3),
            "exit_reason": reason,
            "gross_pnl": round(gross, 2),
            "costs": round(costs, 2),
            "net_pnl": round(gross - costs, 2),
            "session": pos["session"],
            "volatility_mode": pos["vol_mode"],
        })
        if cause == "scalp":   # only scalp-close updates cooldown clock (besides open)
            self.last_trade_epoch = exit_bar["epoch"]
            self.last_trade_type = "scalp"
        # update daily/weekly realised NET tallies and pause flags
        net = (gross - costs)
        self.day_net += net
        self.week_net += net
        if self.daily_target > 0 and self.day_net >= self.daily_target:
            self.paused_today = True
        if self.max_daily_loss > 0 and self.day_net <= -self.max_daily_loss:
            self.paused_today = True
        if self.weekly_enabled:
            if self.max_weekly_profit > 0 and self.week_net >= self.max_weekly_profit:
                self.paused_week = True
            if self.max_weekly_loss > 0 and self.week_net <= -self.max_weekly_loss:
                self.paused_week = True

    def roll_calendar(self, eu):
        """Reset daily/weekly tallies on day/week boundaries (UTC ~ VPS local)."""
        d = eu.date()
        if self.cur_date != d:
            self.cur_date = d; self.day_net = 0.0; self.paused_today = False
        ws = d - timedelta(days=d.weekday())   # Monday-anchored week (week_start_day=monday)
        if self.cur_week != ws:
            self.cur_week = ws; self.week_net = 0.0; self.paused_week = False

    # ---- main loop ----
    def run(self, m1, m15, start_epoch, end_epoch):
        atr15 = simple_atr_series(m15, self.atr_period)
        m15_close = m15["epoch"].to_numpy() + 15 * 60          # bar closes 15 min after open
        m15_rec = m15[["epoch", "open", "high", "low", "close"]]

        o = m1["open"].to_numpy(float); h = m1["high"].to_numpy(float)
        l = m1["low"].to_numpy(float); c = m1["close"].to_numpy(float)
        ep = m1["epoch"].to_numpy(); sp = m1["spread"].to_numpy(float)
        tser = m1["time"].to_numpy()                            # server datetime64
        N = len(m1)

        open_positions = []
        pending = None
        W = self.window
        total_eval = (ep >= start_epoch).sum()
        done = 0
        next_progress = 0.1

        for j in range(N):
            bar_j = {"open": o[j], "high": h[j], "low": l[j], "epoch": int(ep[j])}
            self.roll_calendar(self.utc(int(ep[j])))   # daily/weekly reset on boundaries

            # (a) fill a pending entry at THIS bar's open
            if pending is not None:
                if len(open_positions) < self.max_positions:
                    entry = o[j]
                    atr = pending["atr"]
                    sl, tp = self.risk.calculate_atr_based_stops(entry, atr, pending["dir"])
                    if sl != 0 and tp != 0:
                        eu = self.utc(int(ep[j]))
                        open_positions.append({
                            "dir": pending["dir"], "lots": pending["lots"], "entry": entry,
                            "entry_epoch": int(ep[j]), "atr": atr,
                            "sl": sl, "tp": tp, "orig_sl": sl, "orig_tp": tp,
                            "be_applied": False, "session": current_session(eu),
                            "vol_mode": pending["vol_mode"], "spread_pts": sp[j],
                        })
                        self.last_trade_epoch = int(ep[j])      # open_trade lines 1374-1375
                        self.last_trade_type = "normal"
                pending = None

            # (b) manage open positions against this bar
            if open_positions:
                mode_scalp = self._mode_is_scalp(int(ep[j]), atr15, m15_close)
                still = []
                for pos in open_positions:
                    px, cause = self.manage_bar(pos, bar_j, mode_scalp)
                    if cause is not None:
                        self.close_position(pos, px, cause, bar_j)
                    else:
                        still.append(pos)
                open_positions = still

            # (c) signal evaluation at close of bar j -> pending entry for j+1
            if j + 1 < N and ep[j] >= start_epoch and ep[j] <= end_epoch:
                pending = self._maybe_signal(j, o, h, l, c, ep, sp, tser, atr15, m15_close,
                                             m15_rec, open_positions, W)

            if ep[j] >= start_epoch:
                done += 1
                if total_eval and done / total_eval >= next_progress:
                    print(f"  ... {int(next_progress*100)}% ({done}/{total_eval} bars, "
                          f"{len(self.trades)} trades)", flush=True)
                    next_progress += 0.1
            if ep[j] > end_epoch and not open_positions:
                break

        # force-close any still-open at last bar (mark to market)
        if open_positions:
            last = {"open": o[N-1], "high": h[N-1], "low": l[N-1], "epoch": int(ep[N-1])}
            for pos in open_positions:
                self.close_position(pos, c[N-1], "eod", last)

    def _mode_is_scalp(self, eval_epoch, atr15, m15_close):
        if not self.vol_enabled:
            return False
        a = self._atr_as_of(eval_epoch, atr15, m15_close)
        return a is not None and a > self.atr_scalp_threshold

    @staticmethod
    def _atr_as_of(eval_epoch, atr15, m15_close):
        idx = np.searchsorted(m15_close, eval_epoch, side="right") - 1  # last closed M15 bar
        if idx < 0:
            return None
        a = atr15[idx]
        return None if np.isnan(a) else float(a)

    def _maybe_signal(self, j, o, h, l, c, ep, sp, tser, atr15, m15_close,
                      m15_rec, open_positions, W):
        """Returns a pending-entry dict (filled at j+1 open) or None."""
        eval_epoch = int(ep[j + 1])          # decision happens at close of j ~ open of j+1
        eu = self.utc(eval_epoch)
        if not within_trading_hours(eu, self.th):
            return None
        if not self.args.no_caps and (self.paused_today or self.paused_week):
            return None   # daily target / daily loss / weekly cap pause
        if self.in_cooldown(eval_epoch):
            return None
        if not self.can_open_additional(open_positions):
            return None
        atr = self._atr_as_of(eval_epoch, atr15, m15_close)
        if atr is None:
            return None
        if self.skip_extreme and self.atr_max is not None and atr > float(self.atr_max):
            return None  # extreme volatility skip (run loop lines 1622-1634)
        if j < W:
            return None

        # ---- structure bias, recomputed only on a new closed M15 bar (run 1690-1710) ----
        m15_idx = np.searchsorted(m15_close, eval_epoch, side="right") - 1
        if m15_idx >= 50 and m15_idx != self._last_m15_idx:
            self._last_m15_idx = m15_idx
            lo = max(0, m15_idx - max(250, W) + 1)
            recs = m15_rec.iloc[lo:m15_idx + 1].to_dict("records")  # epoch + ohlc
            try:
                info = self.strategy.compute_structure_bias_from_rates(recs)
                self.last_market_bias = info.get("bias", "NEUTRAL") if isinstance(info, dict) else "NEUTRAL"
            except Exception as e:
                print("bias error:", e)
                self.last_market_bias = "NEUTRAL"

        # strict bias gate (run loop lines 1712-1718)
        if self.strict_bias and self.last_market_bias not in ("BULL", "BEAR"):
            return None

        # ---- ask the (reused) strategy for a signal on the M1 window ----
        s = j - W + 1
        win = pd.DataFrame({
            "time": tser[s:j + 1],
            "open": o[s:j + 1], "high": h[s:j + 1],
            "low": l[s:j + 1], "close": c[s:j + 1],
        })
        signal = self.strategy.analyze(win, bias=self.last_market_bias)
        if not signal:
            return None

        lots = position_size(signal, atr, self.cfg)
        mode = "scalp" if self._mode_is_scalp(eval_epoch, atr15, m15_close) else "normal"
        return {"dir": signal["type"], "lots": lots, "atr": atr, "vol_mode": mode}


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------
def summarize(trades, label, gross_key="gross_pnl", net_key="net_pnl"):
    n = len(trades)
    print(f"\n================ SUMMARY: {label} ================")
    if n == 0:
        print("No trades.")
        return
    df = pd.DataFrame(trades)
    for which, key in (("GROSS", gross_key), ("NET", net_key)):
        pnl = df[key]
        wins = pnl[pnl > 0]; losses = pnl[pnl < 0]
        gross_win = wins.sum(); gross_loss = -losses.sum()
        pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
        wr = 100.0 * len(wins) / n
        exp = pnl.mean()
        # max drawdown on cumulative equity
        eq = pnl.cumsum()
        dd = (eq - eq.cummax()).min()
        print(f"\n--- {which} ---")
        print(f"  Trades            : {n}")
        print(f"  Win rate          : {wr:.1f}%  ({len(wins)}W / {len(losses)}L)")
        print(f"  Profit factor     : {pf:.3f}")
        print(f"  Average win       : {wins.mean() if len(wins) else 0:.2f}")
        print(f"  Average loss      : {losses.mean() if len(losses) else 0:.2f}")
        print(f"  Expectancy/trade  : {exp:.2f}")
        print(f"  Largest win       : {pnl.max():.2f}")
        print(f"  Largest loss      : {pnl.min():.2f}")
        print(f"  Max drawdown      : {dd:.2f}")
        print(f"  Net P&L           : {pnl.sum():.2f}")

    print("\n--- by exit_reason (net) ---")
    by_r = df.groupby("exit_reason")[net_key].agg(["count", "sum", "mean"])
    print(by_r.to_string())
    print("\n--- by session (net) ---")
    by_s = df.groupby("session")[net_key].agg(["count", "sum", "mean"])
    print(by_s.to_string())
    print("\n--- direction mix ---")
    print(df["direction"].value_counts().to_string())


def write_outputs(trades, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    df = pd.DataFrame(trades)
    tpath = os.path.join(out_dir, "trades.csv")
    df.to_csv(tpath, index=False)
    eq = df[["exit_time"]].copy() if len(df) else pd.DataFrame(columns=["exit_time"])
    if len(df):
        eq["gross_cum"] = df["gross_pnl"].cumsum()
        eq["net_cum"] = df["net_pnl"].cumsum()
    epath = os.path.join(out_dir, "equity_curve.csv")
    eq.to_csv(epath, index=False)
    print(f"\nWrote {tpath} ({len(df)} rows) and {epath}")


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-06-05")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--commission", default=5.50, type=float,
                    help="GBP per lot per round turn (default 5.50)")
    ap.add_argument("--gbpusd", default=1.34, type=float,
                    help="USD->GBP conversion (default 1.34, ~Jan 2026)")
    ap.add_argument("--smc", choices=["on", "off"], default=None,
                    help="override SMC.enabled")
    ap.add_argument("--min-conditions", type=int, default=None,
                    help="override STRATEGY.min_conditions_required")
    ap.add_argument("--no-scalp", action="store_true",
                    help="disable the scalp quick-profit exit (diagnostic)")
    ap.add_argument("--no-caps", action="store_true",
                    help="disable daily/weekly profit-loss pause (diagnostic)")
    ap.add_argument("--out", default=os.path.join(ROOT, "results"))
    ap.add_argument("--validate", action="store_true",
                    help="shortcut: 2026-01-12..2026-01-22, SMC off")
    args = ap.parse_args()

    if args.validate:
        args.start, args.end = "2026-01-12", "2026-01-22"
        if args.smc is None:
            args.smc = "off"

    with open(args.config) as f:
        cfg = json.load(f)

    # config overrides
    if args.smc is not None:
        cfg.setdefault("STRATEGY", {}).setdefault("SMC", {})["enabled"] = (args.smc == "on")
    if args.min_conditions is not None:
        cfg.setdefault("STRATEGY", {})["min_conditions_required"] = args.min_conditions

    smc_on = cfg.get("STRATEGY", {}).get("SMC", {}).get("enabled", False)
    minc = cfg.get("STRATEGY", {}).get("min_conditions_required")
    print(f"Config: SMC.enabled={smc_on} | min_conditions_required={minc} | "
          f"lot={cfg['TRADING']['lot_size']} | scalp_exit={'off' if args.no_scalp else 'on'} | "
          f"gbpusd={args.gbpusd} | commission={args.commission}/lot")
    print(f"Window: {args.start} -> {args.end}")

    print("Loading data ...", flush=True)
    m1 = load_csv(M1_CSV)
    m15 = load_csv(M15_CSV)

    start_dt = datetime.fromisoformat(args.start)
    end_dt = datetime.fromisoformat(args.end) + timedelta(days=1)  # inclusive end day
    offset = int(cfg.get("BROKER", {}).get("broker_timezone_offset", 0))
    # start/end given in UTC-ish bot time; convert to server epoch for comparison
    start_epoch = int(start_dt.timestamp()) + offset * 3600
    end_epoch = int(end_dt.timestamp()) + offset * 3600

    # slice with warmup buffer (10 days) to keep the loop fast
    buf = 10 * 86400
    lo = start_epoch - buf
    m1 = m1[(m1["epoch"] >= lo) & (m1["epoch"] <= end_epoch + 86400)].reset_index(drop=True)
    m15 = m15[(m15["epoch"] >= lo - 5 * 86400) & (m15["epoch"] <= end_epoch + 86400)].reset_index(drop=True)
    print(f"M1 bars in scope: {len(m1)} | M15 bars in scope: {len(m15)}", flush=True)

    bt = Backtest(cfg, args)
    print("Running engine ...", flush=True)
    bt.run(m1, m15, start_epoch, end_epoch)

    write_outputs(bt.trades, args.out)
    summarize(bt.trades, f"{args.start}..{args.end} (SMC {'on' if smc_on else 'off'}, minc={minc})")


if __name__ == "__main__":
    main()
