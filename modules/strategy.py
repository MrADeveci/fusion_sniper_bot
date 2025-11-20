"""
C79 Sniper Bot Strategy v4.0
Independent BUY/SELL condition evaluation
"""

import pandas as pd
from ta.trend import EMAIndicator, ADXIndicator
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import BollingerBands


class C79Strategy:
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

        # EMA parameters
        self.ema_fast = self.strategy_config.get("ema_fast_period", 21)
        self.ema_slow = self.strategy_config.get("ema_slow_period", 50)
        self.ema_trend = self.strategy_config.get("ema_trend_period", 200)

        # RSI parameters
        self.rsi_period = self.strategy_config.get("rsi_period", 14)
        self.rsi_oversold = self.strategy_config.get("rsi_oversold", 30)
        self.rsi_overbought = self.strategy_config.get("rsi_overbought", 70)

        # ADX parameters
        self.adx_period = self.strategy_config.get("adx_period", 14)
        self.adx_threshold = self.strategy_config.get("adx_threshold", 25)

        # Stochastic parameters
        self.stoch_k = self.strategy_config.get("stochastic_k", 14)
        self.stoch_d = self.strategy_config.get("stochastic_d", 3)
        self.stoch_oversold = self.strategy_config.get("stochastic_oversold", 20)
        self.stoch_overbought = self.strategy_config.get("stochastic_overbought", 80)

        # Bollinger Bands
        self.bb_period = self.strategy_config.get("bollinger_period", 20)
        self.bb_std = self.strategy_config.get("bollinger_std", 2)

    # ------------------------------------------------------------------
    # Session / weekday helpers for Thursday Asia logic
    # ------------------------------------------------------------------
    def _is_thursday_asia(self, current_time):
        """
        Return True if current_time falls in the Thursday Asia session.

        Assumes current_time is already in the timezone you use for trading
        logic (for example broker or server time).
        The hour range here is a simple Asia session approximation and can
        be adjusted if needed.
        """
        try:
            if current_time is None or pd.isna(current_time):
                return False

            ts = pd.to_datetime(current_time)
            weekday = ts.weekday()  # Monday = 0 ... Thursday = 3
            hour = ts.hour

            # Asia session approximation: 00:00 <= hour < 08:00 on Thursday
            if weekday == 3 and 0 <= hour < 8:
                return True
        except Exception:
            # If anything goes wrong, do not apply special logic
            return False

        return False

    def _is_signal_allowed(self, side, current_time, conditions_met, conditions_detail):
        """
        Apply Thursday Asia overrides.

        On Thursday Asia:
            SELL: min_conditions + 1 (4 of 5) AND must include a trend flag
            BUY : min_conditions     (3 of 5) AND must include a trend flag

        On all other days or sessions:
            Behaves like the original logic:
            conditions_met >= min_conditions with no mandatory trend flags.
        """
        # Default behaviour for all other times
        min_required = self.min_conditions
        require_trend_flag = False

        if self._is_thursday_asia(current_time):
            if side == "SELL":
                # Stricter SELL on Thursday Asia: 4 of 5 + trend alignment
                min_required = self.min_conditions + 1
                require_trend_flag = True
            elif side == "BUY":
                # BUY keeps normal count but must be trend aligned
                require_trend_flag = True

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

            # Last bar time for Thursday Asia logic, if we have it
            current_time = None
            if "time" in df.columns:
                current_time = df["time"].iloc[-1]

            # BUY conditions (independent evaluation)
            buy_conditions = 0
            buy_details = []

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

            # SELL conditions (independent evaluation)
            sell_conditions = 0
            sell_details = []

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

            # Optional debug output for signal evaluation
            if self.debug_signals:
                try:
                    print(
                        f"[DEBUG] BUY {buy_conditions}/5 {buy_details} | "
                        f"SELL {sell_conditions}/5 {sell_details} | "
                        f"price={current_price:.2f} RSI={current_rsi:.1f} ADX={current_adx:.1f}"
                    )
                except Exception:
                    # Never let debug printing break the strategy
                    pass

            # Generate signal if conditions met
            # Thursday Asia overrides are applied via _is_signal_allowed

            # Prefer BUY if both are allowed, same as your original priority
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
