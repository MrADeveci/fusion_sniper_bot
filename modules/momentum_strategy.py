"""
Momentum Breakout Strategy - Fusion Sniper Bot v5.0.0

A self-contained, validated trend-following breakout strategy that is the SINGLE SOURCE
OF TRUTH for entries, stops, the trailing exit and risk-based sizing, imported by BOTH
the live bot (main_bot.py) and the offline backtester (tools/backtest_mom.py) so the bot
provably makes the same decisions that were validated.

Validated method (lab, walk-forward 2021-2023 in-sample / 2024-2026 out-of-sample):
  - Medium-term H4 trend filter: H4 close above its EMA(50) => longs allowed; below => shorts.
    (Robust across EMA 40-70; 50 is the robust-centre value.)
  - M15 breakout: long when M15 close > highest high of the prior `breakout_lookback`
    (default 20) M15 bars; short on a break below the prior-N low; only in the H4-trend
    direction.
  - Exit (validated "Variant 2"): SL = sl_atr_mult x ATR(M15) (default 1.5), NO fixed take
    profit, ratcheting ATR trailing stop = trail_atr_mult x ATR (default 3.5, mid-shelf for
    robustness margin), activating once price is trail_activation_atr x ATR in profit.
    No scalp quick-profit, no breakeven.
  - Risk-based sizing: lots derived from the stop distance so each trade risks a set amount
    (flat amount for backtest parity, percent-of-equity for live).
  - Session: enter only between session_start_uk and session_end_uk UK local time. One
    position at a time.

This module has NO MetaTrader5 dependency so it stays pure and testable.
"""
from __future__ import annotations

from typing import Optional, Dict, Any

import pandas as pd


DEFAULTS = dict(
    h4_ema=50,
    breakout_lookback=20,
    atr_period=14,
    sl_atr_mult=1.5,
    trail_atr_mult=3.5,
    trail_activation_atr=1.0,
    session_start_uk=7,
    session_end_uk=18,
    sizing_mode="percent_equity",   # "percent_equity" or "flat"
    risk_percent=0.5,               # percent of equity per trade (live default)
    risk_flat_gbp=50.0,             # flat amount per trade (backtest parity)
    gbpusd=1.34,                    # USD->GBP conversion for sizing
    price_digits=2,                 # XAUUSD
)


