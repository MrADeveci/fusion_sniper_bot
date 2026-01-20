"""
Fusion Sniper Bot Strategy v4.3
Independent BUY/SELL condition evaluation
"""

import pandas as pd
from ta.trend import EMAIndicator, ADXIndicator
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import BollingerBands


class FusionStrategy:
    """Trading strategy with independent BUY/SELL evaluation"""

    def __init__(self, config):
        self.config = config
        self.strategy_config = config["STRATEGY"]

        # Optional debug flag, default False if not in config
        self.debug_signals = self.strategy_config.get("debug_signals", False)

        # Load parameters
        self.min_conditions = self.strategy_config.get("min_conditions_required", 3)

        # Trend-related condition labels (must match conditions_detail)
        self.trend_flags = {"ABOVE_TREND", "BELOW_TREND", "STRONG_TREND"}

        # Trend filter configuration
        # This can be enabled or disabled per symbol via config.json under STRATEGY.trend_filter.
        # If no config is provided, a legacy time window is used to preserve existing behaviour.
        default_trend_filter = {
            "enabled": True,
            "scope": "window",  # "window" or "always"
            "window": {
                "weekday": 3,      # 0=Mon ... 6=Sun
                "start_hour": 0,
                "end_hour": 8,
            },
            "require_trend_flag": True,
            "buy_extra_conditions": 0,
            "sell_extra_conditions": 1,
        }

        user_trend_filter = self.strategy_config.get("trend_filter", None)
        if isinstance(user_trend_filter, dict):
            merged = dict(default_trend_filter)
            merged.update(user_trend_filter)
            # Merge nested window config safely
            window_cfg = dict(default_trend_filter.get("window", {}))
            window_cfg.update(user_trend_filter.get("window", {}) or {})
            merged["window"] = window_cfg
            self.trend_filter = merged
        elif user_trend_filter is None:
            self.trend_filter = default_trend_filter
        else:
            # Invalid type, fall back to defaults
            self.trend_filter = default_trend_filter

        # EMA parameters
        # Supports both canonical keys and legacy keys from older configs.
        def _pick(keys, default):
            for k in keys:
                if k in self.strategy_config:
                    return self.strategy_config.get(k)
            return default

        self.ema_fast = int(_pick(["ema_fast_period", "ema_20_period"], 21))
        self.ema_slow = int(_pick(["ema_slow_period", "ema_50_period"], 50))
        self.ema_trend = int(_pick(["ema_trend_period", "ema_200_period"], 200))

        # RSI parameters
        self.rsi_period = int(self.strategy_config.get("rsi_period", 14))
        self.rsi_oversold = float(self.strategy_config.get("rsi_oversold", 30))
        self.rsi_overbought = float(self.strategy_config.get("rsi_overbought", 70))

        # ADX parameters
        self.adx_period = int(self.strategy_config.get("adx_period", 14))
        self.adx_threshold = float(self.strategy_config.get("adx_threshold", 25))

        # Stochastic parameters
        self.stoch_k = int(self.strategy_config.get("stochastic_k", 14))
        self.stoch_d = int(self.strategy_config.get("stochastic_d", 3))
        self.stoch_oversold = float(self.strategy_config.get("stochastic_oversold", 20))
        self.stoch_overbought = float(self.strategy_config.get("stochastic_overbought", 80))

        # Bollinger Bands
        # Supports both canonical keys and legacy keys from older configs.
        self.bb_period = int(_pick(["bollinger_period", "bb_period"], 20))
        self.bb_std = float(_pick(["bollinger_std", "bb_std_dev"], 2))


    # ------------------------------------------------------------------
    # Trend filter helpers
    # ------------------------------------------------------------------
    def _is_trend_filter_window_active(self, current_time):
        """
        Return True if current_time falls inside the configured trend-filter window.

        The window is configured via STRATEGY.trend_filter.window and is evaluated
        using the timestamp passed into this strategy (typically broker or server time).
        """
        try:
            if current_time is None or pd.isna(current_time):
                return False

            ts = pd.to_datetime(current_time)
            weekday = ts.weekday()  # Monday = 0 ... Sunday = 6
            hour = ts.hour

            window_cfg = (self.trend_filter or {}).get("window", {}) or {}
            target_weekday = int(window_cfg.get("weekday", 3))
            start_hour = int(window_cfg.get("start_hour", 0))
            end_hour = int(window_cfg.get("end_hour", 8))

            if weekday != target_weekday:
                return False

            # Active only inside the configured hour window.
            # Supports windows that span midnight, e.g. 22:00 -> 02:00
            if start_hour <= end_hour:
                return start_hour <= hour < end_hour
            return hour >= start_hour or hour < end_hour

        except Exception:
            # If anything goes wrong, do not apply special logic
            return False

    def _is_trend_filter_active(self, current_time):
        """
        Return True if the trend filter should be enforced for the given timestamp.

        Supported scopes:
            - "always": enforce on every bar
            - "window": enforce only inside the configured weekday and hour window
        """
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
        """
        Apply optional trend filter rules.

        When the trend filter is active (configured via STRATEGY.trend_filter):
            - Optionally requires at least one trend flag in conditions_detail.
            - Can increase the minimum number of conditions required per side.

        When inactive, behaves like the original logic:
            conditions_met >= min_conditions with no mandatory trend flags.
        """
        # Default behaviour
        min_required = self.min_conditions
        require_trend_flag = False

        cfg = getattr(self, "trend_filter", {}) or {}

        # Optional trend filter behaviour
        if self._is_trend_filter_active(current_time):
            require_trend_flag = bool(cfg.get("require_trend_flag", True))

            try:
                buy_extra = int(cfg.get("buy_extra_conditions", 0) or 0)
            except Exception:
                buy_extra = 0
            try:
                sell_extra = int(cfg.get("sell_extra_conditions", 0) or 0)
            except Exception:
                sell_extra = 0

            if side == "BUY":
                min_required = self.min_conditions + max(0, buy_extra)
            elif side == "SELL":
                min_required = self.min_conditions + max(0, sell_extra)

        # Check count
        if conditions_met < min_required:
            return False

        # Optionally enforce trend alignment
        if require_trend_flag:
            if not any(flag in self.trend_flags for flag in conditions_detail):
                return False

        return True

    def analyze_from_rates(self, rates):
        """Takes raw MT5 rates, builds DataFrame, then runs main analyze logic"""
        if rates is None or len(rates) < 200:
            return None

        df = pd.DataFrame(rates)
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], unit="s")

        return self.analyze(df)

    def analyze(self, data):
        """Analyze market data and return signal"""
        try:
            if data is None or len(data) < 200:
                return None

            df = data.copy()

            # Calculate indicators
            # EMAs
            ema_fast = EMAIndicator(close=df["close"], window=self.ema_fast).ema_indicator()
            ema_slow = EMAIndicator(close=df["close"], window=self.ema_slow).ema_indicator()
            ema_trend = EMAIndicator(close=df["close"], window=self.ema_trend).ema_indicator()

            # RSI
            rsi = RSIIndicator(close=df["close"], window=self.rsi_period).rsi()

            # ADX
            adx_indicator = ADXIndicator(
                high=df["high"],
                low=df["low"],
                close=df["close"],
                window=self.adx_period,
            )
            adx = adx_indicator.adx()

            # Stochastic
            stoch = StochasticOscillator(
                high=df["high"],
                low=df["low"],
                close=df["close"],
                window=self.stoch_k,
                smooth_window=self.stoch_d,
            )
            stoch_k = stoch.stoch()
            stoch_d = stoch.stoch_signal()

            # Bollinger Bands
            bb = BollingerBands(
                close=df["close"],
                window=self.bb_period,
                window_dev=self.bb_std,
            )
            bb_upper = bb.bollinger_hband()
            bb_lower = bb.bollinger_lband()
            bb_middle = bb.bollinger_mavg()

            # Current values
            current_price = df["close"].iloc[-1]
            current_ema_fast = ema_fast.iloc[-1]
            current_ema_slow = ema_slow.iloc[-1]
            current_ema_trend = ema_trend.iloc[-1]
            current_rsi = rsi.iloc[-1]
            current_adx = adx.iloc[-1]
            current_stoch_k = stoch_k.iloc[-1]
            current_stoch_d = stoch_d.iloc[-1]
            current_bb_upper = bb_upper.iloc[-1]
            current_bb_lower = bb_lower.iloc[-1]
            current_bb_middle = bb_middle.iloc[-1]

            # Last bar time for optional trend filter window logic, if we have it
            current_time = None
            if "time" in df.columns:
                current_time = df["time"].iloc[-1]


            # BUY and SELL conditions
            # This deliberately uses more 'trigger' style conditions so the bot can take more scalps,
            # while the multi-timeframe bias (passed in via 'bias') keeps it directionally disciplined.

            total_conditions = 7

            buy_conditions = 0
            buy_details = []

            sell_conditions = 0
            sell_details = []

            # Strict bias filter: only trade in the bias direction
            bias = (bias or 'NEUTRAL').upper().strip()
            allow_buy = bias == 'BULL'
            allow_sell = bias == 'BEAR'

            # Previous values for trigger logic
            prev_price = df['close'].iloc[-2]
            prev_ema_fast = ema_fast.iloc[-2]
            prev_rsi = rsi.iloc[-2]
            prev_stoch_k = stoch_k.iloc[-2]
            prev_stoch_d = stoch_d.iloc[-2]
            prev_bb_middle = bb_middle.iloc[-2]

            # Recent band touches (use previous 3 closed candles for stability)
            lookback = 3
            recent_lows = df['low'].iloc[-(lookback + 1):-1]
            recent_highs = df['high'].iloc[-(lookback + 1):-1]
            recent_bb_lower = bb_lower.iloc[-(lookback + 1):-1]
            recent_bb_upper = bb_upper.iloc[-(lookback + 1):-1]

            touched_lower_band = any(l <= bl for l, bl in zip(recent_lows, recent_bb_lower))
            touched_upper_band = any(h >= bu for h, bu in zip(recent_highs, recent_bb_upper))

            # 1) EMA structure (trend direction)
            if current_ema_fast > current_ema_slow:
                buy_conditions += 1
                buy_details.append('EMA_TREND')
            if current_ema_fast < current_ema_slow:
                sell_conditions += 1
                sell_details.append('EMA_TREND')

            # 2) Trend anchor (200 EMA)
            if current_price > current_ema_trend:
                buy_conditions += 1
                buy_details.append('ABOVE_TREND')
            if current_price < current_ema_trend:
                sell_conditions += 1
                sell_details.append('BELOW_TREND')

            # 3) Momentum / regime proxy
            if current_adx > self.adx_threshold:
                buy_conditions += 1
                buy_details.append('STRONG_TREND')
                sell_conditions += 1
                sell_details.append('STRONG_TREND')

            # 4) RSI pullback recovery / rollover (more frequent than strict oversold/overbought)
            if prev_rsi < 50 and current_rsi > prev_rsi and current_rsi >= 45:
                buy_conditions += 1
                buy_details.append('RSI_RECOVERY')
            if prev_rsi > 50 and current_rsi < prev_rsi and current_rsi <= 55:
                sell_conditions += 1
                sell_details.append('RSI_ROLLOVER')

            # 5) Stoch cross trigger
            if prev_stoch_k <= prev_stoch_d and current_stoch_k > current_stoch_d and current_stoch_k < 60:
                buy_conditions += 1
                buy_details.append('STOCH_CROSS_UP')
            if prev_stoch_k >= prev_stoch_d and current_stoch_k < current_stoch_d and current_stoch_k > 40:
                sell_conditions += 1
                sell_details.append('STOCH_CROSS_DN')

            # 6) Bollinger middle band reclaim
            if prev_price <= prev_bb_middle and current_price > current_bb_middle:
                buy_conditions += 1
                buy_details.append('BB_RECLAIM')
            if prev_price >= prev_bb_middle and current_price < current_bb_middle:
                sell_conditions += 1
                sell_details.append('BB_RECLAIM')

            # 7) Band touch in last few candles (pullback context)
            if touched_lower_band:
                buy_conditions += 1
                buy_details.append('BB_LOWER_TOUCH')
            if touched_upper_band:
                sell_conditions += 1
                sell_details.append('BB_UPPER_TOUCH')

            # Apply strict bias gating after building conditions so debug output is useful
            if not allow_buy:
                buy_conditions = 0
                buy_details = []
            if not allow_sell:
                sell_conditions = 0
                sell_details = []

            # Optional debug output for signal evaluation
            if self.debug_signals:
                try:
                    print(
                        f"[DEBUG] BUY {buy_conditions}/{total_conditions} {buy_details} | "
                        f"SELL {sell_conditions}/{total_conditions} {sell_details} | "
                        f"price={current_price:.2f} RSI={current_rsi:.1f} ADX={current_adx:.1f}"
                    )
                except Exception:
                    # Never let debug printing break the strategy
                    pass

            # Generate signal if conditions met
            # Optional trend filter rules are applied via _is_signal_allowed

            # Prefer BUY if both are allowed, same as your original priority
            if self._is_signal_allowed(
                side="BUY",
                current_time=current_time,
                conditions_met=buy_conditions,
                conditions_detail=buy_details,
            ):
                return {
                    "type": "BUY",
                    "confidence": buy_conditions / float(total_conditions),
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
                    "confidence": sell_conditions / float(total_conditions),
                    "conditions_met": sell_conditions,
                    "conditions_detail": sell_details,
                }

            return None

        except Exception as e:
            print(f"Error in strategy analysis: {e}")
            return None