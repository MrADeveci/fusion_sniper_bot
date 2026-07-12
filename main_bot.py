"""
Fusion Sniper Bot - v5.0.0
"""

import datetime as dt
import glob
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import MetaTrader5 as mt5
import numpy as np
import pandas as pd


# Import local modules
from modules.strategy import FusionStrategy
from modules.momentum_strategy import MomentumBreakoutStrategy
from modules.risk_manager import RiskManager
from modules.news_filter import EconomicNewsFilter
from modules.telegram_notifier import TelegramNotifier
from modules.trade_statistics import TradeStatistics
from modules.atomic_json import write_json_atomic, read_json_quarantine
from modules.broker_costs import swap_cost, commission_cost

PAPER_TICKET_BASE = 90000000     # simulated tickets start above any real broker ticket


class FusionSniperBot:
    """Main trading bot class with ATR-based position management"""
    
    def __init__(self, config_file, paper_mode=False):
        """Initialize bot with configuration"""
        self.config_file = config_file
        self.config = self.load_config(config_file)

        # Validate config before proceeding
        self.validate_config()

        # v5.0.0: PAPER (dry-run) mode. On when SYSTEM.paper_mode is true OR --paper given.
        self._resolve_paper_mode(paper_mode)
        self._paper_ticket_seq = PAPER_TICKET_BASE   # re-seeded from state in _restore_paper_state
        
        # Setup logging first
        self.setup_logging()
        
        # Clean old log files (7-day retention)
        self.cleanup_old_logs()
        
        # Initialize MT5
        if not self.initialize_mt5():
            raise Exception("Failed to initialize MT5")
        
        # Initialize modules
        self.strategy = FusionStrategy(self.config)
        self.risk_manager = RiskManager(self.config)
        self.news_filter = EconomicNewsFilter(self.config)
        # Simulated trades go to trade_statistics_{symbol}_paper.json, never the live file.
        self.stats_tracker = TradeStatistics(self.config, paper=self.paper_mode)
        
        # Initialize Telegram
        telegram_config = self.config.get('TELEGRAM', {})
        self.telegram = TelegramNotifier(
            bot_token=telegram_config.get('bot_token', ''),
            chat_id=telegram_config.get('chat_id', ''),
            enabled=telegram_config.get('enabled', False)
        )
        
        # Bot state
        self.symbol = self.config['BROKER']['symbol']
        self.magic_number = self.config['BROKER']['magic_number']
        # Timeframes (multi-timeframe support)
        trading_cfg = self.config.get('TRADING', {})
        base_tf_str = trading_cfg.get('timeframe', 'M15')
        self.base_timeframe_str = base_tf_str
        self.entry_timeframe_str = trading_cfg.get('entry_timeframe', base_tf_str)
        self.bias_timeframe_str = trading_cfg.get('bias_timeframe', base_tf_str)
        self.atr_timeframe_str = trading_cfg.get('atr_timeframe', self.bias_timeframe_str)

        # MT5 timeframe constants
        self.entry_timeframe = getattr(mt5, f"TIMEFRAME_{self.entry_timeframe_str}")
        self.bias_timeframe = getattr(mt5, f"TIMEFRAME_{self.bias_timeframe_str}")
        self.atr_timeframe = getattr(mt5, f"TIMEFRAME_{self.atr_timeframe_str}")

        # Backwards compatibility. legacy code uses self.timeframe for data fetching.
        # In multi-timeframe mode, self.timeframe is the entry timeframe.
        self.timeframe = self.entry_timeframe

        # Candle gating and bias cache
        self.last_entry_bar_time = None  # epoch seconds of last processed entry candle
        self.last_bias_bar_time = None   # epoch seconds of last processed bias candle
        self.last_market_bias = "NEUTRAL"
        self.last_bias_detail = {}

        # Bias behaviour
        strategy_cfg = self.config.get('STRATEGY', {}) or {}
        smc_cfg = strategy_cfg.get('SMC', {}) if isinstance(strategy_cfg, dict) else {}
        # Default to strict bias when using multi-timeframe mode, unless explicitly disabled
        self.strict_bias = bool(
            smc_cfg.get('strict_bias', self.entry_timeframe_str != self.base_timeframe_str)
        )

        # v5.0.0: strategy engine selector ("smc" | "momentum"). SMC path is unchanged.
        self.engine = str(strategy_cfg.get('engine', 'smc')).lower()
        self.momentum = None
        if self.engine == 'momentum':
            self.momentum = MomentumBreakoutStrategy(strategy_cfg.get('momentum', {}) or {})
            # momentum runs M15 breakout entries with an H4 trend filter and M15 ATR
            self.entry_timeframe_str, self.bias_timeframe_str, self.atr_timeframe_str = "M15", "H4", "M15"
            self.entry_timeframe = getattr(mt5, "TIMEFRAME_M15")
            self.bias_timeframe = getattr(mt5, "TIMEFRAME_H4")
            self.atr_timeframe = getattr(mt5, "TIMEFRAME_M15")
            self.timeframe = self.entry_timeframe
            self.strict_bias = False  # momentum uses its own H4 trend filter
            self.logger.info("Strategy engine: MOMENTUM breakout (entry M15 / bias H4 / ATR M15)")
        else:
            self.logger.info("Strategy engine: SMC")

        self.running = True
        
        # Trading parameters
        self.lot_size = self.config['TRADING']['lot_size']
        self.max_positions = self.config['TRADING'].get('max_positions', 1)
        self.use_atr_stops = self.config['TRADING'].get('use_atr_based_stops', True)
        
        # v5.0.0: ATR-based break-even and trailing stop
        self.use_trailing_stop = self.config['TRADING'].get('use_trailing_stop', True)
        self.trailing_stop_type = self.config['TRADING'].get('trailing_stop_type', 'chandelier')
        self.trailing_atr_multiple = self.config['TRADING'].get('trailing_stop_atr_multiple', 2.0)
        self.trail_activation_multiple = self.config['TRADING'].get('min_profit_for_trail_activation', 1.5)
        
        self.use_breakeven = self.config['TRADING'].get('use_smart_breakeven', True)
        self.breakeven_trigger_multiple = self.config['TRADING'].get('breakeven_profit_multiple', 1.2)
        self.breakeven_lock_multiple = self.config['TRADING'].get('breakeven_lock_profit_multiple', 0.3)

        # News notification tracking
        news_config = self.config.get('NEWS_FILTER', {})
        self.last_weekly_summary_date = None
        self.weekly_summary_enabled = news_config.get('weekly_summary_enabled', True)
        self.weekly_summary_day = news_config.get('weekly_summary_day', 6)  # 6 = Sunday
        self.weekly_summary_hour = news_config.get('weekly_summary_hour_gmt', 22)  # 10pm
        self.alerted_news_events = set()  # Track which events we've alerted on
        
        # Position tracking for closure detection
        self.tracked_positions = {}
        
        # Cooldown tracking
        # v5.0.0 (L1): removed unused self.trade_cooldown (is_in_cooldown uses the
        # scalp_cooldown/normal_cooldown values from volatility_detection instead).
        self.last_trade_time = None
        self.last_trade_type = None
        
        # Trading hours from config
        trading_hours = self.config['TRADING'].get('trading_hours', {})
        self.saturday_closed = trading_hours.get('saturday_closed', True)
        self.sunday_closed = trading_hours.get('sunday_closed', False)
        self.monday_open_hour = trading_hours.get('monday_open_hour', 0)
        self.sunday_open_hour = trading_hours.get('sunday_open_hour', 22)
        self.friday_close_hour = trading_hours.get('friday_close_hour', 22)
        self.weekday_open_hour = trading_hours.get('weekday_open_hour', None)
        self.weekday_close_hour = trading_hours.get('weekday_close_hour', None)

        # Daily profit tracking (PAUSE mode instead of shutdown)
        self.daily_profit_target = self.config['TRADING'].get('daily_profit_target', 0)
        self.daily_target_reached = False
        self.last_target_check_date = datetime.now().date()
        self.starting_equity_today = None  # Track starting equity for loss limit

        # Daily loss or profit pending state so we can recheck after open trades close
        self.loss_limit_pending = False
        self.profit_target_pending = False

        # Weekly profit or loss limits
        risk_cfg = self.config.get('RISK', {})
        self.weekly_limits_enabled = risk_cfg.get('weekly_limits_enabled', False)
        self.max_weekly_profit = risk_cfg.get('max_weekly_profit', 0.0)
        self.max_weekly_loss = risk_cfg.get('max_weekly_loss', 0.0)
        self.week_start_day = risk_cfg.get('week_start_day', 'monday').lower()
        self.weekly_limit_triggered = False    # True once weekly cap is hit
        self.weekly_limit_side = None          # 'profit' or 'loss'
        self.last_week_start_date = None       # broker week start date used for reset

        # Loop timing from config
        system_cfg = self.config.get('SYSTEM', {})
        # main_loop_interval controls how often the bot runs when flat (scanning for entries)
        # active_loop_interval controls how often the bot runs when there is at least one open position
        self.main_loop_interval = int(system_cfg.get('main_loop_interval', 10))
        self.active_loop_interval = int(system_cfg.get('active_loop_interval', self.main_loop_interval))
        self.paused_loop_interval = int(system_cfg.get('paused_loop_interval', 30))
        # v5.0.0 (M8): throttle the repeated "scanning"/mode log lines
        self.waiting_log_interval = int(system_cfg.get('waiting_log_interval', 300))
        self._last_scan_log_ts = 0.0
        self._last_pos_count = -1
        self._last_logged_mode = None

        
        # VOLATILITY DETECTION
        self.volatility_config = self.config['TRADING'].get('volatility_detection', {})
        self.volatility_enabled = self.volatility_config.get('enabled', False)
        self.atr_period = self.volatility_config.get('atr_period', 14)
        self.atr_scalp_threshold = self.volatility_config.get('atr_scalp_threshold', 2.0)
        self.scalp_profit_target = self.volatility_config.get('scalp_profit_target_gbp', 26.82)
        self.scalp_cooldown = self.volatility_config.get('scalp_cooldown_seconds', 30)
        self.normal_cooldown = self.volatility_config.get('normal_cooldown_seconds', 60)
        
        # Track current ATR and mode
        self.current_atr = None
        self.current_mode = 'normal'
        self.last_atr_check = None

        # Extreme volatility handling
        self.skip_on_extreme_atr = self.volatility_config.get('skip_trading_when_atr_extreme', False)
        self.atr_max_for_trading = self.volatility_config.get('atr_max_for_trading', None)

        # Swap / rollover avoidance (server time windows)
        swap_cfg = self.config['TRADING'].get('swap_avoidance', {})
        self.swap_avoidance_enabled = swap_cfg.get('enabled', False)
        # List of {"start": "HH:MM", "end": "HH:MM"} in server time
        self.swap_avoidance_windows = swap_cfg.get('server_time_windows', [])
        
        # Get pip size (used for stats and SL/TP hit detection, not order sizing)
        symbol_info = mt5.symbol_info(self.symbol)
        if symbol_info:
            if self.symbol == "XAUUSD":
                # XAU is typically quoted to 2 or 3 decimals; treat 0.1 as a "pip"
                self.pip_size = 0.1
            elif "BTC" in self.symbol:
                # For BTC CFDs treat 1.0 as a pip (adjust later if you prefer)
                self.pip_size = 1.0
            else:
                # Generic case based on broker point value
                self.pip_size = symbol_info.point * 10
        else:
            # Fallback. safe default if symbol_info is not available
            self.pip_size = 0.0001

        # v5.0.0 (H2): cache symbol trading params for order normalisation/rounding.
        if symbol_info:
            self.symbol_digits = int(symbol_info.digits)
            self.symbol_point = float(symbol_info.point)
            self.volume_step = float(symbol_info.volume_step) or 0.01
            self.volume_min = float(symbol_info.volume_min) or 0.01
            self.volume_max = float(symbol_info.volume_max) or 100.0
            self.trade_stops_level = int(getattr(symbol_info, "trade_stops_level", 0) or 0)
            self.filling_mode_mask = int(getattr(symbol_info, "filling_mode", 0) or 0)
        else:
            self.symbol_digits = 2
            self.symbol_point = 0.01
            self.volume_step = 0.01
            self.volume_min = 0.01
            self.volume_max = 100.0
            self.trade_stops_level = 0
            self.filling_mode_mask = 0

        # v5.0.0 (H2): order execution params from config instead of hardcoding.
        order_exec = self.config['TRADING'].get('order_execution', {})
        self.order_comment = order_exec.get('comment', 'fusion_sniper_v5')
        self.order_deviation = int(order_exec.get('deviation', 10))
        self.order_send_retries = int(order_exec.get('order_send_retries', 2))
        # v5.0.0 (O1): minimum stop distance floor so a tiny ATR can't create an
        # ultra-tight stop. Configurable in price; default 0.50 (= 50 points on gold).
        self.min_stop_distance_price = float(order_exec.get('min_stop_distance_usd', 0.50))

        # v5.0.0 (H3): persistent state file (entry_atr / breakeven_applied / cooldown).
        self.state_file = Path('logs') / 'bot_state.json'
        self._restored_state = self._load_state()
        # Restore cooldown clock so a restart doesn't immediately re-enter.
        lt = self._restored_state.get('last_trade_time')
        if lt:
            try:
                self.last_trade_time = datetime.fromisoformat(lt)
                self.last_trade_type = self._restored_state.get('last_trade_type', 'normal')
            except Exception:
                pass

        # v5.0.0: simulated positions + realised-P&L ledger for paper mode.
        # Restored from bot_state.json so a restart does not lose the paper book or
        # recycle ticket numbers. [{'time': datetime, 'net': float, 'ticket': int}]
        self._restore_paper_state()

        # Paper cost model: the SAME swap/commission arithmetic as the backtester, so a
        # paper "net" is comparable with a backtest "net" rather than a gross price move.
        self.commission_per_lot = float(order_exec.get('commission_per_lot_gbp', 5.52))
        self._paper_swap_warned = False

        # Logging
        self.logger.info("="*60)
        self.logger.info("Fusion Sniper Bot v5.0.0")
        self.logger.info(f"Symbol: {self.symbol}")
        self.logger.info("="*60)
        self._log_mode_banner()
        self.logger.info(f"Magic number: {self.magic_number}")

        if self.engine == 'momentum':
            # Momentum engine: report the REAL validated params (shared module), not the
            # legacy SMC ATR/scalp/breakeven settings which this engine does not use.
            m = self.momentum
            self.logger.info("Strategy: MOMENTUM breakout (H4-trend-filtered M15 breakout)")
            self.logger.info(f"  H4 Trend Filter: EMA{m.h4_ema} (close > EMA => longs, < EMA => shorts)")
            self.logger.info(f"  Entry: M15 close breaks prior-{m.lookback}-bar high/low in trend direction")
            self.logger.info(f"  Stop Loss: {m.sl_mult}x ATR(M15)")
            self.logger.info("  Take Profit: NONE (ratcheting trailing-stop exit only)")
            self.logger.info(f"  Trailing Stop: {m.trail_mult}x ATR, activates at {m.trail_act}x ATR profit")
            self.logger.info("  Scalp / Break-Even: DISABLED")
            self.logger.info(f"  Session: {m.sess_start:02d}:00-{m.sess_end:02d}:00 UK | one position at a time")
            if m.sizing_mode == 'percent_equity':
                self.logger.info(f"  Sizing: {m.sizing_mode} | {m.risk_percent}% equity risk per trade")
            else:
                self.logger.info(f"  Sizing: {m.sizing_mode} | £{m.risk_flat_gbp:.0f} risk per trade")
            if self.daily_profit_target > 0:
                self.logger.info(f"Daily Profit Target: £{self.daily_profit_target} (PAUSE mode)")
        else:
            self.logger.info(f"Lot size: {self.lot_size}")
            self.logger.info(f"Max Concurrent Positions: {self.max_positions}")
            self.logger.info("Stop/TP Mode: ATR-Based (Dynamic)")
            self.logger.info(f"  SL Multiplier: {self.config['TRADING'].get('stop_loss_atr_multiple', 1.0)}x ATR")
            self.logger.info(f"  TP Multiplier: {self.config['TRADING'].get('take_profit_atr_multiple', 2.0)}x ATR")

            # Log break-even settings
            if self.use_breakeven:
                self.logger.info("Break-Even: ENABLED (ATR-based)")
                self.logger.info(f"  Trigger: {self.breakeven_trigger_multiple}x ATR profit")
                self.logger.info(f"  Lock: {self.breakeven_lock_multiple}x ATR profit")

            # Log trailing stop settings
            if self.use_trailing_stop:
                self.logger.info(f"Trailing Stop: {self.trailing_stop_type.upper()} (ATR-based)")
                self.logger.info(f"  Distance: {self.trailing_atr_multiple}x ATR")
                self.logger.info(f"  Activation: {self.trail_activation_multiple}x ATR profit")

            if self.daily_profit_target > 0:
                self.logger.info(f"Daily Profit Target: £{self.daily_profit_target} (PAUSE mode)")

            if self.volatility_enabled:
                self.logger.info("="*50)
                self.logger.info("AUTO-VOLATILITY DETECTION ENABLED")
                self.logger.info(f"Scalp threshold: ATR > {self.atr_scalp_threshold}")
                self.logger.info(f"Scalp target: £{self.scalp_profit_target}")
                self.logger.info("="*50)

        self.logger.info("News Filter: ENABLED (ForexFactory XML format)")
        self.logger.info("News Fetch: Continuous (even when paused)")
        
        self.telegram.notify_bot_started(self.symbol)
        if self.paper_mode:
            try:
                self.telegram.notify_paper_mode(self.symbol)
            except Exception:
                pass

        # Write status file for remote control
        self.write_status_file()
    
    def _resolve_paper_mode(self, cli_paper):
        """Decide PAPER vs LIVE and record which source asked for it.

        LIVE is only ever reachable through an explicit SYSTEM.paper_mode: false. An
        absent key, or one that is not a JSON boolean, is a hard startup error -- the
        bot refuses to run rather than infer a trading mode from an ambiguous config.

        PAPER then wins if EITHER source asks for it, so a relaunch that drops --paper
        (watchdog restart, /start) cannot silently go live while the config says true.

        Runs before setup_logging(), so refusal is reported on stderr, not the logger.
        """
        sysd = self.config.get('SYSTEM', {})
        if 'paper_mode' not in sysd or not isinstance(sysd['paper_mode'], bool):
            found = ("the SYSTEM.paper_mode key is missing" if 'paper_mode' not in sysd
                     else f"SYSTEM.paper_mode is {sysd['paper_mode']!r}, not a JSON boolean")
            sys.stderr.write(
                "\n" + "!" * 72 + "\n"
                "REFUSING TO START: trading mode is ambiguous.\n\n"
                f"  config: {self.config_file}\n"
                f"  problem: {found}\n\n"
                "The bot will not guess whether to trade for real. Set the mode explicitly:\n\n"
                '  "SYSTEM": { "paper_mode": true }    <- simulated orders (safe)\n'
                '  "SYSTEM": { "paper_mode": false }   <- LIVE, real orders will be sent\n\n'
                "LIVE requires an explicit false. Nothing was traded.\n"
                + "!" * 72 + "\n"
            )
            sys.exit(2)

        self.paper_cfg = sysd['paper_mode']        # guaranteed a real bool from here on
        self.paper_cli = bool(cli_paper)
        self.paper_mode = self.paper_cfg or self.paper_cli

        if self.paper_cfg and self.paper_cli:
            self.mode_source = "config SYSTEM.paper_mode=true AND --paper flag"
        elif self.paper_cfg:
            self.mode_source = "config SYSTEM.paper_mode=true"
        elif self.paper_cli:
            self.mode_source = "--paper flag ONLY (config SYSTEM.paper_mode=false)"
        else:
            self.mode_source = "config SYSTEM.paper_mode=false (explicit), no --paper flag"

    def _log_mode_banner(self):
        """State the effective mode and its origin, loudly and unambiguously."""
        bar = "#" * 64
        self.logger.warning(bar)
        if self.paper_mode:
            self.logger.warning("###  EFFECTIVE MODE: PAPER - NO REAL ORDERS WILL BE SENT")
            self.logger.warning("###  order_send / modify_position are SIMULATED")
        else:
            self.logger.warning("###  EFFECTIVE MODE: LIVE - REAL ORDERS WILL BE SENT")
        self.logger.warning(f"###  mode source: {self.mode_source}")
        self.logger.warning(bar)

        if self.paper_cli and not self.paper_cfg:
            self.logger.warning(
                "PAPER is active via the CLI flag ONLY. A restart that omits --paper (watchdog "
                "restart, Telegram /start) would run LIVE. Set SYSTEM.paper_mode=true in the "
                "config to make paper mode survive a restart."
            )

    def load_config(self, config_file):
        """Load configuration from JSON file"""
        try:
            with open(config_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading config: {e}")
            sys.exit(1)
    
    def validate_config(self):
        """Validate that all required config fields are present"""
        required_sections = ['BROKER', 'TRADING', 'RISK', 'STRATEGY', 'TELEGRAM', 'NEWS_FILTER']
        
        for section in required_sections:
            if section not in self.config:
                raise ValueError(f"Missing required config section: {section}")
        
        broker_fields = ['symbol', 'magic_number', 'account', 'password', 'server']
        for field in broker_fields:
            if field not in self.config['BROKER']:
                raise ValueError(f"Missing required field in BROKER: {field}")
        
        trading_fields = ['timeframe', 'lot_size']
        for field in trading_fields:
            if field not in self.config['TRADING']:
                raise ValueError(f"Missing required field in TRADING: {field}")
        
        has_atr_stops = self.config['TRADING'].get('use_atr_based_stops', False)
        has_legacy_stops = 'stop_loss_pips' in self.config['TRADING']
        
        if not has_atr_stops and not has_legacy_stops:
            raise ValueError("TRADING must have use_atr_based_stops or legacy stop fields")
    
    def setup_logging(self):
        """Setup logging system with daily symbol based files"""
        log_dir = Path('logs')
        log_dir.mkdir(exist_ok=True)

        symbol = self.config['BROKER']['symbol']

        # Use DDMMYYYY in the filename as requested
        today_date = datetime.now().date()
        date_str = today_date.strftime('%d%m%Y')
        log_file = log_dir / f"{symbol}_{date_str}.log"

        self.logger = logging.getLogger(f"FusionSniper_{symbol}")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers = []

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.INFO)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)

        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)

        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

        # Track current log date and the active file handler so we can rotate at midnight
        self.current_log_date = today_date
        self.log_file_handler = file_handler

    
    def write_status_file(self):
        """Write bot status file with PID for remote control"""
        try:
            status_file = Path('logs') / 'bot_status.json'
            status_data = {
                'pid': os.getpid(),
                'started_at': datetime.now().isoformat(),
                'symbol': self.symbol,
                'magic_number': self.magic_number
            }
            with open(status_file, 'w') as f:
                json.dump(status_data, f, indent=2)
            self.logger.info(f"Status file created: PID {os.getpid()}")
        except Exception as e:
            self.logger.error(f"Error writing status file: {e}")
    
    def remove_status_file(self):
        """Remove status file on shutdown"""
        try:
            status_file = Path('logs') / 'bot_status.json'
            if status_file.exists():
                status_file.unlink()
                self.logger.info("Status file removed")
        except Exception as e:
            self.logger.error(f"Error removing status file: {e}")
    
    def cleanup_old_logs(self):
        """Delete symbol log files older than 7 days (e.g. XAUUSD_DDMMYYYY.log)"""
        try:
            log_dir = Path('logs')
            if not log_dir.exists():
                return

            # Get symbol safely (works even when called early in __init__)
            symbol = getattr(self, "symbol", None)
            if not symbol:
                symbol = self.config.get("BROKER", {}).get("symbol", "")

            # Only touch files like XAUUSD_*.log (or whatever the symbol is)
            pattern = f"{symbol}_*.log" if symbol else "*.log"
            log_files = glob.glob(str(log_dir / pattern))

            cutoff_date = datetime.now() - timedelta(days=7)
            deleted_count = 0

            for log_file in log_files:
                file_path = Path(log_file)
                if not file_path.is_file():
                    continue

                file_mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                if file_mtime < cutoff_date:
                    file_path.unlink()
                    deleted_count += 1

            if deleted_count > 0:
                # Log to the bot logger so it appears in the normal logs
                self.logger.info(
                    f"Cleaned up {deleted_count} old log file(s) (>7 days) matching {pattern}"
                )
        except Exception as e:
            # Use logger if available, otherwise fall back to print
            try:
                self.logger.error(f"Error cleaning old logs: {e}")
            except Exception:
                print(f"Error cleaning old logs: {e}")

    def rotate_log_file_if_needed(self):
        """
        Rotate the symbol log file when the calendar day changes.
        This keeps filenames in the form SYMBOL_DDMMYYYY.log and
        calls cleanup_old_logs to enforce seven day retention.
        """
        try:
            today = datetime.now().date()
            current_date = getattr(self, "current_log_date", None)

            # Nothing to do if still the same day
            if current_date == today:
                return

            log_dir = Path('logs')
            log_dir.mkdir(exist_ok=True)

            # Close and detach the previous file handler if present
            old_handler = getattr(self, "log_file_handler", None)
            formatter = getattr(old_handler, "formatter", None)

            if old_handler is not None:
                try:
                    self.logger.removeHandler(old_handler)
                    old_handler.close()
                except Exception:
                    pass

            # Build new filename with DDMMYYYY
            date_str = today.strftime('%d%m%Y')
            new_log_file = log_dir / f"{self.symbol}_{date_str}.log"

            new_handler = logging.FileHandler(new_log_file, encoding="utf-8")
            new_handler.setLevel(logging.INFO)

            if formatter is None:
                formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

            new_handler.setFormatter(formatter)

            # Attach new handler and update tracking fields
            self.logger.addHandler(new_handler)
            self.log_file_handler = new_handler
            self.current_log_date = today

            self.logger.info("=" * 60)
            self.logger.info(f"New trading day detected. switched to log file {new_log_file.name}")
            self.logger.info("=" * 60)

            # Enforce seven day retention after rolling
            self.cleanup_old_logs()

        except Exception as e:
            # Fall back to console output if anything goes wrong here
            print(f"Error rotating log file: {e}")

    def initialize_mt5(self):
        """Initialize MT5 connection"""
        broker = self.config['BROKER']
        mt5_path = broker.get('mt5_path')

        try:
            if mt5_path:
                # Use specific terminal path if provided in config
                if not mt5.initialize(path=mt5_path):
                    print(f"MT5 initialization failed for path: {mt5_path}")
                    return False
            else:
                # Fallback to default behaviour if no path is configured
                if not mt5.initialize():
                    print("MT5 initialization failed")
                    return False
        except Exception as e:
            print(f"MT5 initialization exception: {e}")
            return False

        account = broker.get('account')
        password = broker.get('password')
        server = broker.get('server')

        if account and password and server:
            authorized = mt5.login(account, password=password, server=server)
            if not authorized:
                print(f"Login failed: {mt5.last_error()}")
                mt5.shutdown()
                return False

        symbol = broker['symbol']
        if not mt5.symbol_select(symbol, True):
            print(f"Failed to select {symbol}")
            mt5.shutdown()
            return False

        return True

    # ------------------------------------------------------------------
    # v5.0.0 helpers: connection, state persistence, order normalisation
    # ------------------------------------------------------------------
    def ensure_connection(self):
        """C1: verify the MT5 terminal is connected and the account is reachable.
        On failure, attempt re-initialise/login with capped exponential backoff and
        alert via Telegram. Returns True only when we have a live, logged-in session.
        In paper mode we still want a live data feed, so we reconnect the same way."""
        try:
            ti = mt5.terminal_info()
            ai = mt5.account_info()
            if ti is not None and getattr(ti, "connected", False) and ai is not None:
                if getattr(self, "_was_disconnected", False):
                    self.logger.info("MT5 connection restored")
                    self._was_disconnected = False
                return True
        except Exception as e:
            self.logger.error(f"Connection check error: {e}")

        # Disconnected -> alert once, then try to recover with backoff
        if not getattr(self, "_was_disconnected", False):
            self.logger.error("MT5 connection lost - attempting to reconnect")
            try:
                self.telegram.notify_connection_lost(self.symbol)
            except Exception:
                pass
            self._was_disconnected = True

        for attempt in range(1, 6):
            delay = min(60, 2 ** attempt)
            time.sleep(delay)
            try:
                mt5.shutdown()
            except Exception:
                pass
            if self.initialize_mt5():
                ti = mt5.terminal_info()
                ai = mt5.account_info()
                if ti is not None and getattr(ti, "connected", False) and ai is not None:
                    self.logger.info(f"Reconnected to MT5 after {attempt} attempt(s)")
                    self._was_disconnected = False
                    return True
            self.logger.warning(f"Reconnect attempt {attempt} failed; retrying...")
        return False

    def _manual_stop_requested(self):
        """M11: True if the Telegram handler has requested a graceful stop via flag file."""
        try:
            paths = self.config.get('TELEGRAM_HANDLER', {}).get('paths', {})
            flag = Path(paths.get('manual_stop_flag', 'logs/manual_stop.flag'))
            return flag.exists()
        except Exception:
            return False

    def _load_state(self):
        """Load persisted state. A corrupt file is moved aside, never silently discarded."""
        logger = getattr(self, "logger", None)
        data, quarantined = read_json_quarantine(self.state_file, logger)
        if quarantined and logger:
            logger.error("Bot state was CORRUPT and has been quarantined. Restored nothing: "
                         "open positions will be re-adopted from the broker, but paper "
                         "positions and the paper ledger for this run are LOST.")
        return data or {}

    def _restore_paper_state(self):
        """Restore the paper book: simulated positions, the realised ledger, and the ticket
        sequence. Without this, a restart resurrected an empty book while bot_state.json
        still referenced the old tickets, and the sequence restarted at 90000000 -- so
        fresh trades RECYCLED ticket numbers already used by closed ones."""
        st = self._restored_state

        self.paper_positions = {}
        for tkt, p in (st.get('paper_positions') or {}).items():
            try:
                self.paper_positions[int(tkt)] = dict(p)
            except (TypeError, ValueError):
                continue

        self.paper_closed_trades = []
        for t in (st.get('paper_closed_trades') or []):
            try:
                self.paper_closed_trades.append({
                    'time': datetime.fromisoformat(t['time']),
                    'net': float(t['net']),
                    'ticket': t.get('ticket'),
                })
            except Exception:
                continue

        # Seed the sequence ABOVE every ticket we have ever issued -- the saved counter,
        # any still-open paper position, any closed one in the ledger, and any ticket left
        # in the tracked-positions block of a stale state file.
        seen = [int(st.get('paper_ticket_seq') or 0), PAPER_TICKET_BASE]
        seen += [int(t) for t in self.paper_positions]
        seen += [int(t['ticket']) for t in self.paper_closed_trades
                 if isinstance(t.get('ticket'), int)]
        for tkt in (st.get('positions') or {}):
            try:
                if int(tkt) >= PAPER_TICKET_BASE:
                    seen.append(int(tkt))
            except (TypeError, ValueError):
                continue
        self._paper_ticket_seq = max(seen)

        if self.paper_mode and (self.paper_positions or self.paper_closed_trades):
            self.logger.warning(
                f"[PAPER] restored {len(self.paper_positions)} open simulated position(s), "
                f"{len(self.paper_closed_trades)} closed trade(s); next ticket "
                f"{self._paper_ticket_seq + 1}")

    def _save_state(self):
        """Persist the cooldown clock, per-position data, and the FULL paper book.

        Written atomically (temp file + os.replace): a crash mid-write can no longer leave
        a truncated bot_state.json that the loader would silently treat as 'no state'.
        """
        try:
            positions = {}
            for ticket, pdata in getattr(self, "tracked_positions", {}).items():
                positions[str(ticket)] = {
                    'entry_atr': pdata.get('entry_atr', 0),
                    'breakeven_applied': bool(pdata.get('breakeven_applied', False)),
                }
            data = {
                'last_trade_time': self.last_trade_time.isoformat() if self.last_trade_time else None,
                'last_trade_type': self.last_trade_type,
                'positions': positions,
                # --- paper book (meaningless in live mode, but harmless and cheap) ---
                'paper_ticket_seq': int(getattr(self, '_paper_ticket_seq', PAPER_TICKET_BASE)),
                'paper_positions': {str(k): v for k, v in
                                    getattr(self, 'paper_positions', {}).items()},
                'paper_closed_trades': [
                    {'time': t['time'].isoformat() if hasattr(t['time'], 'isoformat') else str(t['time']),
                     'net': float(t['net']), 'ticket': t.get('ticket')}
                    for t in getattr(self, 'paper_closed_trades', [])
                ],
            }
            write_json_atomic(self.state_file, data)
        except Exception as e:
            self.logger.error(f"Error saving state file: {e}")

    def _select_filling_mode(self):
        """H2: choose a fill mode the symbol actually supports (mask is a bitfield)."""
        mask = getattr(self, "filling_mode_mask", 0)
        # SYMBOL_FILLING_FOK=1, SYMBOL_FILLING_IOC=2
        if mask & 2:
            return mt5.ORDER_FILLING_IOC
        if mask & 1:
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    def _normalize_volume(self, volume):
        """H2: snap volume to volume_step and clamp to [volume_min, volume_max]."""
        step = self.volume_step or 0.01
        vol = round(round(volume / step) * step, 8)
        vol = max(self.volume_min, min(self.volume_max, vol))
        # round to the step's decimal places to avoid float noise
        decimals = max(0, len(str(step).split('.')[-1])) if '.' in str(step) else 0
        return round(vol, decimals)

    def _round_price(self, price):
        """H2: round a price to the symbol's digits."""
        return round(float(price), self.symbol_digits)

    def _apply_stop_floor(self, price, sl, tp, direction):
        """O1 + H2: enforce a minimum stop distance (max of broker stops-level and the
        configured floor), then round to digits. Returns (sl, tp)."""
        broker_min = self.trade_stops_level * self.symbol_point
        min_dist = max(broker_min, self.min_stop_distance_price)
        if direction == 'BUY':
            if (price - sl) < min_dist:
                sl = price - min_dist
            if (tp - price) < min_dist:
                tp = price + min_dist
        else:  # SELL
            if (sl - price) < min_dist:
                sl = price + min_dist
            if (price - tp) < min_dist:
                tp = price - min_dist
        return self._round_price(sl), self._round_price(tp)

    def _order_send_retry(self, request, refresh_price=False):
        """H2: send an order with a bounded retry on requote/price-changed retcodes.
        When refresh_price is True (market entries), re-read the tick price between tries.
        Returns the final result object (or None)."""
        retryable = {
            getattr(mt5, "TRADE_RETCODE_REQUOTE", 10004),
            getattr(mt5, "TRADE_RETCODE_PRICE_CHANGED", 10020),
            getattr(mt5, "TRADE_RETCODE_PRICE_OFF", 10021),
            getattr(mt5, "TRADE_RETCODE_REJECT", 10006),
        }
        result = None
        for attempt in range(self.order_send_retries + 1):
            if refresh_price and attempt > 0:
                tick = mt5.symbol_info_tick(self.symbol)
                if tick is not None:
                    request["price"] = tick.ask if request["type"] == mt5.ORDER_TYPE_BUY else tick.bid
            result = mt5.order_send(request)
            if result is None:
                self.logger.error(f"Order send returned None (attempt {attempt+1}). last_error: {mt5.last_error()}")
                continue
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                return result
            if result.retcode in retryable and attempt < self.order_send_retries:
                self.logger.warning(f"Order retcode {result.retcode} ({result.comment}); retry {attempt+1}")
                time.sleep(0.5)
                continue
            return result
        return result

    def _get_open_positions(self):
        """Return this bot's open positions as position-like objects.

        LIVE: real MT5 positions filtered by magic (None on query error -- callers
        must NOT treat None as 'flat'). PAPER: SimpleNamespace wrappers around the
        simulated positions, with a fresh floating profit computed from the tick."""
        if self.paper_mode:
            tick = mt5.symbol_info_tick(self.symbol)
            out = []
            for p in list(self.paper_positions.values()):
                cur = (tick.bid if p['type'] == 0 else tick.ask) if tick else p['price_open']
                profit = 0.0
                try:
                    action = mt5.ORDER_TYPE_BUY if p['type'] == 0 else mt5.ORDER_TYPE_SELL
                    calc = mt5.order_calc_profit(action, self.symbol, p['volume'], p['price_open'], cur)
                    profit = float(calc) if calc is not None else 0.0
                except Exception:
                    profit = 0.0
                p['profit'] = profit
                out.append(SimpleNamespace(price_current=cur, **p))
            return out

        positions = mt5.positions_get(symbol=self.symbol)
        if positions is None:
            return None  # query error / disconnect -- do NOT treat as flat
        return [p for p in positions if p.magic == self.magic_number]

    def _sl_exit_reason(self, position):
        """Label an SL-triggered exit. Momentum: 'Trail' if the stop was ratcheted away
        from its original level (trailing exit), else 'Stop Loss hit'. SMC path is
        unchanged (always 'Stop Loss hit')."""
        if self.engine == 'momentum':
            orig = getattr(position, 'orig_sl', None)
            if orig is not None and abs(position.sl - orig) > (self.symbol_point / 2.0):
                return "Trail"
        return "Stop Loss hit"

    def _paper_check_sl_tp(self, position):
        """PAPER: simulate the broker closing a position on an SL/TP touch.

        Checking only the CURRENT tick was dishonest: the loop runs every few seconds, so
        any stop breached BETWEEN two checks was missed entirely, and the position carried
        on as if the stop had never been hit. That flatters results -- it silently deletes
        exactly the losers a real stop would have taken.

        We now replay the M1 bars that closed since this position was last checked and
        detect the breach against the bar EXTREMES. On a breach we fill at the WORSE of
        (the stop, the breaching bar's open if it gapped straight through, the current
        price) -- which is how a stop behaves through a gap: it becomes a market order at
        whatever is available, not a guarantee of the stop price.

        TP fills are NOT improved on a gap (filled at the TP), so the simulation cannot
        flatter itself in either direction.
        """
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            return False
        pos = self.paper_positions.get(position.ticket)
        if pos is None:
            return False

        is_buy = position.type == 0
        cur = tick.bid if is_buy else tick.ask          # the side this position exits on
        sl, tp = position.sl, position.tp

        # M1 bars that closed since the last check (bar times are SERVER epochs, like ticks)
        last_seen = int(pos.get('last_check_srv') or pos.get('time_server') or 0)
        highs_lows = []
        try:
            bars = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M1, 0, 60)
            if bars is not None:
                for b in bars:
                    if int(b['time']) > last_seen:
                        highs_lows.append((float(b['open']), float(b['high']), float(b['low'])))
        except Exception as e:
            self.logger.error(f"[PAPER] could not fetch M1 bars for fill simulation: {e}")
        pos['last_check_srv'] = int(getattr(tick, 'time', 0) or 0)

        # ---- stop loss -----------------------------------------------------
        if sl > 0:
            worst = None
            for o, h, l in highs_lows:
                if (is_buy and l <= sl) or ((not is_buy) and h >= sl):
                    # gapped straight through the stop? then the open is the realistic fill
                    gap = o if ((is_buy and o <= sl) or ((not is_buy) and o >= sl)) else None
                    cands = [sl] + ([gap] if gap is not None else [])
                    w = min(cands) if is_buy else max(cands)
                    worst = w if worst is None else (min(worst, w) if is_buy else max(worst, w))
            touched_now = (is_buy and cur <= sl) or ((not is_buy) and cur >= sl)
            if worst is not None or touched_now:
                cands = [sl, cur] + ([worst] if worst is not None else [])
                fill = min(cands) if is_buy else max(cands)
                if worst is not None and not touched_now:
                    self.logger.warning(
                        f"[PAPER] stop for #{position.ticket} was breached BETWEEN checks "
                        f"(bar extreme), filling at {fill:.2f} not the current {cur:.2f}")
                self._close_paper_position(position.ticket, fill, self._sl_exit_reason(position))
                return True

        # ---- take profit (no gap improvement: filled AT the TP) -------------
        if tp > 0:
            hit = (is_buy and cur >= tp) or ((not is_buy) and cur <= tp)
            if not hit:
                for o, h, l in highs_lows:
                    if (is_buy and h >= tp) or ((not is_buy) and l <= tp):
                        hit = True
                        break
            if hit:
                self._close_paper_position(position.ticket, tp, "Take Profit hit")
                return True
        return False

    def _paper_trade_costs(self, pos, exit_srv_epoch):
        """Commission + swap for a simulated trade, using the SAME model as the backtest
        (modules/broker_costs.py). Without these, paper 'net' was a gross price move and
        systematically overstated the result."""
        direction = "BUY" if pos['type'] == 0 else "SELL"
        lots = float(pos['volume'])
        commission = commission_cost(lots, self.commission_per_lot)
        swap = 0.0
        nights = (0, 0)
        try:
            si = mt5.symbol_info(self.symbol)
            if si is not None and getattr(si, 'swap_mode', 0) == 1:   # POINTS
                swap, n1, n3 = swap_cost(
                    direction, lots,
                    entry_epoch_srv=int(pos.get('time_server') or 0),
                    exit_epoch_srv=int(exit_srv_epoch or 0),
                    swap_long_pts=float(si.swap_long), swap_short_pts=float(si.swap_short),
                    point=float(si.point), contract_size=float(si.trade_contract_size),
                    fx_rate=float(self.momentum.gbpusd) if self.momentum else 1.0,
                    swap_rollover3days=getattr(si, 'swap_rollover3days', 3))
                nights = (n1, n3)
            elif si is not None and not self._paper_swap_warned:
                self._paper_swap_warned = True
                self.logger.warning(
                    f"[PAPER] swap_mode={si.swap_mode} is not POINTS; swap is NOT modelled "
                    "for this symbol, so paper net EXCLUDES financing.")
        except Exception as e:
            self.logger.error(f"[PAPER] swap calculation failed ({e}); swap treated as 0")
        return commission, swap, nights

    def _close_paper_position(self, ticket, exit_price, reason, scalp=False):
        """PAPER: simulate a close -> log, record stats, notify, drop the sim position."""
        pos = self.paper_positions.get(ticket)
        if pos is None:
            return
        direction = "BUY" if pos['type'] == 0 else "SELL"
        gross = 0.0
        try:
            action = mt5.ORDER_TYPE_BUY if pos['type'] == 0 else mt5.ORDER_TYPE_SELL
            calc = mt5.order_calc_profit(action, self.symbol, pos['volume'], pos['price_open'], exit_price)
            gross = float(calc) if calc is not None else 0.0
        except Exception:
            gross = 0.0

        # Paper NET = gross price move - commission + swap, via the same model the lab uses.
        tick = mt5.symbol_info_tick(self.symbol)
        exit_srv = int(getattr(tick, 'time', 0) or 0) if tick else 0
        commission, swap, (n1, n3) = self._paper_trade_costs(pos, exit_srv)
        profit = gross - commission + swap

        nights = n1 + n3
        self.logger.warning(
            f"[PAPER] Would CLOSE #{ticket} {direction} @ {exit_price} ({reason}) "
            f"NET {profit:.2f} = gross {gross:.2f} - comm {commission:.2f} + swap {swap:.2f}"
            + (f" ({nights} night(s), {n3} triple)" if nights else "")
        )
        self.paper_closed_trades.append(
            {'time': datetime.now(), 'net': float(profit), 'ticket': int(ticket)})
        profit_pips = abs(exit_price - pos['price_open']) / self.pip_size
        if profit < 0:
            profit_pips = -profit_pips
        try:
            self.stats_tracker.end_trade({
                'exit_price': exit_price,
                'exit_reason': reason,
                'profit': profit,            # NET, so the stats file is not flattered either
                'gross_profit': gross,
                'commission': commission,
                'swap': swap,
                'profit_pips': profit_pips,
                'expected_exit': pos['tp'] if profit > 0 else pos['sl'],
            })
        except Exception:
            pass
        try:
            self.telegram.notify_trade_closed(
                symbol=self.symbol, direction=direction, lot_size=pos['volume'],
                entry_price=pos['price_open'], exit_price=exit_price, profit=profit, reason=reason)
        except Exception:
            pass
        self.paper_positions.pop(ticket, None)
        self.tracked_positions.pop(ticket, None)
        if scalp:
            self.last_trade_time = datetime.now()
            self.last_trade_type = 'scalp'
        self._save_state()

    def calculate_atr(self, timeframe=None, period=None, closed_only=True):
        """Calculate Average True Range.

        By default this uses the configured ATR timeframe and closed candles.
        """
        try:
            tf = timeframe if timeframe is not None else getattr(self, "atr_timeframe", self.timeframe)

            if period is None:
                period = self.atr_period if self.volatility_enabled else 14

            start_pos = 1 if closed_only else 0
            rates = mt5.copy_rates_from_pos(self.symbol, tf, start_pos, int(period) + 1)

            if rates is None or len(rates) < int(period) + 1:
                return None

            high = np.array([r['high'] for r in rates], dtype=float)
            low = np.array([r['low'] for r in rates], dtype=float)
            close = np.array([r['close'] for r in rates], dtype=float)

            tr_list = []
            for i in range(1, len(rates)):
                hl = high[i] - low[i]
                hc = abs(high[i] - close[i - 1])
                lc = abs(low[i] - close[i - 1])
                tr_list.append(max(hl, hc, lc))

            if len(tr_list) < int(period):
                return None

            return float(np.mean(tr_list[-int(period):]))

        except Exception as e:
            self.logger.error(f"Error calculating ATR: {e}")
            return None

    def update_trading_mode(self):
        """Update trading mode based on ATR"""
        if not self.volatility_enabled:
            return
        
        try:
            atr = self.calculate_atr()
            if atr is None:
                return
            
            self.current_atr = atr
            previous_mode = self.current_mode
            
            self.current_mode = 'scalp' if atr > self.atr_scalp_threshold else 'normal'
            
            if previous_mode != self.current_mode:
                if self.current_mode == 'scalp':
                    self.logger.info("="*50)
                    self.logger.info(f"SWITCHED TO SCALPING MODE | ATR: {atr:.4f}")
                    self.logger.info("="*50)
                else:
                    self.logger.info("="*50)
                    self.logger.info(f"SWITCHED TO NORMAL MODE | ATR: {atr:.4f}")
                    self.logger.info("="*50)
            
            self.last_atr_check = datetime.now()
            
        except Exception as e:
            self.logger.error(f"Error updating trading mode: {e}")
    
    def check_quick_profit_exit(self, position):
        """Check quick profit exit in scalping mode"""
        if not self.volatility_enabled or self.current_mode != 'scalp':
            return False
        
        try:
            current_profit = position.profit

            if current_profit >= self.scalp_profit_target:
                self.logger.info(f"SCALP EXIT | Profit: £{current_profit:.2f}")

                tick = mt5.symbol_info_tick(self.symbol)
                if tick is None:
                    return False

                close_price = tick.bid if position.type == 0 else tick.ask

                if self.paper_mode:
                    self._close_paper_position(position.ticket, close_price, "Quick scalp profit", scalp=True)
                    return True

                close_type = mt5.ORDER_TYPE_SELL if position.type == 0 else mt5.ORDER_TYPE_BUY
                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": position.symbol,
                    "volume": position.volume,
                    "type": close_type,
                    "position": position.ticket,
                    "price": close_price,
                    "deviation": self.order_deviation,
                    "magic": self.magic_number,
                    "comment": "scalp_quick_profit",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": self._select_filling_mode(),
                }

                result = self._order_send_retry(request, refresh_price=False)

                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    self.last_trade_time = datetime.now()
                    self.last_trade_type = 'scalp'
                    self._save_state()
                    self.logger.info(f"Position closed | Ticket: {position.ticket}")
                    return True
                else:
                    self.logger.error(
                        f"Scalp close failed for #{position.ticket}: "
                        f"{getattr(result, 'retcode', None)} {mt5.last_error()}")

        except Exception as e:
            self.logger.error(f"Error in quick profit exit: {e}")
        
        return False
    
    def is_within_trading_hours(self):
        """Check if within trading hours - returns tuple (bool, str) with status message"""
        now = datetime.now()
        weekday = now.weekday()
        hour = now.hour
        
        days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        current_day = days[weekday]
        
        # Saturday check
        if weekday == 5 and self.saturday_closed:
            if self.sunday_closed:
                # Sunday closed - open Monday
                monday_open = now.replace(hour=self.monday_open_hour, minute=0, second=0, microsecond=0) + timedelta(days=2)
                hours_until_open = (monday_open - now).total_seconds() / 3600
                return False, f"Market CLOSED - {current_day} | Opens Monday {self.monday_open_hour:02d}:00 (in {hours_until_open:.1f} hours)"
            else:
                # Sunday trading enabled - open Sunday
                sunday_open = now.replace(hour=self.sunday_open_hour, minute=0, second=0, microsecond=0) + timedelta(days=1)
                hours_until_open = (sunday_open - now).total_seconds() / 3600
                return False, f"Market CLOSED - {current_day} | Opens Sunday {self.sunday_open_hour:02d}:00 (in {hours_until_open:.1f} hours)"
        
        # Sunday check
        if weekday == 6:
            if self.sunday_closed:
                # Sunday closed - open Monday
                monday_open = now.replace(hour=self.monday_open_hour, minute=0, second=0, microsecond=0) + timedelta(days=1)
                hours_until_open = (monday_open - now).total_seconds() / 3600
                return False, f"Market CLOSED - {current_day} {now.strftime('%H:%M')} | Opens Monday {self.monday_open_hour:02d}:00 (in {hours_until_open:.1f} hours)"
            elif hour < self.sunday_open_hour:
                # Sunday trading enabled but before opening hour
                sunday_open = now.replace(hour=self.sunday_open_hour, minute=0, second=0, microsecond=0)
                hours_until_open = (sunday_open - now).total_seconds() / 3600
                return False, f"Market CLOSED - {current_day} {now.strftime('%H:%M')} | Opens today at {self.sunday_open_hour:02d}:00 (in {hours_until_open:.1f} hours)"
        
        # Friday after closing hour. only if we actually close on the weekend
        if weekday == 4 and hour >= self.friday_close_hour and (self.saturday_closed or self.sunday_closed):
            if self.sunday_closed:
                # Sunday closed - open Monday
                monday_open = now.replace(hour=self.monday_open_hour, minute=0, second=0, microsecond=0) + timedelta(days=3)
                hours_until_open = (monday_open - now).total_seconds() / 3600
                return False, f"Market CLOSED - {current_day} after {self.friday_close_hour:02d}:00 | Opens Monday {self.monday_open_hour:02d}:00 (in {hours_until_open:.1f} hours)"
            else:
                # Sunday trading enabled - open Sunday
                sunday_open = now.replace(hour=self.sunday_open_hour, minute=0, second=0, microsecond=0) + timedelta(days=2)
                hours_until_open = (sunday_open - now).total_seconds() / 3600
                return False, f"Market CLOSED - {current_day} after {self.friday_close_hour:02d}:00 | Opens Sunday {self.sunday_open_hour:02d}:00 (in {hours_until_open:.1f} hours)"

        # Generic daily open/close window if configured
        if self.weekday_open_hour is not None and self.weekday_close_hour is not None:
            if hour < self.weekday_open_hour or hour >= self.weekday_close_hour:
                return False, (
                    f"Market CLOSED - {current_day} {now.strftime('%H:%M')} | "
                    f"Daily window {self.weekday_open_hour:02d}:00-"
                    f"{self.weekday_close_hour:02d}:00"
                )

        return True, f"Market OPEN - {current_day} {now.strftime('%H:%M')}"

    def is_in_swap_avoidance_window(self):
        """
        Check if we are inside a configured swap or rollover avoidance window.
        Times are interpreted in broker server time using BROKER.broker_timezone_offset.
        Returns (bool, str) where str is a human readable status message.
        """
        if not getattr(self, "swap_avoidance_enabled", False):
            return False, ""

        if not self.swap_avoidance_windows:
            return False, ""

        try:
            broker_offset = self.config.get('BROKER', {}).get('broker_timezone_offset', 0)
            # Approximate server time = local time + offset
            server_now = datetime.now() + timedelta(hours=broker_offset)
            current_time = server_now.time()

            for window in self.swap_avoidance_windows:
                start_str = window.get("start")
                end_str = window.get("end")
                if not start_str or not end_str:
                    continue

                try:
                    start_hour, start_minute = map(int, start_str.split(":"))
                    end_hour, end_minute = map(int, end_str.split(":"))
                except ValueError:
                    continue  # skip malformed entries

                # Build time objects from the datetime module, not the time module
                start_time = dt.time(start_hour, start_minute)
                end_time = dt.time(end_hour, end_minute)

                # Handle windows that cross midnight, e.g. 23:30 -> 00:20
                if start_time <= end_time:
                    in_window = start_time <= current_time <= end_time
                else:
                    in_window = (current_time >= start_time) or (current_time <= end_time)

                if in_window:
                    msg = (
                        f"Swap avoidance window {start_str}-{end_str} (server time). "
                        f"Server now {server_now.strftime('%Y-%m-%d %H:%M')}"
                    )
                    return True, msg

            return False, ""
        except Exception as e:
            self.logger.error(f"Error checking swap avoidance window: {e}")
            return False, ""
    
    def get_market_data(self, bars=100, timeframe=None, closed_only=True):
        """Get market data from MT5.

        Args:
            bars: number of bars to fetch
            timeframe: MT5 timeframe constant. defaults to the bot entry timeframe
            closed_only: if True, skip the currently forming candle (start_pos=1)
        """
        try:
            tf = timeframe if timeframe is not None else self.timeframe
            start_pos = 1 if closed_only else 0
            rates = mt5.copy_rates_from_pos(self.symbol, tf, start_pos, int(bars))
            if rates is None or len(rates) == 0:
                return None

            # Ensure oldest -> newest
            if len(rates) >= 2 and rates[0]['time'] > rates[-1]['time']:
                rates = rates[::-1]

            return rates
        except Exception as e:
            self.logger.error(f"Error getting market data: {e}")
            return None

    def _has_open_positions_for_bot(self):
        """Return True if there are any open positions for this bot on this symbol."""
        try:
            positions = self._get_open_positions()
            return bool(positions)
        except Exception as e:
            self.logger.debug(f"Position lookup failed. {e}")
        return False
    
    
    def _is_position_risk_free(self, position):
        """Return True if the position stop loss is at breakeven or better."""
        try:
            sl = getattr(position, "sl", 0.0) or 0.0
            entry = getattr(position, "price_open", 0.0) or 0.0
            if sl <= 0.0 or entry <= 0.0:
                return False

            # BUY: SL >= entry is breakeven or better
            # SELL: SL <= entry is breakeven or better
            if position.type == mt5.POSITION_TYPE_BUY:
                return sl >= entry
            if position.type == mt5.POSITION_TYPE_SELL:
                return sl <= entry
            return False
        except Exception:
            return False

    def _can_open_additional_position(self, bot_positions):
        """Allow stacking only if the existing position is risk-free.

        Intended behaviour:
            - If there are no open positions, allow.
            - If max_positions is 1, do not stack.
            - If there is exactly 1 open position and max_positions >= 2, allow a 2nd
              only if the existing position is at breakeven or better.
        """
        try:
            count = len(bot_positions)
            if count == 0:
                return True

            # Momentum engine is strictly one position at a time (matches the backtest).
            if self.engine == 'momentum':
                return False

            if self.max_positions <= 1:
                return False

            if count >= self.max_positions:
                return False

            # Only allow stacking when all existing bot positions are risk-free
            return all(self._is_position_risk_free(p) for p in bot_positions)

        except Exception:
            return False

    def check_daily_profit(self):
        """Check daily profit and update pause status. TIMEZONE AWARE + SWAP INCLUDED with pending loss and profit behaviour"""
        try:
            # Get timezone offset from config (default 0 if not specified)
            broker_timezone_offset = self.config.get('BROKER', {}).get('broker_timezone_offset', 0)
            now_local = datetime.now()
            
            # Check if new day. reset pause flags (using LOCAL time for date check)
            current_date = now_local.date()
            if current_date != self.last_target_check_date:
                self.daily_target_reached = False
                self.loss_limit_pending = False
                self.profit_target_pending = False
                self.starting_equity_today = None  # reset starting equity tracker
                self.last_target_check_date = current_date
                self.logger.info("NEW DAY. Daily profit/loss logic reset (Local timezone)")
                if broker_timezone_offset != 0:
                    self.logger.info(f"Timezone offset. Broker is GMT+{broker_timezone_offset}, queries adjusted accordingly")
            
            # If we already have a confirmed daily stop, stay paused
            if self.daily_target_reached:
                return True
            
            # Calculate today's profit. TIMEZONE ADJUSTED
            local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            broker_today_start = local_midnight + timedelta(hours=broker_timezone_offset)
            broker_today_end = broker_today_start + timedelta(days=1) - timedelta(seconds=1)
            broker_now = min(now_local + timedelta(hours=broker_timezone_offset), broker_today_end)
                        
            # v5.0.0: in PAPER mode there are no broker deals; accumulate realised NET
            # P&L from closed SIMULATED positions (today, local date) so the SAME daily
            # target / loss logic runs exactly as it would live.
            if self.paper_mode:
                today = now_local.date()
                net_profit = sum(t['net'] for t in self.paper_closed_trades
                                 if t['time'].date() == today)
            else:
                # Query MT5 with broker-adjusted times
                deals = mt5.history_deals_get(broker_today_start, broker_now)

                if deals is None:
                    self.logger.debug("  No deals returned from MT5")
                    return self.daily_target_reached or self.loss_limit_pending or self.profit_target_pending

                # Calculate NET profit (only count exit deals to avoid double-counting)
                total_profit = 0.0
                total_commission = 0.0
                total_swap = 0.0

                # Count ALL deals for commission, but only EXIT deals for profit
                for deal in deals:
                    if deal.magic == self.magic_number:
                        # Commission from all deals (entry + exit)
                        total_commission += abs(deal.commission)

                        # Only exit deals for profit/swap
                        if deal.entry == mt5.DEAL_ENTRY_OUT:
                            total_profit += deal.profit
                            total_swap += deal.swap

                # NET profit = Gross profit - Commission + Swap
                net_profit = total_profit - total_commission + total_swap

            # Check if there are open positions for this bot on this symbol
            has_open_positions_for_bot = self._has_open_positions_for_bot()
            
            # --- DAILY LOSS LIMIT ENFORCEMENT ---
            loss_breached = False
            max_daily_loss_cfg = self.config.get('RISK', {}).get('max_daily_loss', 0)
            max_daily_loss_currency = self.config.get('RISK', {}).get('max_daily_loss_currency', 'GBP')

            cap = None
            account_currency = None

            if max_daily_loss_cfg and max_daily_loss_cfg > 0:
                # Determine account currency
                try:
                    acct = mt5.account_info()
                    account_currency = getattr(acct, 'currency', None) or self.config.get('BROKER', {}).get('account_currency')
                except Exception:
                    account_currency = self.config.get('BROKER', {}).get('account_currency')

                # Convert configured loss into account currency if needed
                max_daily_loss_account_ccy = float(max_daily_loss_cfg)
                if account_currency and account_currency.upper() != max_daily_loss_currency.upper():
                    src = max_daily_loss_currency.upper()
                    dst = account_currency.upper()
                    possible_pairs = [f"{src}{dst}", f"{dst}{src}"]
                    rate = None
                    for pair in possible_pairs:
                        try:
                            tick = mt5.symbol_info_tick(pair)
                            if tick is not None:
                                mid = (tick.ask + tick.bid) / 2.0
                                if pair == f"{src}{dst}":
                                    max_daily_loss_account_ccy *= mid
                                else:
                                    max_daily_loss_account_ccy /= mid
                                rate = mid
                                break
                        except Exception:
                            continue

                    if rate is None:
                        self.logger.warning(f"Failed to auto-convert {max_daily_loss_cfg} {src} to {dst}. Treating limit as {dst}.")
                        # fall through using raw number

                try:
                    cap = float(max_daily_loss_account_ccy)
                except Exception:
                    cap = float(max_daily_loss_cfg)

                # CHECK 1. closed-deals net profit
                if net_profit <= -cap:
                    loss_breached = True

                # CHECK 2. optional equity drawdown based (skipped in paper: real account
                # equity does not reflect simulated trades, so realised paper net is authoritative)
                if (not self.paper_mode) and self.config.get('RISK', {}).get('loss_limit_by_equity', True):
                    try:
                        account_info = mt5.account_info()
                        current_equity = float(account_info.equity)
                        starting_equity = getattr(self, 'starting_equity_today', None)
                        if starting_equity is None:
                            # v5.0.0 (M10): seed from EQUITY (includes any floating P&L at
                            # the day boundary), not balance, so the drawdown base is correct.
                            self.starting_equity_today = float(account_info.equity)
                            starting_equity = self.starting_equity_today

                        drawdown = starting_equity - current_equity
                        if drawdown >= cap:
                            loss_breached = True
                    except Exception as e:
                        self.logger.debug(f"Equity-based loss check failed. {e}")

            # Handle existing pending loss state first
            if self.loss_limit_pending:
                if has_open_positions_for_bot:
                    # Still waiting for positions to close. block new trades
                    return True
                else:
                    # All positions for this bot are closed. re-evaluate loss
                    if loss_breached and cap is not None:
                        self.daily_target_reached = True
                        self.loss_limit_pending = False
                        self.logger.info("=" * 60)
                        self.logger.info("DAILY LOSS LIMIT CONFIRMED AFTER POSITIONS CLOSED")
                        if account_currency:
                            self.logger.info(f"Net profit today. {net_profit:.2f} {account_currency} vs cap -{cap:.2f} {account_currency}")
                        self.logger.info("Bot will PAUSE new trades until midnight (Local).")
                        self.logger.info("=" * 60)
                        try:
                            self.telegram.notify_daily_loss_limit(self.symbol, net_profit, max_daily_loss_cfg)
                        except Exception:
                            pass
                        return True
                    else:
                        # Loss has recovered above threshold. clear and resume
                        self.logger.info("Loss limit recovered after positions closed. resuming trading for today.")
                        self.loss_limit_pending = False
                        # fall through to profit target logic

            # No previous pending loss state. handle a fresh loss breach
            if loss_breached and cap is not None and not self.daily_target_reached:
                if has_open_positions_for_bot:
                    # Soft lock. wait for these positions to close
                    self.loss_limit_pending = True
                    self.logger.info("=" * 60)
                    self.logger.info("DAILY LOSS LIMIT BREACHED WITH OPEN POSITIONS")
                    if account_currency:
                        self.logger.info(f"Current net profit. {net_profit:.2f} {account_currency} vs cap -{cap:.2f} {account_currency}")
                    self.logger.info("Pausing NEW trades until existing positions for this bot are closed. loss will then be re-evaluated.")
                    self.logger.info("=" * 60)
                    return True
                else:
                    # Hard daily stop straight away
                    self.daily_target_reached = True
                    self.logger.info("=" * 60)
                    self.logger.info(f"DAILY LOSS LIMIT REACHED. -{max_daily_loss_cfg:.2f} {max_daily_loss_currency} (approx {cap:.2f} {account_currency})")
                    self.logger.info("Bot will PAUSE new trades until midnight (Local). Existing positions will be managed.")
                    self.logger.info("=" * 60)
                    try:
                        self.telegram.notify_daily_loss_limit(self.symbol, net_profit, max_daily_loss_cfg)
                    except Exception:
                        pass
                    return True

            # --- DAILY PROFIT TARGET ENFORCEMENT ---
            profit_target_breached = False
            if self.daily_profit_target > 0:
                if net_profit >= self.daily_profit_target:
                    profit_target_breached = True

            # Handle existing pending profit state
            if self.profit_target_pending:
                if has_open_positions_for_bot:
                    # Still waiting for positions to close. block new trades
                    return True
                else:
                    # All positions closed. confirm or cancel profit pause
                    if profit_target_breached:
                        self.daily_target_reached = True
                        self.profit_target_pending = False
                        self.logger.info("=" * 60)
                        self.logger.info("DAILY PROFIT TARGET CONFIRMED AFTER POSITIONS CLOSED")
                        self.logger.info(f"Net profit today. £{net_profit:.2f} (target £{self.daily_profit_target:.2f})")
                        self.logger.info("Bot will PAUSE new trades until midnight (00:00 Local)")
                        self.logger.info("=" * 60)
                        try:
                            self.telegram.notify_daily_target_reached(self.symbol, net_profit, self.daily_profit_target)
                        except Exception:
                            pass
                        return True
                    else:
                        # Profit has fallen back below target. resume trading
                        self.logger.info("Daily profit dropped back below target after positions closed. resuming trading for today.")
                        self.profit_target_pending = False
                        # fall through

            # Fresh profit target breach and no existing pending state
            if profit_target_breached and not self.daily_target_reached:
                if has_open_positions_for_bot:
                    # Soft lock. wait for these positions to close
                    self.profit_target_pending = True
                    self.logger.info("=" * 60)
                    self.logger.info("DAILY PROFIT TARGET REACHED WITH OPEN POSITIONS")
                    self.logger.info(f"Current net profit. £{net_profit:.2f} (target £{self.daily_profit_target:.2f})")
                    self.logger.info("Pausing NEW trades until existing positions for this bot are closed. profit will then be re-evaluated.")
                    self.logger.info("=" * 60)
                    return True
                else:
                    # Hard daily profit stop straight away
                    self.daily_target_reached = True
                    self.logger.info("=" * 60)
                    self.logger.info(f"DAILY PROFIT TARGET REACHED. £{net_profit:.2f}")
                    self.logger.info("Bot will PAUSE new trades until midnight (00:00 Local)")
                    self.logger.info("Existing positions will still be managed")
                    self.logger.info("=" * 60)
                    try:
                        self.telegram.notify_daily_target_reached(self.symbol, net_profit, self.daily_profit_target)
                    except Exception:
                        pass
                    return True
            
            # If none of the conditions fired, only pause if a flag is set
            return self.daily_target_reached or self.loss_limit_pending or self.profit_target_pending

        except Exception as e:
            self.logger.error(f"Error checking daily profit. {e}")
            return self.daily_target_reached or self.loss_limit_pending or self.profit_target_pending
    
    def check_weekly_limits(self):
        """
        Check weekly NET P&L against configured weekly caps.

        NET P&L = profit from exit deals - commission from all deals + swap

        Returns:
            bool: True if weekly cap is active and bot should pause new trades
        """
        try:
            risk_cfg = self.config.get('RISK', {})
            if not risk_cfg.get('weekly_limits_enabled', False):
                return False

            max_weekly_profit = risk_cfg.get('max_weekly_profit', 0.0)
            max_weekly_loss = risk_cfg.get('max_weekly_loss', 0.0)

            if max_weekly_profit <= 0 and max_weekly_loss <= 0:
                # No usable weekly caps set
                return False

            # Timezone handling similar to daily check
            broker_timezone_offset = self.config.get('BROKER', {}).get('broker_timezone_offset', 0)
            now_local = datetime.now()

            # Broker now and "today start" in broker time
            local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            broker_today_start = local_midnight + timedelta(hours=broker_timezone_offset)
            broker_now = now_local + timedelta(hours=broker_timezone_offset)

            # Work out week start in broker timezone
            week_day_map = {
                'monday': 0,
                'tuesday': 1,
                'wednesday': 2,
                'thursday': 3,
                'friday': 4,
                'saturday': 5,
                'sunday': 6,
            }
            week_start_index = week_day_map.get(self.week_start_day, 0)
            current_weekday = broker_today_start.weekday()
            days_since_week_start = (current_weekday - week_start_index) % 7
            broker_week_start = broker_today_start - timedelta(days=days_since_week_start)

            # Reset weekly state on new week
            week_start_date = broker_week_start.date()
            if self.last_week_start_date is None or self.last_week_start_date != week_start_date:
                if self.last_week_start_date is not None:
                    self.logger.info("NEW WEEK detected. resetting weekly risk state")
                self.last_week_start_date = week_start_date
                self.weekly_limit_triggered = False
                self.weekly_limit_side = None

            # If already locked for this week. keep blocking new trades
            if self.weekly_limit_triggered:
                return True

            # v5.0.0: PAPER mode -> sum realised NET from closed simulated positions in
            # this week instead of broker deals, so the weekly pause behaves as it would live.
            if self.paper_mode:
                net_weekly_profit = sum(t['net'] for t in self.paper_closed_trades
                                        if t['time'].date() >= week_start_date)
            else:
                # Pull deals for this week in broker time
                deals = mt5.history_deals_get(broker_week_start, broker_now)
                if deals is None:
                    self.logger.debug("No deals returned for weekly limits calculation")
                    return False

                total_profit = 0.0
                total_commission = 0.0
                total_swap = 0.0

                # Count commission from all deals. profit or swap only from exit deals
                for deal in deals:
                    if deal.magic == self.magic_number:
                        total_commission += abs(deal.commission)
                        if deal.entry == mt5.DEAL_ENTRY_OUT:
                            total_profit += deal.profit
                            total_swap += deal.swap

                net_weekly_profit = total_profit - total_commission + total_swap

            weekly_loss_hit = max_weekly_loss > 0 and net_weekly_profit <= -max_weekly_loss
            weekly_profit_hit = max_weekly_profit > 0 and net_weekly_profit >= max_weekly_profit

            if not weekly_loss_hit and not weekly_profit_hit:
                return False

            # Lock for the rest of the week
            self.weekly_limit_triggered = True
            self.weekly_limit_side = 'loss' if weekly_loss_hit else 'profit'

            self.logger.info("=" * 60)
            if weekly_loss_hit:
                self.logger.info("WEEKLY LOSS LIMIT REACHED")
                self.logger.info(f"Net weekly profit. £{net_weekly_profit:.2f} vs loss cap £{max_weekly_loss:.2f}")
            else:
                self.logger.info("WEEKLY PROFIT LIMIT REACHED")
                self.logger.info(f"Net weekly profit. £{net_weekly_profit:.2f} vs profit cap £{max_weekly_profit:.2f}")
            self.logger.info("Bot will PAUSE new trades for the rest of this trading week")
            self.logger.info("=" * 60)

            # Telegram alerts
            try:
                if weekly_loss_hit:
                    self.telegram.notify_weekly_loss_limit(self.symbol, net_weekly_profit, max_weekly_loss)
                else:
                    self.telegram.notify_weekly_profit_limit(self.symbol, net_weekly_profit, max_weekly_profit)
            except Exception:
                # Telegram is optional. never break trading on notification failure
                pass

            return True

        except Exception as e:
            self.logger.error(f"Error in weekly limits check. {e}")
            return False

    def is_in_cooldown(self):
        """Check trade cooldown"""
        if self.last_trade_time is None:
            return False, 0
        
        cooldown_seconds = self.scalp_cooldown if (self.volatility_enabled and self.last_trade_type == 'scalp') else self.normal_cooldown
        
        time_since_last = (datetime.now() - self.last_trade_time).total_seconds()
        if time_since_last < cooldown_seconds:
            remaining = int(cooldown_seconds - time_since_last)
            return True, remaining
        
        return False, 0
    
    def update_tracked_positions(self):
        """Update tracked positions.

        v5.0.0 (H3): when first registering an open position, restore its entry_atr and
        breakeven_applied from the persisted state file if present (so a restart doesn't
        reset the breakeven flag or recompute entry_atr from the wrong, current ATR).
        Also: a None result (query error/disconnect) is NOT treated as 'all closed'.
        """
        try:
            current_positions = self._get_open_positions()
            if current_positions is None:
                return  # query error -- skip; do not spuriously mark positions closed

            current_positions = list(current_positions)
            current_tickets = set([p.ticket for p in current_positions])

            tracked_tickets = set(self.tracked_positions.keys())
            closed_tickets = tracked_tickets - current_tickets

            for ticket in closed_tickets:
                if not self.paper_mode:
                    self.handle_position_closure(ticket)  # paper closes handled inline
                del self.tracked_positions[ticket]

            restored = (self._restored_state or {}).get('positions', {})
            for position in current_positions:
                if position.ticket not in self.tracked_positions:
                    saved = restored.get(str(position.ticket))
                    if saved:  # restore across restart
                        entry_atr = saved.get('entry_atr', 0) or 0
                        breakeven_applied = bool(saved.get('breakeven_applied', False))
                        self.logger.info(
                            f"Restored state for #{position.ticket}: "
                            f"entry_atr={entry_atr}, breakeven_applied={breakeven_applied}")
                    else:
                        entry_atr = self.calculate_atr()
                        if entry_atr is None:
                            entry_atr = 0
                        breakeven_applied = False

                    self.tracked_positions[position.ticket] = {
                        'entry': position.price_open,
                        'sl': position.sl,
                        'tp': position.tp,
                        'type': position.type,
                        'volume': position.volume,
                        'open_time': position.time,
                        'entry_atr': entry_atr,
                        'breakeven_applied': breakeven_applied,
                    }
                    self._save_state()
        except Exception as e:
            self.logger.error(f"Error updating tracked positions: {e}")
    
    def handle_position_closure(self, ticket):
        """Handle position closure"""
        try:
            if ticket not in self.tracked_positions:
                return
            
            pos_data = self.tracked_positions[ticket]
            deals = mt5.history_deals_get(position=ticket)
            
            if deals is None or len(deals) == 0:
                return
            
            exit_deal = None
            for deal in deals:
                if deal.entry == mt5.DEAL_ENTRY_OUT:
                    exit_deal = deal
            
            if exit_deal is None:
                return
            
            # v5.0.0 (M3): report NET per trade (gross - commission + swap) so per-trade
            # Telegram/stats values are on the SAME basis as the daily/weekly caps.
            gross_profit = exit_deal.profit
            total_commission = sum(abs(getattr(d, 'commission', 0.0)) for d in deals)
            total_swap = sum(getattr(d, 'swap', 0.0) for d in deals if d.entry == mt5.DEAL_ENTRY_OUT)
            net_profit = gross_profit - total_commission + total_swap

            direction = "BUY" if pos_data['type'] == 0 else "SELL"
            exit_price = exit_deal.price
            entry_price = pos_data['entry']

            if exit_deal.comment == "scalp_quick_profit":
                reason = "Quick scalp profit"
            else:
                reason = self.determine_close_reason(exit_price, pos_data['sl'], pos_data['tp'], direction)
                # Momentum has no fixed TP, so a trailed-stop exit lands away from the
                # original SL and would otherwise read as "Manual close": label it "Trail".
                # (SMC labelling is untouched.)
                if self.engine == 'momentum' and reason == "Manual close":
                    reason = "Trail"

            self.telegram.notify_trade_closed(
                symbol=self.symbol,
                direction=direction,
                lot_size=pos_data['volume'],
                entry_price=entry_price,
                exit_price=exit_price,
                profit=net_profit,
                reason=reason
            )

            profit_pips = abs(exit_price - entry_price) / self.pip_size
            if net_profit < 0:   # v5.0.0 (L2): direction-independent (old check was redundant)
                profit_pips = -profit_pips

            expected_exit = pos_data['tp'] if net_profit > 0 else pos_data['sl']

            self.stats_tracker.end_trade({
                'exit_price': exit_price,
                'exit_reason': reason,
                'profit': net_profit,
                'profit_pips': profit_pips,
                'expected_exit': expected_exit if expected_exit > 0 else exit_price
            })

            self.logger.info(
                f"Position closed: #{ticket}, {direction}, net £{net_profit:.2f} "
                f"(gross £{gross_profit:.2f}), {reason}")
            self._save_state()
        except Exception as e:
            self.logger.error(f"Error handling closure: {e}")
    
    def get_current_session(self):
        """Get current trading session"""
        try:
            hour = datetime.utcnow().hour
            if 0 <= hour < 9:
                return 'asia'
            elif 8 <= hour < 16:
                return 'london'
            elif 13 <= hour < 21:
                return 'new_york'
            else:
                return 'unknown'
        except:
            return 'unknown'
    
    def determine_close_reason(self, exit_price, sl_price, tp_price, direction):
        """Determine close reason"""
        try:
            tolerance = self.pip_size * 2
            
            if sl_price > 0 and abs(exit_price - sl_price) < tolerance:
                return "Stop Loss hit"
            
            if tp_price > 0 and abs(exit_price - tp_price) < tolerance:
                return "Take Profit hit"
            
            if direction == "BUY":
                if sl_price > 0 and exit_price > sl_price and exit_price < tp_price:
                    return "Trailing Stop"
            else:
                if sl_price > 0 and exit_price < sl_price and exit_price > tp_price:
                    return "Trailing Stop"
            
            return "Manual close"
        except:
            return "Unknown"
    
    def _generate_signal(self, bars_to_fetch):
        """Return a signal dict (or None) from the active engine, gated on a newly closed
        entry candle (one evaluation per closed entry bar). SMC path unchanged."""
        entry_rates = self.get_market_data(
            bars=bars_to_fetch,
            timeframe=getattr(self, "entry_timeframe", self.timeframe),
            closed_only=True,
        )
        if entry_rates is None:
            return None
        entry_bar_time = entry_rates[-1]['time']
        if self.last_entry_bar_time == entry_bar_time:
            return None  # not a new closed entry bar
        self.last_entry_bar_time = entry_bar_time

        bias_rates = self.get_market_data(
            bars=max(250, bars_to_fetch),
            timeframe=getattr(self, "bias_timeframe", self.timeframe),
            closed_only=True,
        )

        # ---- Momentum engine (shared module) ----
        if self.engine == 'momentum':
            return self._momentum_signal(entry_rates, bias_rates)

        # ---- SMC engine (unchanged) ----
        if bias_rates is not None:
            bias_bar_time = bias_rates[-1]['time']
            if self.last_bias_bar_time != bias_bar_time:
                self.last_bias_bar_time = bias_bar_time
                try:
                    bias_info = self.strategy.compute_structure_bias_from_rates(bias_rates)
                    if isinstance(bias_info, dict):
                        self.last_market_bias = bias_info.get('bias', 'NEUTRAL')
                        self.last_bias_detail = bias_info
                    else:
                        self.last_market_bias = str(bias_info or 'NEUTRAL')
                        self.last_bias_detail = {}
                except Exception as e:
                    self.logger.error(f"Error computing structure bias: {e}")
                    self.last_market_bias = 'NEUTRAL'
                    self.last_bias_detail = {}

        signal = None
        if (not self.strict_bias) or (self.last_market_bias in {'BULL', 'BEAR'}):
            signal = self.strategy.analyze_from_rates(entry_rates, bias=self.last_market_bias)
        else:
            self.logger.info(f"[BIAS] {self.bias_timeframe_str} bias is NEUTRAL. skipping entries")

        # v5.0.0 (M12): direction must match bias when strict_bias is on
        if signal and self.strict_bias:
            want = 'BULL' if signal['type'] == 'BUY' else 'BEAR'
            if self.last_market_bias != want:
                self.logger.info(
                    f"[BIAS] {signal['type']} signal rejected: bias is {self.last_market_bias}, needs {want}")
                signal = None
        return signal

    def _momentum_signal(self, entry_rates, bias_rates):
        """Momentum engine: session-gated, H4-trend-filtered M15 breakout via the shared
        MomentumBreakoutStrategy module. The VPS clock is UK local time, so datetime.now()
        gives the UK hour used for the session filter."""
        in_session = self.momentum.in_session(datetime.now().hour)
        m15_df = pd.DataFrame(entry_rates)
        h4_df = pd.DataFrame(bias_rates) if bias_rates is not None else None

        # Diagnostics (mirror MomentumBreakoutStrategy.signal internals so the log line
        # shows exactly the inputs behind the decision). Runs once per new M15 bar.
        lookback = self.momentum.lookback
        trend = self.momentum.compute_h4_trend(h4_df) if h4_df is not None else 0
        close = float(m15_df["close"].iloc[-1]) if len(m15_df) else float("nan")
        if len(m15_df) >= lookback + 1:
            prior = m15_df.iloc[-(lookback + 1):-1]
            prior_high = float(prior["high"].max())
            prior_low = float(prior["low"].min())
        else:
            prior_high = prior_low = float("nan")

        # Only produce a tradable signal inside the session with valid bias data;
        # the shared module stays the single source of truth for the decision.
        signal = None
        if in_session and h4_df is not None and len(h4_df) >= 1:
            signal = self.momentum.signal(m15_df, h4_df)
        direction = signal["type"] if signal else None

        self.logger.info(
            f"[MOM] session={in_session} trend={trend} close={close:.2f} "
            f"prHi={prior_high:.2f} prLo={prior_low:.2f} -> {direction or 'None'}")
        return signal

    def open_trade(self, signal):
        """Execute trade with ATR-based stops and broker validation"""
        try:
            order_type = signal['type']
            
            tick = mt5.symbol_info_tick(self.symbol)
            if tick is None:
                self.logger.error("Failed to get tick data")
                return False
            
            price = tick.ask if order_type == 'BUY' else tick.bid
            
            # Get current ATR for this trade
            current_atr = self.calculate_atr()
            if current_atr is None:
                self.logger.warning("Cannot calculate ATR, skipping trade")
                return False
            
            price = self._round_price(price)
            mt5_order_type = mt5.ORDER_TYPE_BUY if order_type == 'BUY' else mt5.ORDER_TYPE_SELL

            if self.engine == 'momentum':
                # Momentum: SL = sl_mult x ATR(M15), NO fixed TP; risk-based sizing (shared module)
                sl = self.momentum.initial_stop(price, current_atr, order_type)
                tp = 0.0
                if (order_type == 'BUY' and sl >= price) or (order_type == 'SELL' and sl <= price):
                    self.logger.warning("Momentum SL on wrong side of price; skipping")
                    return False
                acct = mt5.account_info()
                equity = float(acct.equity) if acct is not None else None
                risk_amt = self.momentum.risk_amount(equity)
                volume = self.momentum.lots_for_risk(
                    risk_amt, current_atr, contract_size=100.0,
                    vol_step=self.volume_step, vol_min=self.volume_min, vol_max=self.volume_max)
            else:
                # SMC / default: ATR stops from RiskManager + O1 floor + validate + fixed lot
                if order_type == 'BUY':
                    sl, tp = self.risk_manager.calculate_atr_based_stops(price, current_atr, 'BUY')
                else:
                    sl, tp = self.risk_manager.calculate_atr_based_stops(price, current_atr, 'SELL')
                if sl == 0 or tp == 0:
                    self.logger.warning("RiskManager returned invalid stops")
                    return False
                sl, tp = self._apply_stop_floor(price, sl, tp, order_type)
                if not self.risk_manager.validate_trade(order_type, price, sl, tp):
                    self.logger.warning("Risk manager validation failed")
                    return False
                volume = self._normalize_volume(self.lot_size)

            symbol_info = mt5.symbol_info(self.symbol)
            spread = symbol_info.spread * symbol_info.point if symbol_info else 0

            # ---- Resolve the fill: PAPER (simulated) vs LIVE (broker) ----
            if self.paper_mode:
                self._paper_ticket_seq += 1
                ticket = self._paper_ticket_seq
                self.logger.warning(
                    f"[PAPER] Would OPEN {order_type} {volume} {self.symbol} @ {price} "
                    f"SL {sl} TP {tp} (ticket {ticket})"
                )
                # time_server: the BROKER's clock (tick.time), not the VPS clock. Swap
                # counts SERVER midnights, and the server is not UTC (and its offset moves
                # with US DST), so a local timestamp would mis-count financing.
                _tk = mt5.symbol_info_tick(self.symbol)
                _srv = int(getattr(_tk, 'time', 0) or 0) if _tk else 0
                self.paper_positions[ticket] = {
                    'ticket': ticket, 'type': 0 if order_type == 'BUY' else 1,
                    'price_open': price, 'sl': sl, 'orig_sl': sl, 'tp': tp, 'volume': volume,
                    'magic': self.magic_number, 'symbol': self.symbol,
                    'time': int(datetime.now().timestamp()), 'profit': 0.0,
                    'time_server': _srv, 'last_check_srv': _srv,
                }
            else:
                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": self.symbol,
                    "volume": volume,
                    "type": mt5_order_type,
                    "price": price,
                    "sl": sl,
                    "tp": tp,
                    "deviation": self.order_deviation,
                    "magic": self.magic_number,
                    "comment": self.order_comment,
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": self._select_filling_mode(),
                }
                result = self._order_send_retry(request, refresh_price=True)
                if result is None:
                    self.logger.error(f"Order send failed: No result. MT5 last_error: {mt5.last_error()}")
                    return False
                if result.retcode != mt5.TRADE_RETCODE_DONE:
                    self.logger.error(f"Trade rejected: {result.retcode} - {result.comment}")
                    return False
                ticket = result.order
                self.logger.info(
                    f"Trade opened: {order_type} @ {price}, SL: {sl}, TP: {tp}, Ticket: {ticket}"
                )

            self.last_trade_time = datetime.now()
            self.last_trade_type = 'normal'

            self.stats_tracker.start_trade({
                'ticket': ticket,
                'order_type': order_type,
                'entry_price': price,
                'lot_size': volume,
                'stop_loss': sl,
                'take_profit': tp,
                'atr': current_atr,
                'spread': spread,
                'conditions_met': signal.get('conditions_met', 0),
                'conditions_detail': signal.get('conditions_detail', []),
                'confidence': signal.get('confidence', 1.0),
                'session': self.get_current_session(),
                'volatility_mode': self.current_mode
            })

            self.telegram.notify_trade_opened(
                symbol=self.symbol,
                direction=order_type,
                lot_size=volume,
                entry_price=price,
                sl_price=sl,
                tp_price=tp
            )

            self._save_state()
            return True
        except Exception as e:
            self.logger.error(f"Error opening trade: {e}")
            return False
    
    def manage_positions(self):
        """Manage positions with ATR-based break-even and trailing"""
        try:
            positions = self._get_open_positions()
            if not positions:   # None (query error) or empty -> nothing to manage
                return

            for position in positions:
                # Update statistics
                self.stats_tracker.update_trade({'current_profit': position.profit})

                # ---- Momentum engine: trailing-only exit (shared module) ----
                if self.engine == 'momentum':
                    if self.paper_mode and self._paper_check_sl_tp(position):
                        continue
                    if position.ticket not in self.tracked_positions:
                        continue
                    pos_data = self.tracked_positions[position.ticket]
                    entry_atr = pos_data.get('entry_atr', 0)
                    if entry_atr == 0:
                        entry_atr = self.calculate_atr() or 0
                        pos_data['entry_atr'] = entry_atr
                    if entry_atr > 0:
                        self._manage_momentum(position, pos_data, entry_atr)
                    continue

                # ---- SMC / default exit management ----
                # Quick scalp exit
                if self.check_quick_profit_exit(position):
                    continue

                # PAPER: simulate broker SL/TP execution (live broker does this itself)
                if self.paper_mode and self._paper_check_sl_tp(position):
                    continue

                # Get position tracking data
                if position.ticket not in self.tracked_positions:
                    continue
                
                pos_data = self.tracked_positions[position.ticket]
                entry_atr = pos_data.get('entry_atr', 0)
                
                if entry_atr == 0:
                    entry_atr = self.calculate_atr()
                    if entry_atr:
                        pos_data['entry_atr'] = entry_atr
                
                # ATR-based break-even
                if self.use_breakeven and entry_atr > 0:
                    self.apply_atr_breakeven(position, pos_data, entry_atr)
                
                # Chandelier trailing stop
                if self.use_trailing_stop and entry_atr > 0:
                    self.apply_chandelier_trailing(position, pos_data, entry_atr)
        except Exception as e:
            self.logger.error(f"Error managing positions: {e}")

    def _manage_momentum(self, position, pos_data, entry_atr):
        """Momentum exit: ratcheting ATR trailing stop only (no scalp, no breakeven),
        using the shared MomentumBreakoutStrategy. Tracks the favourable extreme from the
        current tick and moves the broker SL when the module says to."""
        try:
            tick = mt5.symbol_info_tick(self.symbol)
            if tick is None:
                return
            cur = tick.bid if position.type == 0 else tick.ask
            run_extreme = pos_data.get('run_extreme')
            if run_extreme is None:
                run_extreme = position.price_open
            run_extreme = max(run_extreme, cur) if position.type == 0 else min(run_extreme, cur)
            pos_data['run_extreme'] = run_extreme

            direction = 'BUY' if position.type == 0 else 'SELL'
            new_sl = self.momentum.update_trailing_stop(
                direction, position.price_open, position.sl, run_extreme, entry_atr)
            if abs(new_sl - position.sl) > (self.symbol_point / 2.0):
                if self.modify_position(position.ticket, new_sl, position.tp):
                    self.logger.info(f"Momentum trail: #{position.ticket} SL -> {new_sl}")
        except Exception as e:
            self.logger.error(f"Error in momentum management: {e}")
    
    def apply_atr_breakeven(self, position, pos_data, entry_atr):
        """Apply ATR-based break-even"""
        try:
            if pos_data.get('breakeven_applied', False):
                return
            
            tick = mt5.symbol_info_tick(self.symbol)
            if tick is None:
                return
            
            current_price = tick.bid if position.type == 0 else tick.ask
            entry_price = position.price_open
            
            trigger_distance = entry_atr * self.breakeven_trigger_multiple
            lock_distance = entry_atr * self.breakeven_lock_multiple
            
            if position.type == 0:  # BUY
                if current_price >= entry_price + trigger_distance:
                    new_sl = entry_price + lock_distance
                    
                    if new_sl > position.sl:
                        if self.modify_position(position.ticket, new_sl, position.tp):
                            pos_data['breakeven_applied'] = True
                            self._save_state()  # H3: persist breakeven flag
                            self.logger.info(f"Break-even activated: #{position.ticket}, New SL: {new_sl:.5f}")
                            self.telegram.notify_breakeven_activated(self.symbol, position.ticket, current_price)
            else:  # SELL
                if current_price <= entry_price - trigger_distance:
                    new_sl = entry_price - lock_distance
                    
                    if new_sl < position.sl:
                        if self.modify_position(position.ticket, new_sl, position.tp):
                            pos_data['breakeven_applied'] = True
                            self._save_state()  # H3: persist breakeven flag
                            self.logger.info(f"Break-even activated: #{position.ticket}, New SL: {new_sl:.5f}")
                            self.telegram.notify_breakeven_activated(self.symbol, position.ticket, current_price)
        except Exception as e:
            self.logger.error(f"Error applying ATR break-even: {e}")
    
    def apply_chandelier_trailing(self, position, pos_data, entry_atr):
        """Apply Chandelier trailing stop (ATR-based)"""
        try:
            tick = mt5.symbol_info_tick(self.symbol)
            if tick is None:
                return
            
            current_price = tick.bid if position.type == 0 else tick.ask
            entry_price = position.price_open
            
            activation_distance = entry_atr * self.trail_activation_multiple
            trailing_distance = entry_atr * self.trailing_atr_multiple
            
            if position.type == 0:  # BUY
                if current_price >= entry_price + activation_distance:
                    new_sl = current_price - trailing_distance
                    
                    if new_sl > position.sl and new_sl < current_price:
                        if self.modify_position(position.ticket, new_sl, position.tp):
                            self.logger.info(f"Trailing stop: #{position.ticket}, New SL: {new_sl:.5f}")
            else:  # SELL
                if current_price <= entry_price - activation_distance:
                    new_sl = current_price + trailing_distance
                    
                    if new_sl < position.sl and new_sl > current_price:
                        if self.modify_position(position.ticket, new_sl, position.tp):
                            self.logger.info(f"Trailing stop: #{position.ticket}, New SL: {new_sl:.5f}")
        except Exception as e:
            self.logger.error(f"Error applying Chandelier trailing: {e}")
    
    def modify_position(self, ticket, new_sl, new_tp):
        """Modify position SL/TP.

        v5.0.0 (H1): round prices to digits, check the retcode, LOG last_error/retcode
        on failure (was silent), and retry via the bounded helper. Simulated in paper mode.
        """
        try:
            new_sl = self._round_price(new_sl)
            new_tp = self._round_price(new_tp)

            if self.paper_mode:
                pos = self.paper_positions.get(ticket)
                if pos is not None:
                    pos['sl'] = new_sl
                    pos['tp'] = new_tp
                self.logger.warning(f"[PAPER] Would MODIFY #{ticket} -> SL {new_sl} TP {new_tp}")
                return True

            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": self.symbol,
                "position": ticket,
                "sl": new_sl,
                "tp": new_tp,
            }
            result = self._order_send_retry(request, refresh_price=False)
            if result is None:
                self.logger.error(f"Modify #{ticket} failed: no result. last_error: {mt5.last_error()}")
                return False
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                self.logger.error(f"Modify #{ticket} rejected: {result.retcode} - {result.comment}")
                return False
            return True
        except Exception as e:
            self.logger.error(f"Error modifying position: {e}")
            return False
    
    def ensure_news_data_fresh(self):
        """
        Ensure news data is fresh - fetch if needed
        Called every loop iteration regardless of trading/pause status
        This keeps /news command data fresh even when bot is paused
        """
        try:
            # Check if news filter needs to fetch
            # This is a lightweight check - only fetches if cache is stale
            if self.news_filter.last_fetch is None:
                # First time - try cache first, fetch if needed
                if not self.news_filter.load_cached_events():
                    self.news_filter.fetch_news()
                    self.logger.info(f"Initial news fetch: {len(self.news_filter.events)} events found")
            else:
                # Check if cache is stale (every 5 minutes by default from config)
                time_since_fetch = (datetime.now() - self.news_filter.last_fetch).total_seconds()
                if time_since_fetch > self.news_filter.check_interval:
                    self.news_filter.fetch_news()
                    self.logger.debug(f"News cache refreshed: {len(self.news_filter.events)} events")
        except Exception as e:
            self.logger.error(f"Error ensuring news data fresh: {e}")
    
    def run(self):
        """Main bot loop"""
        last_status_message = ""
        status_message_counter = 0
        consecutive_errors = 0   # v5.0.0 (C3)

        try:
            while self.running:
                # v5.0.0 (C3): every iteration is wrapped so a transient error logs and
                # CONTINUES instead of killing the bot. A run of errors backs off and alerts.
                try:
                    # v5.0.0 (M11): a manual stop flag means a graceful, clean shutdown.
                    if self._manual_stop_requested():
                        self.logger.info("Manual stop flag detected - shutting down gracefully")
                        break

                    # Rotate to a new daily log file after local midnight if needed
                    self.rotate_log_file_if_needed()

                    # v5.0.0 (C1): ensure we have a live, logged-in MT5 session.
                    if not self.ensure_connection():
                        self.logger.error("MT5 still disconnected after retries; pausing this cycle")
                        time.sleep(30)
                        consecutive_errors = 0  # not a code error; don't escalate
                        continue

                    is_open, status_msg = self.is_within_trading_hours()

                    if not is_open:
                        if status_msg != last_status_message:
                            self.logger.warning(f"[CLOSED] {status_msg}")
                            last_status_message = status_msg
                            status_message_counter = 0
                        else:
                            status_message_counter += 1
                            if status_message_counter % 5 == 0:
                                self.logger.info(f"[HEARTBEAT] Bot alive | {status_msg}")

                        # The trading-hours gate blocks NEW ENTRIES ONLY. An open position
                        # must still be tracked, trailed and stopped out while the gate is
                        # shut -- previously this branch 'continue'd straight past
                        # management, so a position held into a closed window ran with its
                        # trailing stop frozen and (in paper) its SL/TP never simulated.
                        # This is a LIVE bug too, not just a paper one.
                        self.update_tracked_positions()
                        held = self._get_open_positions()
                        if held:
                            self.logger.info(
                                f"[CLOSED] Managing {len(held)} open position(s) "
                                "(entries blocked, management continues)")
                            self.manage_positions()

                        time.sleep(60)
                        consecutive_errors = 0
                        continue

                    if last_status_message != status_msg:
                        self.logger.info(f"[OPEN] {status_msg}")
                        last_status_message = status_msg

                    # Check if weekly news summary should be sent
                    self.check_and_send_weekly_news_summary()

                    # Ensure news data is fresh regardless of pause status
                    self.ensure_news_data_fresh()

                    # Check news avoidance status. do this BEFORE checking daily target
                    avoiding_news, news_event = self.news_filter.should_avoid_trading()
                    if avoiding_news:
                        event_key = f"{news_event['title']}_{news_event['time']}"
                        if event_key not in self.alerted_news_events:
                            self.telegram.notify_news_avoidance(news_event)
                            self.alerted_news_events.add(event_key)
                            self.logger.info(f"News avoidance alert sent: {news_event['title']}")
                        self.logger.info(f"[NEWS FILTER] Avoiding trading: {news_event['title']}")

                    # v5.0.0 (M9): single authoritative daily check per iteration
                    daily_paused = self.check_daily_profit()
                    weekly_paused = self.check_weekly_limits()

                    # Check swap or rollover avoidance window (server time)
                    in_swap_window, swap_msg = self.is_in_swap_avoidance_window()
                    if in_swap_window:
                        self.logger.info(f"[SWAP] Avoiding new trades: {swap_msg}")

                    # Update ATR and mode
                    self.update_trading_mode()

                    # Extreme ATR filter. skip new trades if volatility is insane
                    extreme_volatility = False
                    if (
                        getattr(self, "skip_on_extreme_atr", False)
                        and self.current_atr is not None
                        and self.atr_max_for_trading is not None
                    ):
                        if self.current_atr > self.atr_max_for_trading:
                            extreme_volatility = True
                            self.logger.info(
                                f"[VOL] ATR {self.current_atr:.4f} above max {self.atr_max_for_trading:.4f}. "
                                "Skipping new trades due to extreme volatility"
                            )

                    # v5.0.0 (M8): only log the mode line when it CHANGES
                    if self.volatility_enabled and self.current_atr:
                        if self.current_mode != self._last_logged_mode:
                            mode_label = "[SCALP]" if self.current_mode == 'scalp' else "[NORMAL]"
                            self.logger.info(
                                f"{mode_label} Mode: {self.current_mode.upper()} | ATR: {self.current_atr:.4f}")
                            self._last_logged_mode = self.current_mode

                    self.update_tracked_positions()

                    # v5.0.0 (C1): never treat a None positions result as 'flat'
                    bot_positions = self._get_open_positions()
                    if bot_positions is None:
                        self.logger.warning("Position query failed this cycle; skipping")
                        time.sleep(self.active_loop_interval)
                        consecutive_errors = 0
                        continue
                    position_count = len(bot_positions)

                    # v5.0.0 (M8): throttle the repeated scanning/paused log line
                    now_ts = time.time()
                    if (position_count != self._last_pos_count
                            or (now_ts - self._last_scan_log_ts) >= self.waiting_log_interval):
                        if daily_paused:
                            self.logger.info(f"[PAUSED] Daily limit active | Managing {position_count} position(s)")
                        else:
                            self.logger.info(f"Scanning market... (Positions: {position_count})")
                        self._last_scan_log_ts = now_ts
                        self._last_pos_count = position_count

                    if position_count > 0:
                        self.manage_positions()

                    # Look for new trades only when not paused by any gate
                    if (
                        self._can_open_additional_position(bot_positions)
                        and not daily_paused
                        and not weekly_paused
                        and not avoiding_news
                        and not in_swap_window
                        and not extreme_volatility
                    ):
                        in_cooldown, remaining = self.is_in_cooldown()
                        if in_cooldown:
                            self.logger.info(f"In cooldown: {remaining}s remaining")
                        elif self.risk_manager.can_trade():
                            order_execution = self.config['TRADING'].get('order_execution', {})
                            bars_to_fetch = int(order_execution.get('market_data_bars', 250))

                            signal = self._generate_signal(bars_to_fetch)
                            if signal:
                                self.logger.info(
                                    f"Trade signal: {signal['type']} | engine={self.engine}")
                                self.open_trade(signal)

                    # Loop cadence
                    if daily_paused:
                        sleep_interval = self.paused_loop_interval
                    else:
                        sleep_interval = self.active_loop_interval if position_count > 0 else self.main_loop_interval
                    time.sleep(sleep_interval)
                    consecutive_errors = 0   # healthy iteration

                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    # v5.0.0 (C3): log and keep running; back off and alert if it persists
                    consecutive_errors += 1
                    self.logger.error(f"Error in main loop (#{consecutive_errors}): {e}", exc_info=True)
                    if consecutive_errors in (3, 10, 30):
                        try:
                            self.telegram.notify_error(self.symbol, "Main loop error",
                                                       f"{consecutive_errors} consecutive errors: {e}")
                        except Exception:
                            pass
                    time.sleep(min(60, 5 * consecutive_errors))

        except KeyboardInterrupt:
            self.logger.info("Bot stopped by user")
        except Exception as e:
            self.logger.error(f"Fatal error in main loop: {e}")
        finally:
            self.shutdown()
    
    def check_and_send_weekly_news_summary(self):
        """Send weekly news summary on Sunday 10pm GMT (before market open)"""
        if not self.weekly_summary_enabled:
            return
            
        try:
            now_gmt = datetime.utcnow()
            current_weekday = now_gmt.weekday()  # 0=Monday, 6=Sunday
            
            # Check if it's Sunday at 10pm and not sent this week
            if (current_weekday == self.weekly_summary_day and
                now_gmt.hour == self.weekly_summary_hour and
                self.last_weekly_summary_date != now_gmt.date()):
                
                self.logger.info("Sending weekly news summary...")
                
                # Calculate hours until Friday 22:00 (10pm)
                # Sunday 22:00 + 5 days = Friday 22:00
                friday_night = now_gmt.replace(hour=22, minute=0, second=0, microsecond=0) + timedelta(days=5)
                hours_ahead = (friday_night - now_gmt).total_seconds() / 3600
                
                self.logger.info(f"Looking ahead {hours_ahead:.1f} hours (until Friday 22:00)")
                
                # Get events for the week
                upcoming = self.news_filter.get_upcoming_events(hours_ahead=int(hours_ahead))
                
                # Send via telegram
                self.telegram.send_weekly_news_summary(upcoming)
                
                # Mark as sent
                self.last_weekly_summary_date = now_gmt.date()
                self.logger.info("Weekly news summary sent successfully")
            
        except Exception as e:
            self.logger.error(f"Error sending weekly news summary: {e}")

    def shutdown(self):
        """Cleanup on shutdown"""
        self.logger.info("Shutting down bot...")
        self.running = False

        self.stats_tracker.save_stats()
        self._save_state()   # v5.0.0 (H3): persist cooldown + per-position state
        self.remove_status_file()

        mt5.shutdown()
        self.logger.info("Bot shut down complete")

        self.telegram.notify_shutdown(self.symbol)


if __name__ == "__main__":
    # v5.0.0: accept "--paper" anywhere; config file is the first positional arg.
    args = sys.argv[1:]
    paper = '--paper' in args
    positional = [a for a in args if not a.startswith('--')]

    if not positional:
        print("Usage: python main_bot.py <config_file> [--paper]")
        sys.exit(1)

    config_file = positional[0]

    if not Path(config_file).exists():
        print(f"Config file not found: {config_file}")
        sys.exit(1)

    bot = FusionSniperBot(config_file, paper_mode=paper)
    bot.run()