class MomentumBreakoutStrategy:
    """H4-trend-filtered M15 breakout with a ratcheting ATR trailing exit."""

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        p = dict(DEFAULTS)
        if params:
            p.update({k: v for k, v in params.items() if v is not None})
        self.h4_ema = int(p["h4_ema"])
        self.lookback = int(p["breakout_lookback"])
        self.atr_period = int(p["atr_period"])
        self.sl_mult = float(p["sl_atr_mult"])
        self.trail_mult = float(p["trail_atr_mult"])
        self.trail_act = float(p["trail_activation_atr"])
        self.sess_start = int(p["session_start_uk"])
        self.sess_end = int(p["session_end_uk"])
        self.sizing_mode = str(p["sizing_mode"])
        self.risk_percent = float(p["risk_percent"])
        self.risk_flat_gbp = float(p["risk_flat_gbp"])
        self.gbpusd = float(p["gbpusd"])
        self.digits = int(p["price_digits"])

    # ------------------------------------------------------------------
    # Trend filter (H4)
    # ------------------------------------------------------------------
    def ema_series(self, close: pd.Series) -> pd.Series:
        """EMA used by both callers (same span/formula = shared definition)."""
        return close.ewm(span=self.h4_ema, adjust=False).mean()

    @staticmethod
    def trend_sign(close_val: float, ema_val: float) -> int:
        if close_val > ema_val:
            return 1
        if close_val < ema_val:
            return -1
        return 0

    def compute_h4_trend(self, h4_df: pd.DataFrame) -> int:
        """+1 = uptrend (longs allowed), -1 = downtrend (shorts), 0 = neutral."""
        if h4_df is None or len(h4_df) < self.h4_ema:
            return 0
        ema = self.ema_series(h4_df["close"])
        return self.trend_sign(float(h4_df["close"].iloc[-1]), float(ema.iloc[-1]))

    # ------------------------------------------------------------------
    # Entry decision (single source of truth for both callers)
    # ------------------------------------------------------------------
    @staticmethod
    def decide_entry(trend_sign: int, m15_close: float,
                     prior_high: float, prior_low: float) -> Optional[str]:
        if trend_sign > 0 and m15_close > prior_high:
            return "BUY"
        if trend_sign < 0 and m15_close < prior_low:
            return "SELL"
        return None

    def signal(self, m15_df: pd.DataFrame, h4_df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """df-in / signal-out, mirroring how FusionStrategy is consumed by the bot.

        The last row of m15_df is the just-closed signal bar; the prior `lookback`
        bars (excluding it) define the breakout levels.
        """
        if m15_df is None or len(m15_df) < self.lookback + 1:
            return None
        trend = self.compute_h4_trend(h4_df)
        if trend == 0:
            return None
        prior = m15_df.iloc[-(self.lookback + 1):-1]
        prior_high = float(prior["high"].max())
        prior_low = float(prior["low"].min())
        close = float(m15_df["close"].iloc[-1])
        direction = self.decide_entry(trend, close, prior_high, prior_low)
        if direction is None:
            return None
        return {
            "type": direction,
            "conditions_met": 2,
            "conditions_detail": [
                f"H4_TREND_{'UP' if trend > 0 else 'DOWN'}",
                f"M15_BREAKOUT_{self.lookback}",
            ],
            "confidence": 1.0,
        }

    # ------------------------------------------------------------------
    # Stops, trailing, sizing (shared exit code path)
    # ------------------------------------------------------------------
    def initial_stop(self, entry: float, atr: float, direction: str) -> float:
        if direction == "BUY":
            return round(entry - self.sl_mult * atr, self.digits)
        return round(entry + self.sl_mult * atr, self.digits)

    def update_trailing_stop(self, direction: str, entry: float, current_sl: float,
                             run_extreme: float, atr: float) -> float:
        """Ratcheting ATR trailing stop. Activates once price is trail_activation_atr x ATR
        in profit, then trails trail_atr_mult x ATR behind the favourable extreme. Only ever
        moves in the favourable direction (ratchet)."""
        if direction == "BUY":
            if run_extreme >= entry + self.trail_act * atr:
                cand = round(run_extreme - self.trail_mult * atr, self.digits)
                if cand > current_sl:
                    return cand
        else:
            if run_extreme <= entry - self.trail_act * atr:
                cand = round(run_extreme + self.trail_mult * atr, self.digits)
                if cand < current_sl:
                    return cand
        return current_sl

    def in_session(self, uk_hour: int) -> bool:
        return self.sess_start <= uk_hour < self.sess_end

    def risk_amount(self, equity: Optional[float]) -> float:
        """Return the GBP amount to risk on a trade for the configured sizing mode."""
        if self.sizing_mode == "percent_equity" and equity is not None and equity > 0:
            return float(equity) * self.risk_percent / 100.0
        return self.risk_flat_gbp

    def lots_for_risk(self, risk_gbp: float, atr: float, contract_size: float = 100.0,
                      vol_step: float = 0.01, vol_min: float = 0.01,
                      vol_max: float = 100.0) -> float:
        """Lots so that a stop-out (sl_mult x ATR) loses ~risk_gbp, normalised to the
        broker's volume step/min/max."""
        stop_dist = self.sl_mult * atr
        if stop_dist <= 0:
            return vol_min
        lots = risk_gbp * self.gbpusd / (stop_dist * contract_size)
        lots = round(lots / vol_step) * vol_step
        lots = max(vol_min, min(vol_max, lots))
        decimals = len(str(vol_step).split(".")[-1]) if "." in str(vol_step) else 0
        return round(lots, decimals)
