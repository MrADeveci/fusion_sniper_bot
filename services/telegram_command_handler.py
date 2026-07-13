#!/usr/bin/env python3
"""
Telegram Command Handler - Fusion Sniper Bot (v5.0.0)
"""

import os
import sys
import json
import time
import logging
import requests
import subprocess
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any
import MetaTrader5 as mt5

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.atomic_json import write_json_atomic                        # noqa: E402
from modules.liveness import (check_liveness, lock_path, redact_token,   # noqa: E402
                              STOPPED, ALIVE, HUNG)

# ============================================================================
# WINDOWS CONSOLE UTF-8 ENCODING FIX
# ============================================================================
if sys.platform == 'win32':
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleCP(65001)
        kernel32.SetConsoleOutputCP(65001)
    except:
        pass
    
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ============================================================================
# EMOJI REPLACEMENT MAP FOR CONSOLE LOGGING
# ============================================================================
EMOJI_MAP = {
    '🤖': '[BOT]', '💰': '[PROFIT]', '📊': '[STATS]', '📈': '[UP]',
    '📉': '[DOWN]', '⚠️': '[WARN]', '✅': '[OK]', '❌': '[FAIL]',
    '🔴': '[RED]', '🟢': '[GREEN]', '🟡': '[YELLOW]', '⏰': '[TIME]',
    '📍': '[PIN]', '🎯': '[TARGET]', '🔄': '[REFRESH]', '💹': '[CHART]',
    '📝': '[NOTE]', '⚡': '[ALERT]', '🚀': '[ROCKET]', '🛑': '[STOP]',
    '📢': '[NEWS]', '🔔': '[BELL]', '🔍': '[SEARCH]',
}

def clean_emoji_for_console(text):
    """Remove/replace emojis for Windows console logging"""
    if not text:
        return text
    cleaned = text
    for emoji, replacement in EMOJI_MAP.items():
        cleaned = cleaned.replace(emoji, replacement)
    try:
        cleaned = cleaned.encode('ascii', 'ignore').decode('ascii')
    except:
        pass
    return cleaned

# ============================================================================
# LOGGING SETUP - USES CONFIG PATH
# ============================================================================
# Note: Logging will be reconfigured in __init__ with config values

