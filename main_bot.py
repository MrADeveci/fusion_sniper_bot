"""
Fusion Sniper Bot - v4.0
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

import MetaTrader5 as mt5
import numpy as np


# Import local modules
from modules.strategy import FusionStrategy
from modules.risk_manager import RiskManager
from modules.news_filter import EconomicNewsFilter
from modules.telegram_notifier import TelegramNotifier
from modules.trade_statistics import TradeStatistics


class FusionSniperBot:
    """Main trading bot class with ATR-based position management"""
    
    def __init__(self, config_file):
        """Initialize bot with configuration"""
        self.config_file = config_file
        self.config = self.load_config(config_file)
        
        # Validate config before proceeding
        self.validate_config()
        
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
        self.stats_tracker = TradeStatistics(self.config)
        
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
        self.timeframe = getattr(mt5, f"TIMEFRAME_{self.config['TRADING']['timeframe']}")
        self.running = True
        
        # Trading parameters
        self.lot_size = self.config['TRADING']['lot_size']
        self.max_positions = self.config['TRADING'].get('max_positions', 1)
        self.use_atr_stops = self.config['TRADING'].get('use_atr_based_stops', True)
        
        # V3.0: ATR-based break-even and trailing stop
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
        self.last_trade_time = None
        self.last_trade_type = None
        self.trade_cooldown = self.config['TRADING'].get('trade_cooldown_seconds', 60)
        
        # Trading hours from config
        trading_hours = self.config['TRADING'].get('trading_hours', {})
        self.saturday_closed = trading_hours.get('saturday_closed', True)
        self.sunday_closed = trading_hours.get('sunday_closed', False)
        self.monday_open_hour = trading_hours.get('monday_open_hour', 0)
        self.sunday_open_hour = trading_hours.get('sunday_open_hour', 22)
        self.friday_close_hour = trading_hours.get('friday_close_hour', 22)
        
        # Daily profit tracking (PAUSE mode instead of shutdown)
        self.daily_profit_target = self.config['TRADING'].get('daily_profit_target', 0)
        self.daily_target_reached = False
        self.last_target_check_date = datetime.now().date()
        self.starting_equity_today = None  # NEW: Track starting equity for loss limit

        # Loop timing from config
        system_cfg = self.config.get('SYSTEM', {})
        self.main_loop_interval = system_cfg.get('main_loop_interval', 10)
        self.paused_loop_interval = system_cfg.get('paused_loop_interval', 30)
        
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
            # Fallback . safe default if symbol_info is not available
            self.pip_size = 0.0001
        
        # Logging
        self.logger.info("="*60)
        self.logger.info(f"Fusion Sniper Bot v3.0 (News Fetch Fix)")
        self.logger.info(f"Symbol: {self.symbol}")
        self.logger.info("="*60)
        self.logger.info(f"Magic number: {self.magic_number}")
        self.logger.info(f"Lot size: {self.lot_size}")
        self.logger.info(f"Max Concurrent Positions: {self.max_positions}")
        self.logger.info(f"Stop/TP Mode: ATR-Based (Dynamic)")
        self.logger.info(f"  SL Multiplier: {self.config['TRADING'].get('stop_loss_atr_multiple', 1.0)}x ATR")
        self.logger.info(f"  TP Multiplier: {self.config['TRADING'].get('take_profit_atr_multiple', 2.0)}x ATR")
        
        # Log break-even settings
        if self.use_breakeven:
            self.logger.info(f"Break-Even: ENABLED (ATR-based)")
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
        
        self.logger.info(f"News Filter: ENABLED (ForexFactory XML format)")
        self.logger.info(f"News Fetch: Continuous (even when paused)")
        
        self.telegram.notify_bot_started(self.symbol)
        
        # Write status file for remote control
        self.write_status_file()
    
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
    
    def calculate_atr(self):
        """Calculate Average True Range"""
        if not self.volatility_enabled:
            period = 14
        else:
            period = self.atr_period
        
        try:
            rates = mt5.copy_rates_from_pos(self.symbol, self.timeframe, 0, period + 1)
            
            if rates is None or len(rates) < period + 1:
                return None
            
            high = np.array([r['high'] for r in rates])
            low = np.array([r['low'] for r in rates])
            close = np.array([r['close'] for r in rates])
            
            tr_list = []
            for i in range(1, len(rates)):
                hl = high[i] - low[i]
                hc = abs(high[i] - close[i-1])
                lc = abs(low[i] - close[i-1])
                tr = max(hl, hc, lc)
                tr_list.append(tr)
            
            atr = np.mean(tr_list[-period:])
            return atr
            
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
                close_type = mt5.ORDER_TYPE_SELL if position.type == 0 else mt5.ORDER_TYPE_BUY
                
                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": position.symbol,
                    "volume": position.volume,
                    "type": close_type,
                    "position": position.ticket,
                    "price": close_price,
                    "deviation": 20,
                    "magic": self.magic_number,
                    "comment": "scalp_quick_profit",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                
                result = mt5.order_send(request)
                
                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                    self.last_trade_time = datetime.now()
                    self.last_trade_type = 'scalp'
                    self.logger.info(f"Position closed | Ticket: {position.ticket}")
                    return True
                    
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
        
        # Friday after closing hour
        if weekday == 4 and hour >= self.friday_close_hour:
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
    
    def get_market_data(self, bars=100):
        """Get market data"""
        try:
            rates = mt5.copy_rates_from_pos(self.symbol, self.timeframe, 0, bars)
            if rates is None or len(rates) == 0:
                return None
                
            return rates
        except Exception as e:
            self.logger.error(f"Error getting market data: {e}")
            return None
    
    def check_daily_profit(self):
        """Check daily profit and update pause status - TIMEZONE AWARE + SWAP INCLUDED"""
        if self.daily_profit_target <= 0:
            return False
        
        try:
            # Get timezone offset from config (default 0 if not specified)
            broker_timezone_offset = self.config.get('BROKER', {}).get('broker_timezone_offset', 0)
            
            # Check if new day - reset pause flag (using LOCAL time for date check)
            current_date = datetime.now().date()
            if current_date != self.last_target_check_date:
                self.daily_target_reached = False
                self.starting_equity_today = None  # NEW: Reset starting equity tracker
                self.last_target_check_date = current_date
                self.logger.info(f"NEW DAY - Daily profit target reset (Local timezone)")
                if broker_timezone_offset != 0:
                    self.logger.info(f"Timezone offset: Broker is GMT+{broker_timezone_offset}, queries adjusted accordingly")
            
            # If already reached today, stay paused
            if self.daily_target_reached:
                return True
            
            # Calculate today's profit - TIMEZONE ADJUSTED
            # Local midnight + offset = broker midnight
            # Example: UK 00:00 + 2hrs = Server 02:00 (start of UK trading day in server time)
            local_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            broker_today_start = local_midnight + timedelta(hours=broker_timezone_offset)
            broker_today_end = broker_today_start + timedelta(days=1) - timedelta(seconds=1)
            broker_now = min(datetime.now() + timedelta(hours=broker_timezone_offset), broker_today_end)
                        
            # Query MT5 with broker-adjusted times
            deals = mt5.history_deals_get(broker_today_start, broker_now)
            
            if deals is None:
                self.logger.debug(f"  No deals returned from MT5")
                return False
            
            # Calculate NET profit (only count exit deals to avoid double-counting)
            # FIXED: Now includes commission and swap from MT5 deal fields
            total_profit = 0.0
            total_commission = 0.0
            total_swap = 0.0
            trade_count = 0
            swap_count = 0
            
            # Count ALL deals for commission, but only EXIT deals for profit
            for deal in deals:
                if deal.magic == self.magic_number:
                    # Count commission from ALL deals (entry + exit)
                    total_commission += abs(deal.commission)
                    
                    # Only count exit deals for profit/swap/trade count
                    if deal.entry == mt5.DEAL_ENTRY_OUT:
                        total_profit += deal.profit
                        total_swap += deal.swap
                        trade_count += 1
                        if deal.swap != 0:
                            swap_count += 1
            
            # NET profit = Gross profit - Commission + Swap
            net_profit = total_profit - total_commission + total_swap
                       
            # --- BEGIN: DAILY LOSS LIMIT ENFORCEMENT ---
            # Read configured daily loss (assume positive number in config)
            max_daily_loss_cfg = self.config.get('RISK', {}).get('max_daily_loss', 0)
            max_daily_loss_currency = self.config.get('RISK', {}).get('max_daily_loss_currency', 'GBP')  # optional override

            if max_daily_loss_cfg and max_daily_loss_cfg > 0:
                # Determine account currency
                try:
                    acct = mt5.account_info()
                    account_currency = getattr(acct, 'currency', None) or self.config.get('BROKER', {}).get('account_currency')
                except Exception:
                    account_currency = self.config.get('BROKER', {}).get('account_currency')

                # If config currency differs from account currency, try to convert using market rates
                max_daily_loss_account_ccy = float(max_daily_loss_cfg)
                if account_currency and account_currency.upper() != max_daily_loss_currency.upper():
                    # attempt to find a FX symbol to convert from config currency -> account currency
                    src = max_daily_loss_currency.upper()
                    dst = account_currency.upper()

                    # common symbol forms to try
                    possible_pairs = [f"{src}{dst}", f"{dst}{src}"]
                    rate = None
                    for pair in possible_pairs:
                        try:
                            tick = mt5.symbol_info_tick(pair)
                            if tick is not None:
                                # if symbol is DST/SRC inverted, invert rate accordingly
                                if pair == f"{src}{dst}":
                                    rate = (tick.ask + tick.bid) / 2.0
                                    max_daily_loss_account_ccy = max_daily_loss_account_ccy * rate
                                else:
                                    rate = (tick.ask + tick.bid) / 2.0
                                    # pair is DST+SRC, so convert by 1/rate
                                    max_daily_loss_account_ccy = max_daily_loss_account_ccy / rate
                                break
                        except Exception:
                            continue

                    # if conversion failed, log and treat config value as account currency for safety
                    if rate is None:
                        self.logger.warning(f"Failed to auto-convert {max_daily_loss_cfg} {src} to {dst}. Treating limit as {dst}.")
                        # no conversion applied - proceed with raw number

                # At this point max_daily_loss_account_ccy is the numeric cap expressed in account currency
                try:
                    cap = float(max_daily_loss_account_ccy)
                except Exception:
                    cap = float(max_daily_loss_cfg)

                # CHECK 1 - Closed-deals net profit (existing behavior)
                if net_profit <= -cap:
                    self.daily_target_reached = True
                    self.logger.info("="*60)
                    self.logger.info(f"DAILY LOSS LIMIT REACHED: -{max_daily_loss_cfg:.2f} {max_daily_loss_currency} (approx {cap:.2f} {account_currency})")
                    self.logger.info("Bot will PAUSE new trades until midnight (Local). Existing positions will be managed.")
                    self.logger.info("="*60)
                    try:
                        self.telegram.notify_daily_loss_limit(self.symbol, net_profit, max_daily_loss_cfg)
                    except Exception:
                        pass
                    return True

                # CHECK 2 - Optional: equity drawdown based (real-time)
                if self.config.get('RISK', {}).get('loss_limit_by_equity', True):
                    try:
                        account_info = mt5.account_info()
                        current_equity = float(account_info.equity)
                        # track starting equity for the day in state file if not present
                        starting_equity = getattr(self, 'starting_equity_today', None)
                        if starting_equity is None:
                            self.starting_equity_today = float(account_info.balance)
                            starting_equity = self.starting_equity_today

                        drawdown = starting_equity - current_equity
                        if drawdown >= cap:
                            self.daily_target_reached = True
                            self.logger.info("="*60)
                            self.logger.info(f"DAILY LOSS BY EQUITY REACHED: drawdown {drawdown:.2f} {account_currency} >= {cap:.2f}")
                            self.logger.info("Bot will PAUSE new trades until midnight (Local).")
                            self.logger.info("="*60)
                            try:
                                self.telegram.notify_daily_loss_limit(self.symbol, -drawdown, max_daily_loss_cfg)
                            except Exception:
                                pass
                            return True
                    except Exception as e:
                        self.logger.debug(f"Equity-based loss check failed: {e}")
            # --- END: DAILY LOSS LIMIT ENFORCEMENT ---
            
            if net_profit >= self.daily_profit_target:
                self.daily_target_reached = True  # Set pause flag
                self.logger.info(f"="*60)
                self.logger.info(f"DAILY PROFIT TARGET REACHED: £{net_profit:.2f}")
                self.logger.info(f"Bot will PAUSE new trades until midnight (00:00 Local)")
                self.logger.info(f"Existing positions will still be managed")
                self.logger.info(f"="*60)
                self.telegram.notify_daily_target_reached(self.symbol, net_profit, self.daily_profit_target)
                return True
            
            return False
        except Exception as e:
            self.logger.error(f"Error checking daily profit: {e}")
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
        """Update tracked positions"""
        try:
            current_positions = mt5.positions_get(symbol=self.symbol)
            if current_positions is None:
                current_positions = []
            
            current_positions = [p for p in current_positions if p.magic == self.magic_number]
            current_tickets = set([p.ticket for p in current_positions])
            
            tracked_tickets = set(self.tracked_positions.keys())
            closed_tickets = tracked_tickets - current_tickets
            
            for ticket in closed_tickets:
                self.handle_position_closure(ticket)
                del self.tracked_positions[ticket]
            
            for position in current_positions:
                if position.ticket not in self.tracked_positions:
                    entry_atr = self.calculate_atr()
                    if entry_atr is None:
                        entry_atr = 0
                    
                    self.tracked_positions[position.ticket] = {
                        'entry': position.price_open,
                        'sl': position.sl,
                        'tp': position.tp,
                        'type': position.type,
                        'volume': position.volume,
                        'open_time': position.time,
                        'entry_atr': entry_atr,
                        'breakeven_applied': False,
                    }
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
                if deal.entry == 1:
                    exit_deal = deal
            
            if exit_deal is None:
                return
            
            profit = exit_deal.profit
            direction = "BUY" if pos_data['type'] == 0 else "SELL"
            exit_price = exit_deal.price
            entry_price = pos_data['entry']
            
            if exit_deal.comment == "scalp_quick_profit":
                reason = f"Quick scalp profit"
            else:
                reason = self.determine_close_reason(exit_price, pos_data['sl'], pos_data['tp'], direction)
            
            self.telegram.notify_trade_closed(
                symbol=self.symbol,
                direction=direction,
                lot_size=pos_data['volume'],
                entry_price=entry_price,
                exit_price=exit_price,
                profit=profit,
                reason=reason
            )
            
            profit_pips = abs(exit_price - entry_price) / self.pip_size
            if (direction == "SELL" and profit < 0) or (direction == "BUY" and profit < 0):
                profit_pips = -profit_pips
            
            expected_exit = pos_data['tp'] if profit > 0 else pos_data['sl']
            
            self.stats_tracker.end_trade({
                'exit_price': exit_price,
                'exit_reason': reason,
                'profit': profit,
                'profit_pips': profit_pips,
                'expected_exit': expected_exit if expected_exit > 0 else exit_price
            })
            
            self.logger.info(f"Position closed: #{ticket}, {direction}, £{profit:.2f}, {reason}")
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
            
            # Calculate ATR-based stops
            if order_type == 'BUY':
                sl, tp = self.risk_manager.calculate_atr_based_stops(price, current_atr, 'BUY')
                mt5_order_type = mt5.ORDER_TYPE_BUY
            else:
                sl, tp = self.risk_manager.calculate_atr_based_stops(price, current_atr, 'SELL')
                mt5_order_type = mt5.ORDER_TYPE_SELL
            
            if sl == 0 or tp == 0:
                self.logger.warning("RiskManager returned invalid stops")
                return False
            
            # Broker minimum distance validation
            symbol_info = mt5.symbol_info(self.symbol)
            if symbol_info is None:
                self.logger.error("Failed to get symbol info")
                return False
            
            min_distance_points = symbol_info.trade_stops_level
            min_distance_price = min_distance_points * symbol_info.point
            
            sl_distance = abs(price - sl)
            tp_distance = abs(tp - price)
            
            if sl_distance < min_distance_price:
                if order_type == 'BUY':
                    sl = price - min_distance_price
                    tp = price + (min_distance_price * 2.0)
                else:
                    sl = price + min_distance_price
                    tp = price - (min_distance_price * 2.0)
            
            if tp_distance < min_distance_price:
                if order_type == 'BUY':
                    tp = price + max(tp_distance, min_distance_price)
                else:
                    tp = price - max(tp_distance, min_distance_price)
            
            # Validate with risk manager
            if not self.risk_manager.validate_trade(order_type, price, sl, tp):
                self.logger.warning("Risk manager validation failed")
                return False
            
            # Send order
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": self.symbol,
                "volume": self.lot_size,
                "type": mt5_order_type,
                "price": price,
                "sl": sl,
                "tp": tp,
                "deviation": 10,
                "magic": self.magic_number,
                "comment": "fusion_sniper_bot_v4",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            
            result = mt5.order_send(request)

            if result is None:
                # Log the underlying MT5 error for diagnosis
                last_error = mt5.last_error()
                self.logger.error(f"Order send failed: No result. MT5 last_error: {last_error}")
                return False

            if result.retcode != mt5.TRADE_RETCODE_DONE:
                self.logger.error(f"Trade rejected: {result.retcode} - {result.comment}")
                return False
            
            # Trade successful
            self.logger.info(f"Trade opened: {order_type} @ {price:.5f}, SL: {sl:.5f}, TP: {tp:.5f}, Ticket: {result.order}")
            
            self.last_trade_time = datetime.now()
            self.last_trade_type = 'normal'
            
            symbol_info = mt5.symbol_info(self.symbol)
            spread = symbol_info.spread * symbol_info.point if symbol_info else 0
            
            self.stats_tracker.start_trade({
                'ticket': result.order,
                'order_type': order_type,
                'entry_price': price,
                'lot_size': self.lot_size,
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
                lot_size=self.lot_size,
                entry_price=price,
                sl_price=sl,
                tp_price=tp
            )
            
            return True
        except Exception as e:
            self.logger.error(f"Error opening trade: {e}")
            return False
    
    def manage_positions(self):
        """Manage positions with ATR-based break-even and trailing"""
        try:
            positions = mt5.positions_get(symbol=self.symbol)
            if positions is None or len(positions) == 0:
                return
            
            for position in positions:
                if position.magic != self.magic_number:
                    continue
                
                # Update statistics
                self.stats_tracker.update_trade({'current_profit': position.profit})
                
                # Quick scalp exit
                if self.check_quick_profit_exit(position):
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
                            self.logger.info(f"Break-even activated: #{position.ticket}, New SL: {new_sl:.5f}")
                            self.telegram.notify_breakeven_activated(self.symbol, position.ticket, current_price)
            else:  # SELL
                if current_price <= entry_price - trigger_distance:
                    new_sl = entry_price - lock_distance
                    
                    if new_sl < position.sl:
                        if self.modify_position(position.ticket, new_sl, position.tp):
                            pos_data['breakeven_applied'] = True
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
        """Modify position SL/TP"""
        try:
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": self.symbol,
                "position": ticket,
                "sl": new_sl,
                "tp": new_tp
            }
            
            result = mt5.order_send(request)
            return result and result.retcode == mt5.TRADE_RETCODE_DONE
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
        
        try:
            while self.running:
                # Rotate to a new daily log file after local midnight if needed
                self.rotate_log_file_if_needed()

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
                    
                    time.sleep(60)
                    continue
                
                if last_status_message != status_msg:
                    self.logger.info(f"[OPEN] {status_msg}")
                    last_status_message = status_msg

                # Check if weekly news summary should be sent
                self.check_and_send_weekly_news_summary()
                
                # Ensure news data is fresh regardless of pause status
                # This keeps /news command data up to date even when bot is paused
                self.ensure_news_data_fresh()
                
                # Check news avoidance status. do this BEFORE checking daily target
                # This ensures we log news avoidance even when paused for daily profit
                avoiding_news, news_event = self.news_filter.should_avoid_trading()
                if avoiding_news:
                    event_key = f"{news_event['title']}_{news_event['time']}"
                    
                    # Send alert once per event
                    if event_key not in self.alerted_news_events:
                        self.telegram.notify_news_avoidance(news_event)
                        self.alerted_news_events.add(event_key)
                        self.logger.info(f"News avoidance alert sent: {news_event['title']}")
                    
                    # Log that we are avoiding (visible even when paused for daily target)
                    self.logger.info(f"[NEWS FILTER] Avoiding trading: {news_event['title']}")
                
                target_reached = self.check_daily_profit()

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
               
                if self.volatility_enabled and self.current_atr:
                    mode_label = "[SCALP]" if self.current_mode == 'scalp' else "[NORMAL]"
                    self.logger.info(f"{mode_label} Mode: {self.current_mode.upper()} | ATR: {self.current_atr:.4f}")
                
                self.update_tracked_positions()
                
                positions = mt5.positions_get(symbol=self.symbol)
                if positions is None:
                    positions = []
                
                position_count = len([p for p in positions if p.magic == self.magic_number])
                
                if target_reached:
                    self.logger.info(f"[PAUSED] Daily target reached | Managing {position_count} position(s)")
                else:
                    self.logger.info(f"Scanning market... (Positions: {position_count})")
                
                if position_count > 0:
                    self.manage_positions()
                
                # Look for new trades. only when not paused by daily target, news, swap window, or extreme ATR
                if (
                    position_count < self.max_positions
                    and not target_reached
                    and not avoiding_news
                    and not in_swap_window
                    and not extreme_volatility
                ):
                    in_cooldown, remaining = self.is_in_cooldown()
                    if in_cooldown:
                        self.logger.info(f"In cooldown: {remaining}s remaining")
                    else:
                        # News already checked above, now just check risk manager
                        if self.risk_manager.can_trade():
                            # Read market_data_bars from config
                            order_execution = self.config['TRADING'].get('order_execution', {})
                            bars_to_fetch = order_execution.get('market_data_bars', 100)
                            rates = self.get_market_data(bars=bars_to_fetch)
                            
                            if rates is not None:
                                signal = self.strategy.analyze_from_rates(rates)
                                
                                if signal:
                                    self.logger.info(f"Trade signal: {signal['type']}")
                                    self.open_trade(signal)
                
                # Faster loop while trading. slower loop when paused for the day
                sleep_interval = self.paused_loop_interval if target_reached else self.main_loop_interval
                time.sleep(sleep_interval)
        
        except KeyboardInterrupt:
            self.logger.info("Bot stopped by user")
        except Exception as e:
            self.logger.error(f"Error in main loop: {e}")
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
        self.remove_status_file()
        
        mt5.shutdown()
        self.logger.info("Bot shut down complete")
        
        self.telegram.notify_shutdown(self.symbol)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python main_bot.py <config_file>")
        sys.exit(1)
    
    config_file = sys.argv[1]
    
    if not Path(config_file).exists():
        print(f"Config file not found: {config_file}")
        sys.exit(1)
    
    bot = FusionSniperBot(config_file)
    bot.run()
    