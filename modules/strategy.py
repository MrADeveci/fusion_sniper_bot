"""
Fusion Sniper Bot Strategy v4.4
Adds optional Smart Money Concepts (SMC) module:
  - CHoCH style bias using fractal swing breaks on the bias timeframe
  - Fair Value Gap (FVG) zones on the entry timeframe
  - Entry only after a rejection and a close back out of the FVG in the bias direction
Fallback indicator stack remains available (EMA, RSI, ADX, Stochastic, Bollinger).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from ta.trend import EMAIndicator, ADXIndicator
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import BollingerBands


@dataclass
class FVGZone:
    direction: str  # "BULL" or "BEAR"
    low: float      # lower bound of the zone
    high: float     # upper bound of the zone
    created_time: pd.Timestamp
    created_index: int
    used: bool = False


class FusionStrategy:
    """Trading strategy with independent BUY and SELL evaluation."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.strategy_config = config.get("STRATEGY", {}) or {}

        # Optional debug flag, default False if not in config
        self.debug_signals = bool(self.strategy_config.get("debug_signals", False))

        # Base indicator strategy thresholds
        self.min_conditions = int(self.strategy_config.get("min_conditions_required", 3))

        # Trend-related condition labels (must match conditions_detail)
        self.trend_flags = {"ABOVE_TREND", "BELOW_TREND", "STRONG_TREND"}

        # ------------------------------------------------------------------
        # Trend filter config (existing behaviour preserved)
        # ------------------------------------------------------------------
        default_trend_filter = {
            "enabled": True,
            "scope": "window",  # "window" or "always"
            "window": {"weekday": 3, "start_hour": 0, "end_hour": 8},
            "require_trend_flag": True,
            "buy_extra_conditions": 0,
            "sell_extra_conditions": 0,
        }

        user_trend_filter = self.strategy_config.get("trend_filter", None)
        if isinstance(user_trend_filter, dict):
            merged = dict(default_trend_filter)
            merged.update(user_trend_filter)
            window_cfg = dict(default_trend_filter.get("window", {}))
            window_cfg.update(user_trend_filter.get("window", {}) or {})
            merged["window"] = window_cfg
            self.trend_filter = merged
        elif user_trend_filter is None:
            self.trend_filter = default_trend_filter
        else:
            self.trend_filter = default_trend_filter

        # ------------------------------------------------------------------
        # Indicator parameters
        # ------------------------------------------------------------------
        def _pick(keys, default):
            for k in keys:
                if k in self.strategy_config:
                    return self.strategy_config.get(k)
            return default

        self.ema_fast = int(_pick(["ema_fast_period", "ema_20_period"], 21))
        self.ema_slow = int(_pick(["ema_slow_period", "ema_50_period"], 50))
        self.ema_trend = int(_pick(["ema_trend_period", "ema_200_period"], 200))

        self.rsi_period = int(self.strategy_config.get("rsi_period", 14))
        self.rsi_oversold = float(self.strategy_config.get("rsi_oversold", 30))
        self.rsi_overbought = float(self.strategy_config.get("rsi_overbought", 70))

        self.adx_period = int(self.strategy_config.get("adx_period", 14))
        self.adx_threshold = float(self.strategy_config.get("adx_threshold", 25))

        self.stoch_k = int(self.strategy_config.get("stochastic_k", 14))
        self.stoch_d = int(self.strategy_config.get("stochastic_d", 3))
        self.stoch_oversold = float(self.strategy_config.get("stochastic_oversold", 20))
        self.stoch_overbought = float(self.strategy_config.get("stochastic_overbought", 80))

        self.bb_period = int(_pick(["bollinger_period", "bb_period"], 20))
        self.bb_std = float(_pick(["bollinger_std", "bb_std_dev"], 2))

        # ------------------------------------------------------------------
        # SMC module config (optional)
        # ------------------------------------------------------------------
        smc_cfg = self.strategy_config.get("SMC", {}) if isinstance(self.strategy_config, dict) else {}
        self.smc_enabled = bool(smc_cfg.get("enabled", False))
        self.smc_only = bool(smc_cfg.get("smc_only", True)) if self.smc_enabled else False

        # CHoCH bias (computed externally by bot on bias timeframe, or via helper)
        self.persist_bias = bool(smc_cfg.get("persist_bias", True))
        self.fractal_left_right = int(smc_cfg.get("fractal_left_right", 2))

        # FVG behaviour
        self.fvg_enabled = bool(smc_cfg.get("use_fvg_entries", True)) if self.smc_enabled else False
        self.fvg_max_age_bars = int(smc_cfg.get("fvg_max_age_bars", 40))
        self.fvg_min_size_atr_mult = float(smc_cfg.get("fvg_min_size_atr_mult", 0.15))
        self.fvg_wick_body_ratio = float(smc_cfg.get("fvg_rejection_wick_ratio", 0.5))
        self.fvg_require_candle_direction = bool(smc_cfg.get("fvg_require_candle_direction", True))

        # State
        self.last_structure_bias: str = "NEUTRAL"  # "BULL", "BEAR", "NEUTRAL"
        self._active_fvgs: List[FVGZone] = []
        self._fvg_seen_keys: set = set()

    # ------------------------------------------------------------------
    # Trend filter helpers (existing)
    # ------------------------------------------------------------------
    def _is_trend_filter_window_active(self, current_time):
        try:
            if current_time is None or pd.isna(current_time):
                return False

            ts = pd.to_datetime(current_time)
            weekday = ts.weekday()
            hour = ts.hour

            window_cfg = (self.trend_filter or {}).get("window", {}) or {}
            target_weekday = int(window_cfg.get("weekday", 3))
            start_hour = int(window_cfg.get("start_hour", 0))
            end_hour = int(window_cfg.get("end_hour", 8))

            if weekday != target_weekday:
                return False

            if start_hour <= end_hour:
                return start_hour <= hour < end_hour
            return hour >= start_hour or hour < end_hour

        except Exception:
            return False

    def _is_trend_filter_active(self, current_time):
        cfg = getattr(self, "trend_filter", {}) or {}
        if not cfg.get("enabled", False):
            return False

        scope = str(cfg.get("scope", "always")).lower().strip()
        if scope == "always":
            return True
        if scope in {"window", "time_window", "session"}:
            return self._is_trend_filter_window_active(current_time)
        return False

    def _is_signal_allowed(self, side, current_time, conditions_met, conditions_detail):
        min_required = self.min_conditions
        require_trend_flag = False

        cfg = getattr(self, "trend_filter", {}) or {}

        if self._is_trend_filter_active(current_time):
            require_trend_flag = bool(cfg.get("require_trend_flag", True))
            buy_extra = int(cfg.get("buy_extra_conditions", 0) or 0)
            sell_extra = int(cfg.get("sell_extra_conditions", 0) or 0)
            if side == "BUY":
                min_required = self.min_conditions + max(0, buy_extra)
            elif side == "SELL":
                min_required = self.min_conditions + max(0, sell_extra)

        if conditions_met < min_required:
            return False

        if require_trend_flag:
            if not any(flag in self.trend_flags for flag in conditions_detail):
                return False

        return True

    # ------------------------------------------------------------------
    # SMC helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _compute_atr(df: pd.DataFrame, period: int = 14) -> float:
        """Lightweight ATR estimate for size filters."""
        try:
            if df is None or len(df) < period + 2:
                return float("nan")

            high = df["high"]
            low = df["low"]
            close = df["close"]
            prev_close = close.shift(1)

            tr = pd.concat(
                [
                    (high - low).abs(),
                    (high - prev_close).abs(),
                    (low - prev_close).abs(),
                ],
                axis=1,
            ).max(axis=1)

            atr = tr.rolling(period).mean().iloc[-1]
            return float(atr)
        except Exception:
            return float("nan")

    def _find_fractal_swings(self, df: pd.DataFrame, lr: int) -> Tuple[List[int], List[int]]:
        """Return (swing_high_indices, swing_low_indices) using a simple fractal rule."""
        swing_highs: List[int] = []
        swing_lows: List[int] = []

        if df is None or len(df) < (lr * 2 + 5):
            return swing_highs, swing_lows

        highs = df["high"].reset_index(drop=True)
        lows = df["low"].reset_index(drop=True)

        for i in range(lr, len(df) - lr):
            h = highs.iloc[i]
            if h == highs.iloc[i - lr : i + lr + 1].max() and h > highs.iloc[i - lr : i].max() and h > highs.iloc[i + 1 : i + lr + 1].max():
                swing_highs.append(i)

            l = lows.iloc[i]
            if l == lows.iloc[i - lr : i + lr + 1].min() and l < lows.iloc[i - lr : i].min() and l < lows.iloc[i + 1 : i + lr + 1].min():
                swing_lows.append(i)

        return swing_highs, swing_lows

    def compute_structure_bias(self, bias_df: pd.DataFrame) -> Dict[str, Any]:
        """Compute structure bias from a bias timeframe DataFrame.

        Bias is updated when price breaks the last confirmed fractal swing high or low.
        A flip against the current bias is tagged as CHoCH.
        """
        info: Dict[str, Any] = {
            "bias": self.last_structure_bias,
            "event": None,
            "last_swing_high": None,
            "last_swing_low": None,
        }

        if bias_df is None or len(bias_df) < 50:
            return info

        df = bias_df.copy()
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], errors="coerce")

        lr = max(1, int(self.fractal_left_right))
        swing_highs, swing_lows = self._find_fractal_swings(df, lr)

        if swing_highs:
            i_hi = swing_highs[-1]
            info["last_swing_high"] = float(df["high"].iloc[i_hi])
        if swing_lows:
            i_lo = swing_lows[-1]
            info["last_swing_low"] = float(df["low"].iloc[i_lo])

        close = float(df["close"].iloc[-1])
        prev_bias = self.last_structure_bias

        new_bias = prev_bias
        event = None

        last_hi = info["last_swing_high"]
        last_lo = info["last_swing_low"]

        if last_hi is not None and close > last_hi:
            new_bias = "BULL"
            if prev_bias == "BEAR":
                event = "CHOCH_BULL"
        elif last_lo is not None and close < last_lo:
            new_bias = "BEAR"
            if prev_bias == "BULL":
                event = "CHOCH_BEAR"
        else:
            if not self.persist_bias:
                new_bias = "NEUTRAL"

        self.last_structure_bias = new_bias
        info["bias"] = new_bias
        info["event"] = event
        return info

    def compute_structure_bias_from_rates(self, rates) -> Dict[str, Any]:
        if rates is None or len(rates) < 50:
            return {"bias": self.last_structure_bias}

        df = pd.DataFrame(rates)
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], unit="s", errors="coerce")
        return self.compute_structure_bias(df)

    def _seed_fvg_zones(self, df: pd.DataFrame, lookback: int = 120):
        """Seed FVG zones after a restart."""
        start = max(2, len(df) - lookback)
        for i in range(start, len(df)):
            self._maybe_add_fvg(df, i)

    def _maybe_add_fvg(self, df: pd.DataFrame, i: int):
        """Detect and add an FVG using the 3-candle definition at index i."""
        if i < 2:
            return

        t = df["time"].iloc[i] if "time" in df.columns else pd.Timestamp.utcnow()
        if not isinstance(t, pd.Timestamp):
            t = pd.to_datetime(t, errors="coerce") or pd.Timestamp.utcnow()

        hi_2 = float(df["high"].iloc[i - 2])
        lo_2 = float(df["low"].iloc[i - 2])
        hi = float(df["high"].iloc[i])
        lo = float(df["low"].iloc[i])

        # Bullish FVG: low[i] > high[i-2]
        if lo > hi_2:
            zone_low, zone_high = hi_2, lo
            direction = "BULL"
        # Bearish FVG: high[i] < low[i-2]
        elif hi < lo_2:
            zone_low, zone_high = hi, lo_2
            direction = "BEAR"
        else:
            return

        if zone_high <= zone_low:
            return

        # Size filter (relative to entry timeframe ATR)
        atr = self._compute_atr(df, period=14)
        if pd.notna(atr) and atr > 0:
            if (zone_high - zone_low) < (atr * self.fvg_min_size_atr_mult):
                return

        key = (direction, round(zone_low, 6), round(zone_high, 6), pd.to_datetime(t).value)
        if key in self._fvg_seen_keys:
            return
        self._fvg_seen_keys.add(key)

        self._active_fvgs.append(
            FVGZone(
                direction=direction,
                low=float(zone_low),
                high=float(zone_high),
                created_time=pd.to_datetime(t),
                created_index=int(i),
                used=False,
            )
        )

    def _update_fvg_zones(self, df: pd.DataFrame):
        if df is None or len(df) < 10:
            return

        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], errors="coerce")

        # Seed once
        if not self._active_fvgs:
            self._seed_fvg_zones(df)

        # Add the newest potential FVG (based on the latest closed candle)
        self._maybe_add_fvg(df, len(df) - 1)

        # Expire old zones
        max_age = max(5, int(self.fvg_max_age_bars))
        current_idx = len(df) - 1
        self._active_fvgs = [
            z for z in self._active_fvgs
            if (not z.used) and ((current_idx - z.created_index) <= max_age)
        ]

    def _fvg_rejection_signal(self, df: pd.DataFrame, bias: str) -> Optional[Dict[str, Any]]:
        """Return a trade signal if the latest candle confirms an FVG retest + rejection."""
        if df is None or len(df) < 5:
            return None

        last = df.iloc[-1]
        o = float(last["open"])
        h = float(last["high"])
        l = float(last["low"])
        c = float(last["close"])
        t = pd.to_datetime(last["time"]) if "time" in df.columns else pd.Timestamp.utcnow()

        body = abs(c - o)
        body = max(body, 1e-9)

        # Prefer the most recent zones first
        zones = list(reversed(self._active_fvgs))

        if bias == "BULL":
            for z in zones:
                if z.direction != "BULL" or z.used:
                    continue

                intersects = (l <= z.high) and (h >= z.low)
                closes_out = c > z.high
                if not (intersects and closes_out):
                    continue

                if self.fvg_require_candle_direction and not (c > o):
                    continue

                lower_wick = min(o, c) - l
                if lower_wick < (body * self.fvg_wick_body_ratio):
                    continue

                z.used = True
                return {
                    "type": "BUY",
                    "confidence": 0.85,
                    "conditions_met": 5,
                    "conditions_detail": [
                        "SMC_BIAS_BULL",
                        "FVG_RETEST",
                        "FVG_REJECTION_CLOSE_OUT",
                        f"FVG_ZONE:{z.low:.5f}-{z.high:.5f}",
                        f"TIME:{t}",
                    ],
                }

        if bias == "BEAR":
            for z in zones:
                if z.direction != "BEAR" or z.used:
                    continue

                intersects = (h >= z.low) and (l <= z.high)
                closes_out = c < z.low
                if not (intersects and closes_out):
                    continue

                if self.fvg_require_candle_direction and not (c < o):
                    continue

                upper_wick = h - max(o, c)
                if upper_wick < (body * self.fvg_wick_body_ratio):
                    continue

                z.used = True
                return {
                    "type": "SELL",
                    "confidence": 0.85,
                    "conditions_met": 5,
                    "conditions_detail": [
                        "SMC_BIAS_BEAR",
                        "FVG_RETEST",
                        "FVG_REJECTION_CLOSE_OUT",
                        f"FVG_ZONE:{z.low:.5f}-{z.high:.5f}",
                        f"TIME:{t}",
                    ],
                }

        return None

    # ------------------------------------------------------------------
    # Public API used by the bot
    # ------------------------------------------------------------------
    def analyze_from_rates(self, rates, bias: Optional[str] = None):
        """Takes raw MT5 rates, builds DataFrame, then runs main analyze logic."""
        if rates is None or len(rates) < 50:
            return None

        df = pd.DataFrame(rates)
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], unit="s", errors="coerce")

        return self.analyze(df, bias=bias)

    def analyze(self, data: pd.DataFrame, bias: Optional[str] = None):
        """Analyse market data and return a signal dict or None."""
        try:
            if data is None or len(data) < 50:
                return None

            df = data.copy()

            # If SMC is enabled, try SMC entries first
            if self.smc_enabled and self.fvg_enabled:
                self._update_fvg_zones(df)
                bias_val = str(bias or self.last_structure_bias or "NEUTRAL").upper().strip()
                smc_signal = self._fvg_rejection_signal(df, bias=bias_val)
                if smc_signal:
                    if self.debug_signals:
                        print(f"[DEBUG] SMC signal {smc_signal['type']} | bias={bias_val}")
                    return smc_signal

                if self.smc_only:
                    return None

            # ------------------------------------------------------------------
            # Indicator stack (fallback)
            # ------------------------------------------------------------------
            ema_fast = EMAIndicator(close=df["close"], window=self.ema_fast).ema_indicator()
            ema_slow = EMAIndicator(close=df["close"], window=self.ema_slow).ema_indicator()
            ema_trend = EMAIndicator(close=df["close"], window=self.ema_trend).ema_indicator()

            rsi = RSIIndicator(close=df["close"], window=self.rsi_period).rsi()

            adx_indicator = ADXIndicator(
                high=df["high"],
                low=df["low"],
                close=df["close"],
                window=self.adx_period,
            )
            adx = adx_indicator.adx()

            stoch = StochasticOscillator(
                high=df["high"],
                low=df["low"],
                close=df["close"],
                window=self.stoch_k,
                smooth_window=self.stoch_d,
            )
            stoch_k = stoch.stoch()
            stoch_d = stoch.stoch_signal()

            bb = BollingerBands(
                close=df["close"],
                window=self.bb_period,
                window_dev=self.bb_std,
            )
            bb_upper = bb.bollinger_hband()
            bb_lower = bb.bollinger_lband()

            current_price = float(df["close"].iloc[-1])
            current_ema_fast = float(ema_fast.iloc[-1])
            current_ema_slow = float(ema_slow.iloc[-1])
            current_ema_trend = float(ema_trend.iloc[-1])
            current_rsi = float(rsi.iloc[-1])
            current_adx = float(adx.iloc[-1])
            current_stoch_k = float(stoch_k.iloc[-1])
            current_stoch_d = float(stoch_d.iloc[-1])
            current_bb_upper = float(bb_upper.iloc[-1])
            current_bb_lower = float(bb_lower.iloc[-1])

            current_time = None
            if "time" in df.columns:
                current_time = df["time"].iloc[-1]

            # Independent BUY evaluation
            buy_conditions = 0
            buy_details: List[str] = []

            if current_ema_fast > current_ema_slow:
                buy_conditions += 1
                buy_details.append("EMA_CROSS")

            if current_price > current_ema_trend:
                buy_conditions += 1
                buy_details.append("ABOVE_TREND")

            if current_rsi < self.rsi_oversold:
                buy_conditions += 1
                buy_details.append("RSI_OVERSOLD")

            if current_adx > self.adx_threshold:
                buy_conditions += 1
                buy_details.append("STRONG_TREND")

            if current_stoch_k < self.stoch_oversold and current_stoch_k > current_stoch_d:
                buy_conditions += 1
                buy_details.append("STOCH_BULLISH")

            # Independent SELL evaluation
            sell_conditions = 0
            sell_details: List[str] = []

            if current_ema_fast < current_ema_slow:
                sell_conditions += 1
                sell_details.append("EMA_CROSS")

            if current_price < current_ema_trend:
                sell_conditions += 1
                sell_details.append("BELOW_TREND")

            if current_rsi > self.rsi_overbought:
                sell_conditions += 1
                sell_details.append("RSI_OVERBOUGHT")

            if current_adx > self.adx_threshold:
                sell_conditions += 1
                sell_details.append("STRONG_TREND")

            if current_stoch_k > self.stoch_overbought and current_stoch_k < current_stoch_d:
                sell_conditions += 1
                sell_details.append("STOCH_BEARISH")

            if self.debug_signals:
                try:
                    print(
                        f"[DEBUG] BUY {buy_conditions}/5 {buy_details} | "
                        f"SELL {sell_conditions}/5 {sell_details} | "
                        f"price={current_price:.2f} RSI={current_rsi:.1f} ADX={current_adx:.1f} "
                        f"BB({current_bb_lower:.2f}-{current_bb_upper:.2f})"
                    )
                except Exception:
                    pass

            if self._is_signal_allowed(
                side="BUY",
                current_time=current_time,
                conditions_met=buy_conditions,
                conditions_detail=buy_details,
            ):
                return {
                    "type": "BUY",
                    "confidence": buy_conditions / 5.0,
                    "conditions_met": buy_conditions,
                    "conditions_detail": buy_details,
                }

            if self._is_signal_allowed(
                side="SELL",
                current_time=current_time,
                conditions_met=sell_conditions,
                conditions_detail=sell_details,
            ):
                return {
                    "type": "SELL",
                    "confidence": sell_conditions / 5.0,
                    "conditions_met": sell_conditions,
                    "conditions_detail": sell_details,
                }

            return None

        except Exception as e:
            print(f"Error in strategy analysis: {e}")
            return None