class TelegramCommandHandler:
    """Handles incoming Telegram commands for Fusion Sniper bot"""
    
    def __init__(self, config_file='config.json'):
        """Initialize handler with bot config"""
        self.config_file = config_file
        self.config = self.load_config()
        
        # Get symbol FIRST (needed for logging setup)
        self.symbol = self.config['BROKER']['symbol']
        
        # Load handler config section
        handler_config = self.config.get('TELEGRAM_HANDLER', {})
        
        # Setup logging with config path (uses self.symbol)
        self.setup_logging(handler_config)
        
        # Get remaining credentials from config
        self.magic_number = self.config['BROKER']['magic_number']
        self.bot_token = self.config['TELEGRAM']['bot_token']
        self.chat_id = self.config['TELEGRAM']['chat_id']
        # Persisted, so a restart does not replay every queued command (see _load_offset).
        # bot_dir is set further down, so resolve it here for the offset path.
        self.bot_dir = os.path.dirname(os.path.abspath(self.config_file))
        self.last_update_id = self._load_offset()
        
        # Pre-read nested config objects (v5.0.0 M6: use the keys the config actually has)
        paths_cfg = handler_config.get('paths', {})
        health_cfg = handler_config.get('health_thresholds', {})
        display_cfg = handler_config.get('display', {})

        # API timeouts from config (M6: config key is 'api_timeout_seconds')
        telegram_config = self.config.get('TELEGRAM', {})
        self.api_timeout = telegram_config.get('api_timeout_seconds',
                                               telegram_config.get('api_timeout', 10))
        self.long_poll_timeout = handler_config.get('long_poll_timeout', 30)
        self.long_poll_request_timeout = handler_config.get('long_poll_request_timeout', 35)
        
        # Load authorized user IDs for command access control
        self.authorized_user_ids = telegram_config.get('authorized_user_ids', [])
        self.logger.info(f"Authorization: {len(self.authorized_user_ids)} authorized users")

        # Bot control paths from config (M6: these live under TELEGRAM_HANDLER.paths)
        self.bot_dir = os.path.dirname(os.path.abspath(self.config_file))
        self.status_file = Path(paths_cfg.get('bot_status_file',
                                              handler_config.get('bot_status_file', 'logs/bot_status.json')))
        self.manual_stop_flag = Path(paths_cfg.get('manual_stop_flag',
                                                   handler_config.get('manual_stop_flag_file', 'logs/manual_stop.flag')))
        
        # Bot control timeouts from config
        self.bot_startup_max_wait = handler_config.get('bot_startup_max_wait', 10)
        self.bot_startup_check_interval = handler_config.get('bot_startup_check_interval', 1)
        self.process_wait_time = handler_config.get('process_wait_time', 2)
        self.system_command_timeout = handler_config.get('system_command_timeout', 5)
        self.command_poll_interval = handler_config.get('command_poll_interval', 1)
        
        # Trading thresholds from config
        self.close_position_deviation = handler_config.get('close_position_deviation', 20)
        
        # Health check thresholds from config (M6: under TELEGRAM_HANDLER.health_thresholds)
        self.log_active_threshold = health_cfg.get('log_active_minutes',
                                                   handler_config.get('log_active_threshold_minutes', 5))
        self.log_warning_threshold = health_cfg.get('log_warning_minutes',
                                                    handler_config.get('log_warning_threshold_minutes', 60))
        self.margin_safe_level = health_cfg.get('margin_safe_level',
                                                handler_config.get('margin_safe_level', 500))
        self.margin_warning_level = health_cfg.get('margin_warning_level',
                                                   handler_config.get('margin_warning_level', 200))

        # Display settings from config (M6: under TELEGRAM_HANDLER.display)
        self.news_forecast_hours = display_cfg.get('news_forecast_hours',
                                                   handler_config.get('news_forecast_hours', 24))
        self.max_news_events_display = display_cfg.get('max_news_events',
                                                       handler_config.get('max_news_events_display', 5))

        # File paths from config - check both paths object and root level
        self.trade_statistics_file = paths_cfg.get('trade_statistics_file', handler_config.get('trade_statistics_file', 'logs/trade_statistics_{symbol}.json'))
        self.trade_statistics_file = self.trade_statistics_file.replace('{symbol}', self.symbol)
        self.news_events_file = paths_cfg.get('news_events_file', handler_config.get('news_events_file', 'cache/news_events.json'))

        # Initialize MT5 (M2: use the configured terminal path + login, like main_bot,
        # so the handler reads the SAME account/terminal instead of a default one).
        broker = self.config.get('BROKER', {})
        mt5_path = broker.get('mt5_path')
        init_ok = mt5.initialize(path=mt5_path) if mt5_path else mt5.initialize()
        if not init_ok:
            self.logger.error(f"MT5 initialization failed: {mt5.last_error()}")
            raise Exception("Cannot connect to MT5")
        account = broker.get('account')
        password = broker.get('password')
        server = broker.get('server')
        if account and password and server:
            if not mt5.login(int(account), password=password, server=server):
                self.logger.error(f"MT5 login failed: {mt5.last_error()}")
                raise Exception("Cannot log in to MT5")

        # M1: guard against account_info() returning None
        acct = mt5.account_info()
        acct_login = acct.login if acct is not None else "unknown"
        self.logger.info(f"=================================")
        self.logger.info(f"=== Connected to MT5 {acct_login} ===")
        self.logger.info(f"=== Telegram Command Handler  ===")
        self.logger.info(f"=== Built for : {self.symbol} Trades ===")
        self.logger.info(f"=================================")
    
    def setup_logging(self, handler_config: dict):
        """Setup logging system with config path"""
        log_file = handler_config.get('log_file', 'logs/telegram_handler.log')
        log_path = Path(log_file)
        log_path.parent.mkdir(exist_ok=True)
        
        self.logger = logging.getLogger(f"TelegramHandler_{self.symbol}")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers = []
        
        file_handler = logging.FileHandler(log_path, encoding='utf-8')
        console_handler = logging.StreamHandler(sys.stdout)
        
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
    
    def load_config(self):
        """Load bot configuration"""
        try:
            with open(self.config_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Failed to load config: {e}")
            raise
            
    def is_authorized(self, user_id):
        """Check if user is authorized to execute commands"""
        user_id_str = str(user_id)
        authorized = user_id_str in self.authorized_user_ids
        
        if not authorized:
            self.logger.warning(f"Unauthorized command attempt from user ID: {user_id_str}")
        
        return authorized

    def send_message(self, message):
        """Send message to Telegram"""
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }
            response = requests.post(url, json=data, timeout=self.api_timeout)
            
            clean_text = clean_emoji_for_console(message)
            self.logger.info(f"Sent: {clean_text[:100]}...")
            
            return response.json()
        except Exception as e:
            self.logger.error(f"Failed to send message: {e}")
            return None
    
    # ------------------------------------------------------------------
    # COMMAND REPLAY GUARD
    #
    # last_update_id lived only in memory. On restart it reset to 0, so Telegram happily
    # re-delivered every update still in its 24h queue and the handler EXECUTED THEM ALL
    # AGAIN -- a /start, a /stop, a /close from yesterday, replayed against a live account
    # the moment the handler came back up. Two independent guards now:
    #   1. the offset is PERSISTED, so a restart resumes where it left off; and
    #   2. any message older than MAX_MESSAGE_AGE is ignored regardless of the offset,
    #      so even a lost/corrupt offset file cannot resurrect a stale command.
    # ------------------------------------------------------------------
    MAX_MESSAGE_AGE_SECONDS = 120

    def _offset_file(self):
        return Path(self.bot_dir) / 'logs' / 'telegram_offset.json'

    def _load_offset(self):
        try:
            p = self._offset_file()
            if p.exists():
                with open(p, 'r') as f:
                    off = int(json.load(f).get('last_update_id', 0))
                if off > 0:
                    self.logger.info(f"Resuming Telegram updates from offset {off} "
                                     "(stale commands will not be replayed)")
                return max(off, 0)
        except Exception as e:
            self.logger.error(f"Could not load Telegram offset ({e}); starting from 0. "
                              "The age guard still blocks stale commands.")
        return 0

    def _save_offset(self):
        try:
            write_json_atomic(self._offset_file(),
                              {'last_update_id': int(self.last_update_id),
                               'saved_at': datetime.now().isoformat()})
        except Exception as e:
            self.logger.error(f"Could not persist Telegram offset: {e}")

    def _is_stale(self, message):
        """True if this message is too old to act on."""
        ts = message.get('date')
        if not ts:
            return False
        age = time.time() - float(ts)
        if age > self.MAX_MESSAGE_AGE_SECONDS:
            text = (message.get('text') or '')[:40]
            who = (message.get('from') or {}).get('id')
            self.logger.warning(
                f"IGNORED stale Telegram command (age {age:.0f}s > "
                f"{self.MAX_MESSAGE_AGE_SECONDS}s): {text!r} from user {who}. "
                "Queued while the handler was down; not replaying it.")
            return True
        return False

    def get_updates(self):
        """Get new updates from Telegram"""
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
            params = {
                'offset': self.last_update_id + 1,
                'timeout': self.long_poll_timeout
            }
            response = requests.get(url, params=params, timeout=self.long_poll_request_timeout)
            data = response.json()
            # A non-ok response used to be discarded silently, so an expired token or a
            # 409 (another getUpdates consumer) looked exactly like "no new messages".
            if not data.get('ok'):
                self.logger.error(
                    f"Telegram getUpdates ok=false: "
                    f"error_code={data.get('error_code')} "
                    f"description={data.get('description')!r}")
            return data
        except Exception as e:
            # requests puts the full URL -- including the bot token -- in its exception text.
            self.logger.error(f"Failed to get updates: {redact_token(e, self.bot_token)}")
            return None
    
    # ------------------------------------------------------------------
    # PAPER MODE awareness. In paper mode the broker knows nothing about the bot's
    # positions or P&L -- they live in the bot's own state file. Querying MT5 for them
    # (as these commands used to) reports the account's REAL state, which in paper mode is
    # an empty, unchanging account. That reads as "the bot is doing nothing" when it is
    # actually trading a full simulated book.
    # ------------------------------------------------------------------
    def _is_paper(self):
        try:
            return self.load_config().get('SYSTEM', {}).get('paper_mode') is True
        except Exception:
            return False

    def _paper_state(self):
        """The bot's persisted paper book: (positions{ticket:...}, closed_trades[])."""
        try:
            state_file = Path(self.bot_dir) / 'logs' / 'bot_state.json'
            if not state_file.exists():
                return {}, []
            with open(state_file, 'r') as f:
                st = json.load(f)
            return (st.get('paper_positions') or {}), (st.get('paper_closed_trades') or [])
        except Exception as e:
            self.logger.error(f"Could not read paper state: {e}")
            return {}, []

    def _paper_daily_net(self):
        """Realised paper P&L for today, from the paper ledger."""
        _, closed = self._paper_state()
        today = datetime.now().date()
        total, n = 0.0, 0
        for t in closed:
            try:
                if datetime.fromisoformat(t['time']).date() == today:
                    total += float(t['net'])
                    n += 1
            except Exception:
                continue
        return total, n

    def _paper_stats_file(self):
        p = Path(self.trade_statistics_file)
        return str(p.with_name(f"{p.stem}_paper{p.suffix}"))

    def _paper_flag(self):
        """SAFETY: mirror SYSTEM.paper_mode into the relaunch command.

        Re-read from disk so /start reflects the config the bot is about to load, not
        the copy cached when this handler started. Without this, a restart would drop a
        --paper that was only ever passed on the command line and go LIVE.
        """
        try:
            paper = self.load_config().get('SYSTEM', {}).get('paper_mode') is True
        except Exception as e:
            self.logger.error(f"Could not re-read paper_mode ({e}); launching without --paper")
            return ''
        return ' --paper' if paper else ''

    def handle_start(self):
        """Handle /start command - Launch bot in new Windows Terminal tab"""
        try:
            if self._is_bot_running():
                return self.send_message(
                    "⚠️ Bot is already running!\n"
                    "Use /stop first if you want to restart."
                )
            
            if self.manual_stop_flag.exists():
                self.manual_stop_flag.unlink()
                self.logger.info("Removed manual stop flag")
            
            paper_flag = self._paper_flag()
            self.logger.info(
                f"Launching bot in new Windows Terminal tab "
                f"({'PAPER (--paper)' if paper_flag else 'LIVE (no --paper)'})..."
            )

            wt_command = [
                'wt', '-w', '0', 'nt',
                '--title', f'Fusion Sniper Bot - {self.symbol}',
                '--tabColor', '#00FF00',
                '-d', self.bot_dir,
                'cmd', '/c',
                f'color 0A && python main_bot.py {os.path.basename(self.config_file)}{paper_flag}'
            ]
            
            process = subprocess.Popen(
                wt_command,
                cwd=self.bot_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            
            # Wait for bot to start using config values
            elapsed = 0
            while elapsed < self.bot_startup_max_wait:
                time.sleep(self.bot_startup_check_interval)
                elapsed += self.bot_startup_check_interval
                
                if self._is_bot_running():
                    status = self._read_status_file()
                    return self.send_message(
                        "✅ Bot started successfully!\n\n"
                        f"📊 Status: Running\n"
                        f"🔢 PID: {status.get('pid', 'Unknown')}\n"
                        f"⏰ Started: {status.get('start_time', 'Unknown')}\n"
                        f"⏱️ Startup time: {elapsed}s\n\n"
                    )
            
            return self.send_message(
                f"⚠️ Bot launch command sent, but status file not found after {self.bot_startup_max_wait}s.\n"
                "The bot may still be initializing. Please check the Windows Terminal tab.\n\n"
                "Try /status in a few seconds to verify."
            )
                
        except Exception as e:
            error_msg = f"Error starting bot: {str(e)}"
            self.logger.error(error_msg, exc_info=True)
            return self.send_message(f"❌ {error_msg}")
    
    def handle_stop(self):
        """Handle /stop command . Stop bot, keep positions open"""
        try:
            if not self._is_bot_running():
                return self.send_message(
                    "⚠️ Bot is not running!\n"
                    "Nothing to stop."
                )
            
            status = self._read_status_file()
            bot_pid = status.get('pid')
            
            if not bot_pid:
                return self.send_message(
                    "❌ Could not read bot PID from status file.\n"
                    "Manual intervention may be required."
                )
            
            self.send_message(
                "🛑 Stopping bot\n"
                "📍 Leaving positions open and shutting down"
            )
            
            self._create_manual_stop_flag()
            self.logger.info("Created manual stop flag")

            # v5.0.0 (M11): prefer a GRACEFUL stop. main_bot watches the manual_stop
            # flag and shuts down cleanly (saving state/stats). Wait briefly for it to
            # exit on its own before falling back to a force-kill.
            graceful_wait = 15
            waited = 0
            while waited < graceful_wait:
                time.sleep(self.bot_startup_check_interval)
                waited += self.bot_startup_check_interval
                if not self._is_bot_running():
                    return self.send_message(
                        "✅ Bot stopped gracefully (clean shutdown).\n\n"
                        "📍 Positions left open with their SL/TP.\n"
                        "ℹ️ Use /start to launch again."
                    )

            self.logger.info("Graceful stop timed out; force-killing process tree")
            parent_cmd_pid = self._get_parent_cmd_process(bot_pid)
            
            if parent_cmd_pid:
                try:
                    kill_command = f'taskkill /PID {parent_cmd_pid} /F /T'
                    result = subprocess.run(
                        kill_command,
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=self.system_command_timeout
                    )
                    
                    if result.returncode == 0:
                        self.logger.info(f"Successfully killed CMD process tree (PID: {parent_cmd_pid})")
                    else:
                        self.logger.error(f"Failed to kill CMD process: {result.stderr}")
                
                except subprocess.TimeoutExpired:
                    self.logger.error("Timeout while trying to kill CMD process")
                except Exception as e:
                    self.logger.error(f"Error killing CMD process: {str(e)}")
            else:
                try:
                    kill_command = f'taskkill /PID {bot_pid} /F /T'
                    subprocess.run(kill_command, shell=True, timeout=self.system_command_timeout)
                    self.logger.info(f"Killed Python process directly (PID: {bot_pid})")
                except Exception as e:
                    self.logger.error(f"Error killing Python process: {str(e)}")
            
            time.sleep(self.process_wait_time)
            
            if not self._is_bot_running():
                # Get current open positions count
                positions = mt5.positions_get(symbol=self.symbol)
                open_positions = 0
                if positions:
                    open_positions = len([p for p in positions if p.magic == self.magic_number])
                
                return self.send_message(
                    "✅ Bot stopped successfully!\n\n"
                    f"📊 Positions left open: {open_positions}\n"
                    f"🛑 Process terminated: PID {bot_pid}\n"
                    "🔴 Tab will show exit message (press Ctrl+D to close)\n\n"
                    "⚠️ Positions will continue with their SL/TP\n\n"
                    "ℹ️ Use /start to launch bot again"
                )
            else:
                return self.send_message(
                    "⚠️ Bot process may still be running.\n"
                    "Please check Windows Terminal and Task Manager.\n"
                    f"Process PID was: {bot_pid}"
                )
                
        except Exception as e:
            error_msg = f"Error stopping bot: {str(e)}"
            self.logger.error(error_msg, exc_info=True)
            return self.send_message(f"❌ {error_msg}")
    
    def _get_parent_cmd_process(self, python_pid: int) -> Optional[int]:
        """Find the parent CMD.exe process that launched the Python bot"""
        try:
            wmic_command = f'wmic process where "ProcessId={python_pid}" get ParentProcessId'
            result = subprocess.run(
                wmic_command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.system_command_timeout
            )
            
            if result.returncode == 0:
                lines = [line.strip() for line in result.stdout.split('\n') if line.strip()]
                
                for line in lines:
                    if line and line.isdigit():
                        parent_pid = int(line)
                        
                        check_cmd = f'tasklist /FI "PID eq {parent_pid}" /FI "IMAGENAME eq cmd.exe" /NH'
                        check_result = subprocess.run(
                            check_cmd,
                            shell=True,
                            capture_output=True,
                            text=True,
                            timeout=self.system_command_timeout
                        )
                        
                        if 'cmd.exe' in check_result.stdout.lower():
                            self.logger.info(f"Found parent CMD process: PID {parent_pid}")
                            return parent_pid
                        else:
                            self.logger.warning(f"Parent process {parent_pid} is not CMD")
            
            self.logger.warning("Parent CMD process not found")
            return None
            
        except Exception as e:
            self.logger.error(f"Error finding parent CMD process: {str(e)}")
            return None
    
    def _bot_liveness(self):
        """(state, info): STOPPED | ALIVE | HUNG, identity-verified + heartbeat."""
        try:
            return check_liveness(self.config, self.status_file,
                                  lock_path(self.config, self.bot_dir_logs()))
        except Exception as e:
            self.logger.error(f"Error checking bot liveness: {redact_token(e, self.bot_token)}")
            return STOPPED, {}

    def bot_dir_logs(self):
        return os.path.join(self.bot_dir, 'logs')

    def _is_bot_running(self) -> bool:
        """True if a live instance holds the bot's identity.

        A HUNG bot still counts as RUNNING: /start must not launch a second instance
        alongside a wedged one. The watchdog is what kills and replaces a hang.

        The old check shelled out to tasklist with the PID interpolated into a string and
        substring-matched the result, so any process that inherited the dead bot's recycled
        PID reported it as running. Identity (image name) + heartbeat now decide.
        """
        state, _ = self._bot_liveness()
        return state != STOPPED
    
    def _read_status_file(self) -> Dict[str, Any]:
        """Read bot status file"""
        try:
            if self.status_file.exists():
                with open(self.status_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            self.logger.error(f"Error reading status file: {str(e)}")
        
        return {}
    
    def _create_manual_stop_flag(self) -> None:
        """Create manual stop flag file to prevent watchdog from restarting bot"""
        try:
            self.manual_stop_flag.parent.mkdir(parents=True, exist_ok=True)
            with open(self.manual_stop_flag, 'w') as f:
                f.write(json.dumps({
                    'stopped_at': datetime.now().isoformat(),
                    'reason': 'Manual stop via /stop command'
                }))
        except Exception as e:
            self.logger.error(f"Error creating manual stop flag: {str(e)}")
    
    def _get_bot_state(self):
        """Get current bot activity state - HEALTH COMMAND FIX"""
        try:
            # Check if daily target reached (from main bot)
            # Note: This checks today's profit against target
            daily_profit = self._get_daily_net_profit()
            daily_target = self.config['TRADING'].get('daily_profit_target', 0)
            
            if daily_target > 0 and daily_profit >= daily_target:
                return "paused_target", f"Paused (Target: £{daily_profit:.2f})"
            
            # Check trading hours
            if not self._is_within_trading_hours():
                return "outside_hours", "Outside Trading Hours"
            
            # Check for open positions
            positions = mt5.positions_get(symbol=self.symbol)
            if positions:
                bot_positions = [p for p in positions if p.magic == self.magic_number]
                if bot_positions:
                    return "trading", f"Trading ({len(bot_positions)} position{'s' if len(bot_positions) > 1 else ''})"
            
            # Check news filter (high-impact news blocking trades)
            # Note: We can't directly check news_filter from here, so we'll check log patterns
            log_file = f"logs/{self.symbol}_{datetime.now().strftime('%d%m%Y')}.log"  # v5.0.0 (M5): real rotated log name
            if os.path.exists(log_file):
                # Read last few lines to check for news filter messages
                try:
                    with open(log_file, 'r') as f:
                        lines = f.readlines()
                        recent_lines = lines[-20:] if len(lines) > 20 else lines
                        for line in reversed(recent_lines):
                            if 'news filter' in line.lower() and 'blocking' in line.lower():
                                # Check if this was in last 5 minutes
                                return "news_filter", "News Filter Active"
                            elif 'cooldown' in line.lower():
                                # Check for cooldown
                                return "cooldown", "Trade Cooldown"
                except:
                    pass
            
            # Default: Bot is monitoring markets
            return "monitoring", "Monitoring Markets"
            
        except Exception as e:
            self.logger.error(f"Error getting bot state: {e}")
            return "unknown", "Unknown State"
    
    def _get_daily_net_profit(self):
        """Get today's NET profit for state checking - HEALTH COMMAND FIX"""
        try:
            magic_number = self.config['BROKER']['magic_number']
            broker_tz_offset = self.config['BROKER'].get('broker_timezone_offset', 0)
            
            # Calculate UK midnight in server time
            uk_now = datetime.now()
            uk_midnight = uk_now.replace(hour=0, minute=0, second=0, microsecond=0)
            server_midnight = uk_midnight + timedelta(hours=broker_tz_offset)
            
            deals = mt5.history_deals_get(server_midnight, datetime.now())
            if not deals:
                return 0.0
            
            total_profit = 0.0
            total_commission = 0.0
            total_swap = 0.0
            
            for deal in deals:
                if deal.magic == magic_number:
                    # Count commission from ALL deals
                    total_commission += abs(deal.commission)
                    
                    # Only count profit and swap from exit deals
                    if deal.entry == mt5.DEAL_ENTRY_OUT:
                        total_profit += deal.profit
                        total_swap += deal.swap
            
            return total_profit - total_commission + total_swap
            
        except Exception as e:
            self.logger.error(f"Error getting daily profit for state: {e}")
            return 0.0
    
    def _is_within_trading_hours(self):
        """Check if current time is within configured trading hours"""
        try:
            now = datetime.now()
            current_day = now.weekday()  # 0=Monday, 6=Sunday
            current_hour = now.hour

            # Prefer WATCHDOG.trading_hours, fall back to TRADING.trading_hours
            trading_hours = (
                self.config.get('WATCHDOG', {}).get('trading_hours')
                or self.config.get('TRADING', {}).get('trading_hours', {})
            )

            saturday_closed = trading_hours.get('saturday_closed', True)
            sunday_closed = trading_hours.get('sunday_closed', False)
            monday_open_hour = trading_hours.get('monday_open_hour', 0)
            sunday_open_hour = trading_hours.get('sunday_open_hour', 23)
            friday_close_hour = trading_hours.get('friday_close_hour', 23)

            # Saturday . closed
            if current_day == 5 and saturday_closed:
                return False

            # Sunday check
            if current_day == 6:
                if sunday_closed:
                    # Sunday closed . no trading
                    return False
                else:
                    # Sunday trading enabled from configured hour
                    return current_hour >= sunday_open_hour

            # Monday check . only if Sunday was closed and a Monday open hour is set
            if current_day == 0 and sunday_closed and monday_open_hour > 0:
                return current_hour >= monday_open_hour

            # Friday . closes at specified hour
            if current_day == 4:
                return current_hour < friday_close_hour

            # Monday to Thursday . always open, unless restricted by Monday rule above
            return True

        except Exception as e:
            self.logger.error(f"Error checking trading hours: {e}")
            # Fail safe . treat hours as open so health does not incorrectly report outside hours
            return True
    
    # v5.0.0 (L4): removed unused _close_all_positions() (never called; /stop leaves
    # positions open by design, and it had an unguarded result.retcode None-deref).

    def _get_bot_status_state(self):
        """
        Get detailed bot status state for /status command
        Returns: (emoji, status_text) tuple
        """
        try:
            # First check if bot process is running
            if not self._is_bot_running():
                return "🔴", "Stopped"
            
            # Bot is running, now check its state
            
            # Check if daily target reached
            daily_profit = self._get_daily_net_profit()
            daily_target = self.config['TRADING'].get('daily_profit_target', 0)
            
            if daily_target > 0 and daily_profit >= daily_target:
                return "🟡", "Paused (Daily Target)"
            
            # Check for news avoidance by examining recent log entries
            log_file = f"logs/{self.symbol}_{datetime.now().strftime('%d%m%Y')}.log"  # v5.0.0 (M5): real rotated log name
            if os.path.exists(log_file):
                try:
                    with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                        lines = f.readlines()
                        # Check last 30 lines for recent news filter activity
                        recent_lines = lines[-30:] if len(lines) > 30 else lines
                        
                        for line in reversed(recent_lines):
                            # Look for news filter blocking messages
                            if '[NEWS FILTER]' in line and 'Avoiding trading' in line:
                                # Check if this log entry is recent (within last 2 minutes)
                                try:
                                    # Extract timestamp from log line (format: YYYY-MM-DD HH:MM:SS)
                                    if len(line) > 19:
                                        timestamp_str = line[:19]
                                        log_time = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                                        time_diff = (datetime.now() - log_time).total_seconds()
                                        
                                        # If news filter was active in last 2 minutes, consider it active
                                        if time_diff < 120:
                                            return "🟡", "Paused (News)"
                                except:
                                    pass
                except Exception as e:
                    self.logger.error(f"Error checking news filter state: {e}")
            
            # If we get here, bot is running and actively trading/monitoring
            return "🟢", "Trading"
            
        except Exception as e:
            self.logger.error(f"Error getting bot status state: {e}")
            return "🔴", "Unknown"
    

    def handle_status(self):
        """Handle /status command - MODIFIED to show detailed bot states"""
        try:
            account_info = mt5.account_info()
            if not account_info:
                return self.send_message("❌ Cannot retrieve account info")
            
            balance = account_info.balance
            equity = account_info.equity
            profit = equity - balance
            profit_pct = (profit / balance * 100) if balance > 0 else 0
            
            paper = self._is_paper()

            if paper:
                # Positions and P&L come from the bot's simulated book, NOT the broker.
                paper_pos, _ = self._paper_state()
                pos_count = len(paper_pos)
                daily_profit, n_today = self._paper_daily_net()
            else:
                daily_profit = self.get_daily_profit()
                positions = mt5.positions_get(symbol=self.symbol)
                pos_count = len([p for p in positions
                                 if p.magic == self.magic_number]) if positions else 0

            # Get detailed bot status state
            status_emoji, status_text = self._get_bot_status_state()
            bot_status = f"{status_emoji} {status_text}"

            header = ("📝 <b>PAPER MODE — SIMULATED, NO REAL ORDERS</b>\n"
                      "<i>positions and P/L below are simulated; the account figures are real</i>\n\n"
                      if paper else "")
            acct_label = "Account (untouched in paper)" if paper else "Account"
            pl_label = "Paper Daily P/L (net)" if paper else "Daily P/L"
            pos_label = "Simulated Positions" if paper else "Active Positions"

            message = f"""🤖 <b>Status</b>

{header}{bot_status}

<b>{acct_label}</b>
💰 Balance: £{balance:.2f}
📊 Equity: £{equity:.2f}
📈 Open P/L: £{profit:.2f} ({profit_pct:+.2f}%)

📍 {pl_label}: £{daily_profit:.2f}
🎯 Daily Target: £{self.config['TRADING']['daily_profit_target']:.2f}

🎯 {pos_label}: {pos_count}/{self.config['RISK']['max_positions_per_bot']}

⏰ {datetime.now().strftime('%d/%m/%Y %I:%M:%S %p')}"""

            return self.send_message(message)
            
        except Exception as e:
            self.logger.error(f"Error in handle_status: {e}")
            return self.send_message(f"❌ Error: {str(e)}")
    
    
    def handle_positions(self):
        """Handle /positions command"""
        try:
            if self._is_paper():
                return self._handle_positions_paper()

            positions = mt5.positions_get(symbol=self.symbol)

            if not positions:
                return self.send_message("📊 No open positions")
            
            my_positions = [p for p in positions if p.magic == self.magic_number]
            
            if not my_positions:
                return self.send_message("📊 No open positions")
            
            message = f"📊 <b>Open Positions ({len(my_positions)})</b>\n\n"
            
            for pos in my_positions:
                pos_type = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
                duration = datetime.now() - datetime.fromtimestamp(pos.time)
                hours = int(duration.total_seconds() / 3600)
                minutes = int((duration.total_seconds() % 3600) / 60)
                
                message += f"""<b>{self.symbol}</b> - {pos_type}
💹 Entry: {pos.price_open:.2f}
📍 Current: {pos.price_current:.2f}
💰 P/L: £{pos.profit:.2f}
🎯 SL: {pos.sl:.2f} | TP: {pos.tp:.2f}
⏰ Duration: {hours}h {minutes}m

"""
            
            return self.send_message(message)
            
        except Exception as e:
            self.logger.error(f"Error in handle_positions: {e}")
            return self.send_message(f"❌ Error: {str(e)}")
    
    def _handle_positions_paper(self):
        """PAPER: list the SIMULATED positions from the bot's state file. The broker has no
        record of these -- querying MT5 would report an empty account and look like the bot
        was idle."""
        paper_pos, _ = self._paper_state()
        if not paper_pos:
            return self.send_message("📝 <b>PAPER MODE</b>\n\n📊 No open simulated positions")

        tick = mt5.symbol_info_tick(self.symbol)
        message = (f"📝 <b>PAPER MODE — SIMULATED POSITIONS ({len(paper_pos)})</b>\n"
                   f"<i>not real; the broker holds nothing</i>\n\n")

        for tkt, p in sorted(paper_pos.items()):
            try:
                is_buy = int(p.get('type', 0)) == 0
                pos_type = "BUY" if is_buy else "SELL"
                entry = float(p.get('price_open', 0))
                vol = float(p.get('volume', 0))
                cur = 0.0
                if tick:
                    cur = tick.bid if is_buy else tick.ask
                # floating P&L is a GROSS price move (commission/swap land at close)
                floating = 0.0
                if cur:
                    calc = mt5.order_calc_profit(
                        mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
                        self.symbol, vol, entry, cur)
                    floating = float(calc) if calc is not None else 0.0
                opened = datetime.fromtimestamp(int(p.get('time', 0)))
                dur = datetime.now() - opened
                hours = int(dur.total_seconds() / 3600)
                minutes = int((dur.total_seconds() % 3600) / 60)

                message += f"""<b>{self.symbol}</b> - {pos_type} (sim #{tkt})
💹 Entry: {entry:.2f}
📍 Current: {cur:.2f}
💰 Floating P/L: £{floating:.2f} <i>(gross)</i>
🎯 SL: {float(p.get('sl') or 0):.2f} | TP: {float(p.get('tp') or 0):.2f}
📦 Volume: {vol}
⏰ Duration: {hours}h {minutes}m

"""
            except Exception as e:
                self.logger.error(f"Error rendering paper position {tkt}: {e}")

        return self.send_message(message)

    def handle_daily(self):
        """Handle /daily command - Shows NET profit with swap - TIMEZONE AWARE"""
        try:
            if self._is_paper():
                return self._handle_daily_paper()

            if not mt5.initialize():
                return self.send_message("❌ MT5 connection failed")

            # Get timezone offset from config
            broker_timezone_offset = self.config.get('BROKER', {}).get('broker_timezone_offset', 0)
            
            # Calculate today's start in broker timezone
            local_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            broker_today_start = local_midnight + timedelta(hours=broker_timezone_offset)
            broker_now = datetime.now() + timedelta(hours=broker_timezone_offset)
            
            self.logger.info(f"handle_daily timezone adjustment:")
            self.logger.info(f"  Local midnight: {local_midnight.strftime('%Y-%m-%d %H:%M:%S')}")
            self.logger.info(f"  Broker today start: {broker_today_start.strftime('%Y-%m-%d %H:%M:%S')}")
            self.logger.info(f"  Broker now: {broker_now.strftime('%Y-%m-%d %H:%M:%S')}")
            self.logger.info(f"  Offset: {broker_timezone_offset} hours")
            
            deals = mt5.history_deals_get(broker_today_start, broker_now)
            
            if deals is None or len(deals) == 0:
                return self.send_message("📊 <b>Daily Statistics</b>\n\nNo trades today yet.")
            
            # Initialize counters
            total_profit = 0
            total_commission = 0
            total_swap = 0
            trade_count = 0
            swap_count = 0
            win_count = 0
            loss_count = 0
            winning_profit = 0
            losing_profit = 0
            
            # Count ALL deals for commission, but only EXIT deals for profit
            for deal in deals:
                if deal.magic == self.magic_number:
                    # Count commission from ALL deals (entry + exit)
                    total_commission += abs(deal.commission)
                    
                    # Only count exit deals for profit/wins/losses
                    if deal.entry == 1:  # DEAL_ENTRY_OUT
                        total_profit += deal.profit
                        total_swap += deal.swap
                        trade_count += 1
                        
                        if deal.swap != 0:
                            swap_count += 1
                        
                        if deal.profit > 0:
                            win_count += 1
                            winning_profit += deal.profit
                        else:
                            loss_count += 1
                            losing_profit += abs(deal.profit)
            
            # NET profit = Gross - Commission + Swap
            net_profit = total_profit - total_commission + total_swap
            
            win_rate = (win_count / trade_count * 100) if trade_count > 0 else 0
            
            daily_target = self.config.get('TRADING', {}).get('daily_profit_target', 175.0)
            
            # Use NET profit for target percentage
            target_percent = (net_profit / daily_target * 100) if daily_target > 0 else 0
            remaining = daily_target - net_profit
            
            # Add timezone info to response
            tz_info = f" (GMT+{broker_timezone_offset})" if broker_timezone_offset != 0 else ""
            
            # Swap display: show sign clearly
            swap_sign = "+" if total_swap >= 0 else ""
            swap_text = f"{swap_sign}£{total_swap:.2f}"
            if swap_count > 0:
                swap_text += f" ({swap_count} trades)"
            else:
                swap_text += " (no overnight positions)"
            
            response = f"""
📊 <b>Daily Statistics{tz_info}</b>

<b>Profit Summary:</b>
Gross Profit: £{total_profit:.2f}
Commission: £{total_commission:.2f}
Swap: {swap_text}
═══════════════════════
NET Profit: £{net_profit:.2f}

<b>Target Progress:</b>
Daily Target: £{daily_target:.2f}
Achievement: {target_percent:.1f}%
Remaining: £{remaining:.2f}

<b>Trade Performance:</b>
Total Trades: {trade_count}
Wins: {win_count} ({win_rate:.1f}%)
Losses: {loss_count}

<b>Breakdown:</b>
Win Total: £{winning_profit:.2f}
Loss Total: £{losing_profit:.2f}
"""
            
            return self.send_message(response)
            
        except Exception as e:
            self.logger.error(f"Error in handle_daily: {e}")
            return self.send_message(f"❌ Error getting daily stats: {str(e)}")
    
    def _handle_daily_paper(self):
        """PAPER: report the simulated ledger. The broker's deal history is empty in paper
        mode, so the live query would always report a flat, profitless day."""
        paper_pos, closed = self._paper_state()
        today = datetime.now().date()
        todays = []
        for t in closed:
            try:
                if datetime.fromisoformat(t['time']).date() == today:
                    todays.append(t)
            except Exception:
                continue

        net = sum(float(t['net']) for t in todays)
        wins = [t for t in todays if float(t['net']) > 0]
        losses = [t for t in todays if float(t['net']) < 0]
        target = self.config['TRADING'].get('daily_profit_target', 0)
        max_loss = self.config.get('RISK', {}).get('max_daily_loss', 0)

        lines = [
            "📝 <b>PAPER MODE — SIMULATED LEDGER</b>",
            "<i>net = gross - commission + swap, same cost model as the backtest</i>",
            "",
            f"📅 <b>Today ({today.strftime('%d/%m/%Y')})</b>",
            f"💰 Net P/L: £{net:.2f}",
            f"📈 Closed: {len(todays)}  (W {len(wins)} / L {len(losses)})",
            f"🎯 Target: £{float(target):.2f}"
            + ("  ✅ REACHED — new entries paused" if target and net >= float(target) else ""),
            f"🛑 Loss limit: £{float(max_loss):.2f}"
            + ("  ⛔ BREACHED — day is dead" if max_loss and net <= -abs(float(max_loss)) else ""),
            f"📊 Open simulated positions: {len(paper_pos)}",
        ]
        if todays:
            lines += ["", "<b>Closed today:</b>"]
            for t in todays[-10:]:
                ts = datetime.fromisoformat(t['time']).strftime('%H:%M')
                tk = t.get('ticket', '?')
                lines.append(f"  {ts}  #{tk}  £{float(t['net']):+.2f}")
        lines += ["", f"📈 Lifetime simulated trades: {len(closed)}",
                  f"💵 Lifetime net: £{sum(float(t['net']) for t in closed):.2f}",
                  "", f"⏰ {datetime.now().strftime('%d/%m/%Y %I:%M:%S %p')}"]
        return self.send_message("\n".join(lines))

    def handle_health(self):
        """Handle /health command with intelligent bot state detection - UPDATED"""
        try:
            account = mt5.account_info()
            mt5_status = "✅ Connected" if account else "❌ Disconnected"
            
            bot_running = self._is_bot_running()
            bot_status = "✅ Running" if bot_running else "🔴 Stopped"
            
            # Get intelligent bot state
            state_code, state_display = self._get_bot_state()
            
            # Map state to icon + display
            if state_code == "trading":
                log_status = f"✅ {state_display}"
            elif state_code == "paused_target":
                log_status = f"⏸️ {state_display}"
            elif state_code == "outside_hours":
                log_status = f"🕐 {state_display}"
            elif state_code == "news_filter":
                log_status = f"📰 {state_display}"
            elif state_code == "cooldown":
                log_status = f"⏳ {state_display}"
            elif state_code == "monitoring":
                log_status = f"🔄 {state_display}"
            else:
                log_status = f"❓ {state_display}"
            
            # Margin level - only show percentage when positions are open
            if account and account.margin > 0:
                # Positions open - show actual margin level
                margin_level = account.margin_level
                if margin_level > self.margin_safe_level:
                    margin_status = f"✅ {margin_level:.0f}%"
                elif margin_level > self.margin_warning_level:
                    margin_status = f"🟡 {margin_level:.0f}%"
                else:
                    margin_status = f"🔴 {margin_level:.0f}%"
            else:
                # No positions - margin level not applicable
                margin_status = "➖ No Positions"
            
            message = f"""🔍 <b>System Health Check</b>

<b>Bot Process:</b> {bot_status}
<b>MT5 Connection:</b> {mt5_status}
<b>Margin Level:</b> {margin_status}
<b>Bot Activity:</b> {log_status}

<b>Configuration:</b>
Lot Size: {self.config['TRADING']['lot_size']}
Max Positions: {self.config['RISK']['max_positions_per_bot']}
Daily Target: £{self.config['TRADING']['daily_profit_target']:.2f}

⏰ {datetime.now().strftime('%d/%m/%Y %I:%M:%S %p')}"""
            
            return self.send_message(message)
            
        except Exception as e:
            self.logger.error(f"Error in handle_health: {e}")
            return self.send_message(f"❌ Error: {str(e)}")
    
    def handle_stats(self):
        """Handle /stats command using config file path"""
        try:
            # PAPER: read the SEPARATE paper stats file. Simulated trades are never mixed
            # into the live statistics, so reading the live file here would report nothing.
            paper = self._is_paper()
            stats_path = self._paper_stats_file() if paper else self.trade_statistics_file
            banner = ("📝 <b>PAPER MODE — SIMULATED STATISTICS</b>\n"
                      "<i>from the paper stats file; live stats are kept separate</i>\n\n"
                      if paper else "")

            if not os.path.exists(stats_path):
                return self.send_message(f"{banner}📊 No statistics data available yet")

            with open(stats_path, 'r') as f:
                stats = json.load(f)

            total_trades = stats.get('total_trades', 0)
            if total_trades == 0:
                return self.send_message(f"{banner}📊 No trades recorded yet")
            
            win_rate = stats.get('win_rate', 0)
            avg_profit = stats.get('average_profit', 0)
            best_trade = stats.get('best_trade', 0)
            worst_trade = stats.get('worst_trade', 0)
            avg_mae = stats.get('average_mae', 0)
            avg_mfe = stats.get('average_mfe', 0)
            
            sessions = stats.get('trades_by_session', {})
            exit_reasons = stats.get('exit_reasons', {})
            
            message = f"""{banner}📊 <b>{self.symbol} Trading Statistics</b>

📈 Total Trades: {total_trades}
🎯 Win Rate: {win_rate:.1f}%
💰 Avg P/L: £{avg_profit:.2f}
🚀 Best Trade: £{best_trade:.2f}
📉 Worst Trade: £{worst_trade:.2f}

<b>Risk Metrics:</b>
MAE (Max Drawdown): £{avg_mae:.2f}
MFE (Max Profit): £{avg_mfe:.2f}

<b>Session Breakdown:</b>
Asia: {sessions.get('asia', 0)} | London: {sessions.get('london', 0)} | NY: {sessions.get('new_york', 0)}

<b>Exit Reasons:</b>
TP: {exit_reasons.get('take_profit', 0)} | SL: {exit_reasons.get('stop_loss', 0)}
Trail: {exit_reasons.get('trailing', 0)} | BE: {exit_reasons.get('breakeven', 0)}

⏰ {datetime.now().strftime('%d/%m/%Y %I:%M:%S %p')}"""
            
            return self.send_message(message)
            
        except Exception as e:
            self.logger.error(f"Error in handle_stats: {e}")
            return self.send_message(f"❌ Error: {str(e)}")
    
    def handle_news(self):
        """Handle /news command - show next 72 hours (3 days)"""
        try:
            # Using config path
            if not os.path.exists(self.news_events_file):
                return self.send_message("📰 No news data available")
        
            with open(self.news_events_file, 'r') as f:
                news_data = json.load(f)

            # v5.0.0 (H5): event times are stored in UTC (tz-aware); compare in UTC.
            now = datetime.now(timezone.utc)

            # Show next 72 hours (3 days) - more useful than just today+tomorrow
            window_end = now + timedelta(hours=72)
            hours_ahead = 72

            upcoming = []

            for event in news_data.get('events', []):
                event_time = datetime.fromisoformat(event['time'])
                if event_time.tzinfo is None:        # tolerate legacy naive cache
                    event_time = event_time.replace(tzinfo=timezone.utc)
                # Show events from now until 72 hours ahead
                if now <= event_time <= window_end:
                    upcoming.append(event)
        
            if not upcoming:
                no_news_msg = f"📰 <b>NEWS UPDATE</b> - {now.strftime('%d/%m/%Y %I:%M %p')}\n"
                no_news_msg += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                no_news_msg += "✅ <b>No high-impact news</b>\n\n"
                no_news_msg += "<i>Clear to trade - no events in next 72 hours</i>"
                return self.send_message(no_news_msg)

            message = f"📰 <b>NEWS UPDATE</b> - {now.strftime('%d/%m/%Y %I:%M %p')}\n"
            message += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            message += "⚡ <b>Upcoming (Next 72 Hours):</b>\n\n"
        
            # Show all events (not limited to config max)
            for event in upcoming:
                event_time = datetime.fromisoformat(event['time'])
            
                # Check day label
                days_diff = (event_time.date() - now.date()).days
                if days_diff == 0:
                    day_label = "Today"
                elif days_diff == 1:
                    day_label = "Tomorrow"
                else:
                    day_label = event_time.strftime('%A')  # Monday, Tuesday, etc.
            
                time_str = event_time.strftime('%I:%M %p')
            
                # Impact emoji
                impact_emoji = "🔴" if event['impact'] == "High" else "🏖️"
            
                # Get URL if available
                event_url = event.get('url', '')
                if event_url:
                    url_line = f"   🔗 <a href='{event_url}'>View on ForexFactory</a>\n"
                else:
                    url_line = ""
            
                # Determine buffer time based on impact type
                if event['impact'] == 'Holiday':
                    buffer_text = "🔒 Trading paused: ALL DAY"
                else:
                    buffer_text = "🔒 Trading paused: 30min before/after"
                
                message += f"{impact_emoji} <b>{day_label} {time_str}</b> | {event['currency']}\n"
                message += f"   {event['title']}\n"
                message += url_line
                message += f"   {buffer_text}\n\n"
        
            event_count = len(upcoming)
            message += "━━━━━━━━━━━━━━━━━━━━━━\n"
            message += f"<b>Total:</b> {event_count} event{'s' if event_count != 1 else ''}"
        
            return self.send_message(message)
            
        except Exception as e:
            self.logger.error(f"Error in handle_news: {e}")
            return self.send_message(f"❌ Error: {str(e)}")
    
    def handle_help(self):
        """Handle /help command"""
        message = """🤖 <b>Commands</b>

<b>Bot Control:</b>
/start - Launch bot remotely
/stop - Stop bot ony, keep positions

<b>System Monitoring:</b>
/status - Current status & positions
/positions - Detailed position info
/daily - Today's performance (with NET profit)
/health - System health check

<b>Analytics:</b>
/stats - Trading statistics
/news - Upcoming USD news

<b>Help:</b>
/help - Show this message

💰 {self.symbol} Sniper Specialist
🎯 Config-Driven | All settings adjustable"""

        return self.send_message(message)
    
    def get_daily_profit(self):
        """Calculate total NET profit for today - TIMEZONE AWARE + SWAP INCLUDED"""
        try:
            # Get timezone offset from config
            broker_timezone_offset = self.config.get('BROKER', {}).get('broker_timezone_offset', 0)
            
            # Calculate today's start in broker timezone
            local_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            broker_today_start = local_midnight + timedelta(hours=broker_timezone_offset)
            broker_now = datetime.now() + timedelta(hours=broker_timezone_offset)
            
            self.logger.debug(f"get_daily_profit timezone adjustment:")
            self.logger.debug(f"  Local midnight: {local_midnight}")
            self.logger.debug(f"  Broker today start: {broker_today_start}")
            self.logger.debug(f"  Offset applied: {broker_timezone_offset} hours")
            
            deals = mt5.history_deals_get(broker_today_start, broker_now)
            
            if not deals:
                return 0.0
            
            total_profit = 0.0
            total_commission = 0.0
            total_swap = 0.0
            
            # Count ALL deals for commission, but only EXIT deals for profit
            for deal in deals:
                if deal.magic == self.magic_number:
                    # Count commission from ALL deals (entry + exit)
                    total_commission += abs(deal.commission)
                    
                    # Only count exit deals for profit/swap
                    if deal.entry == mt5.DEAL_ENTRY_OUT:
                        total_profit += deal.profit
                        total_swap += deal.swap
            
            # NET profit = Gross - Commission + Swap
            net_profit = total_profit - total_commission + total_swap
            return net_profit
                    
        except Exception as e:
            self.logger.error(f"Error calculating daily profit: {e}")
            return 0.0
    
    def process_command(self, message):
        """Process incoming command"""
        try:
            text = message.get('text', '').strip()
            
            if not text.startswith('/'):
                return
            
            command = text.split()[0].lower().split('@')[0]
            
            # Check authorization
            user_id = message.get('from', {}).get('id', '')
            if not self.is_authorized(user_id):
                self.send_message("<b>Access Denied</b>\n\nYou are not authorized to control this bot.")
                return
            
            self.logger.info(f"Processing command: {command}")
            
            if command == '/start':
                self.handle_start()
            elif command == '/stop':
                self.handle_stop()
            elif command == '/status':
                self.handle_status()
            elif command == '/positions':
                self.handle_positions()
            elif command == '/daily':
                self.handle_daily()
            elif command == '/health':
                self.handle_health()
            elif command == '/stats':
                self.handle_stats()
            elif command == '/news':
                self.handle_news()
            elif command == '/help':
                self.handle_help()
            else:
                self.send_message(f"❓ Unknown command: {command}\n\nUse /help to see available commands.")
                
        except Exception as e:
            self.logger.error(f"Error processing command: {e}")
            self.send_message(f"❌ Error processing command: {str(e)}")
    
    def run(self):
        """Main loop - poll for commands. Returns an exit code (0 = clean)."""
        rc = 0
        try:
            while True:
                updates = self.get_updates()

                if updates and updates.get('ok'):
                    results = updates.get('result', [])
                    for update in results:
                        # Advance the offset for EVERY update, even one we refuse to act on
                        # -- otherwise a stale command is re-fetched forever.
                        self.last_update_id = update['update_id']

                        msg = update.get('message')
                        if msg and not self._is_stale(msg):
                            self.process_command(msg)

                    if results:
                        # Persist AFTER the batch: a crash mid-batch replays at most that
                        # batch, and the age guard still refuses anything genuinely stale.
                        self._save_offset()

                time.sleep(self.command_poll_interval)

        except KeyboardInterrupt:
            print("\nStopping Telegram handler")
            self.logger.info("Telegram handler stopped by user")
        except Exception as e:
            # FATAL: a crashed handler is a bot nobody can stop remotely. It must exit
            # NON-ZERO so whatever supervises it knows it died rather than finished.
            self.logger.error(
                f"Fatal error in main loop: {redact_token(e, self.bot_token)}", exc_info=False)
            self.logger.error(redact_token(traceback.format_exc(), self.bot_token))
            rc = 1
        finally:
            try:
                self._save_offset()
            except Exception:
                pass
            mt5.shutdown()
        return rc


def main():
    """Entry point"""
    try:
        handler = TelegramCommandHandler()
        return handler.run()
    except Exception as e:
        logging.error(f"Failed to start handler: {redact_token(e)}")
        print(redact_token(traceback.format_exc()))
        return 1

if __name__ == '__main__':
    sys.exit(main())
