"""
Watchdog Monitor - Fusion Sniper Trading Bot
Monitors bot health and restarts if necessary
"""

import time
import subprocess
import os
import sys
import json
from datetime import datetime, timedelta
from pathlib import Path

class WatchdogMonitor:
    """Monitor and restart bot if it crashes"""
    
    def __init__(self, config_file='config.json'):
        """Initialize watchdog with config"""
        self.config_file = config_file
        self.config = self.load_config()
        
        # Load watchdog settings from config
        watchdog_config = self.config.get('WATCHDOG', {})
        self.check_interval = watchdog_config.get('check_interval_seconds', 300)
        
        # Trading hours from WATCHDOG.trading_hours
        trading_hours = watchdog_config.get('trading_hours', {})
        self.saturday_closed = trading_hours.get('saturday_closed', True)
        self.sunday_closed = trading_hours.get('sunday_closed', False)
        self.monday_open_hour = trading_hours.get('monday_open_hour', 0)
        self.sunday_open_hour = trading_hours.get('sunday_open_hour', 22)
        self.friday_close_hour = trading_hours.get('friday_close_hour', 22)
        
        # Cache retention from config (convert hours to days)
        cache_retention_hours = watchdog_config.get('cache_retention_hours', 168)
        self.cache_retention_days = cache_retention_hours // 24
        
        # System paths from config
        system_config = self.config.get('SYSTEM', {})
        self.log_dir = Path(system_config.get('log_directory', 'logs'))
        self.bot_status_file = Path(system_config.get('bot_status_file', 'logs/bot_status.json'))
        
        # Track last known bot state to prevent duplicate restarts
        self.last_bot_running = False
        self.startup_grace_period = 30  # Wait 30 seconds before first restart attempt
        self.startup_time = time.time()
        
        print(f"Watchdog Monitor initialized")
        print(f"Check interval: {self.check_interval}s")
        print(f"Startup grace period: {self.startup_grace_period}s")
        print(f"Trading hours: {self._format_trading_hours()}")
    
    def load_config(self):
        """Load bot configuration"""
        try:
            with open(self.config_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading config: {e}")
            sys.exit(1)
    
    def _format_trading_hours(self):
        """Format trading hours for display"""
        saturday_status = "Closed" if self.saturday_closed else "Open"
        sunday_status = "Closed" if self.sunday_closed else f"Open {self.sunday_open_hour:02d}:00"

        if self.sunday_closed:
            return f"Mon {self.monday_open_hour:02d}:00 - Fri {self.friday_close_hour:02d}:00 (Sat/Sun: Closed)"
        else:
            return f"Sun {self.sunday_open_hour:02d}:00 - Fri {self.friday_close_hour:02d}:00 (Sat: {saturday_status})"
    
    def is_within_trading_hours(self):
        """Check if current time is within trading hours"""
        now = datetime.now()
        weekday = now.weekday()  # 0=Monday, 6=Sunday
        hour = now.hour
        
        # Saturday check (if configured as closed)
        if weekday == 5 and self.saturday_closed:  # Saturday = 5
            return False
        
        # Sunday check
        if weekday == 6:  # Sunday = 6
            if self.sunday_closed:
                return False  # Sunday closed - no trading
            elif hour < self.sunday_open_hour:
                return False  # Sunday trading enabled but before opening hour
        
        # Friday after closing hour
        if weekday == 4 and hour >= self.friday_close_hour:  # Friday = 4
            return False
        
        return True
    
    def is_bot_running(self):
        """Check if bot is running by checking status file"""
        if not self.bot_status_file.exists():
            return False
        
        try:
            with open(self.bot_status_file, 'r') as f:
                status = json.load(f)
            
            pid = status.get('pid')
            if not pid:
                return False
            
            # Check if process exists (Windows)
            check_command = f'tasklist /FI "PID eq {pid}" /NH'
            result = subprocess.run(
                check_command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=5
            )
            
            return str(pid) in result.stdout
        except Exception as e:
            print(f"Error checking bot status: {e}")
            return False
    
    def is_bot_recently_started(self):
        """Check if bot was started very recently (within last 30 seconds)"""
        if not self.bot_status_file.exists():
            return False
        
        try:
            file_mtime = self.bot_status_file.stat().st_mtime
            file_age = time.time() - file_mtime
            return file_age < 30  # Bot started less than 30 seconds ago
        except Exception:
            return False
    
    def check_manual_stop_flag(self):
        """Check if manual stop flag exists"""
        telegram_handler_config = self.config.get('TELEGRAM_HANDLER', {})
        paths = telegram_handler_config.get('paths', {})
        manual_stop_flag = Path(paths.get('manual_stop_flag', 'logs/manual_stop.flag'))
        return manual_stop_flag.exists()
    
    def start_bot(self):
        """Start the bot"""
        try:
            print(f"Starting bot with config: {self.config_file}")
            
            # Launch in new Windows Terminal tab
            bot_dir = os.path.dirname(os.path.abspath(self.config_file))
            symbol = self.config['BROKER']['symbol']
            
            wt_command = [
                'wt', '-w', '0', 'nt',
                '--title', f'Fusion Sniper Bot - {symbol}',
                '--tabColor', '#00FF00',
                '-d', bot_dir,
                'cmd', '/c',
                f'color 0A && python main_bot.py {os.path.basename(self.config_file)}'
            ]
            
            subprocess.Popen(
                wt_command,
                cwd=bot_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            
            print("Bot started successfully")
            return True
        except Exception as e:
            print(f"Error starting bot: {e}")
            return False
    
    def cleanup_old_cache(self):
        """Clean up old cache files"""
        try:
            news_config = self.config.get('NEWS_FILTER', {})
            cache_file = Path(news_config.get('cache_file', 'cache/news_events.json'))
            cache_dir = cache_file.parent
            
            if not cache_dir.exists():
                return
            
            cutoff_date = datetime.now() - timedelta(days=self.cache_retention_days)
            deleted_count = 0
            
            for cache_file in cache_dir.glob('*.json'):
                file_mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
                if file_mtime < cutoff_date:
                    cache_file.unlink()
                    deleted_count += 1
            
            if deleted_count > 0:
                print(f"Cleaned up {deleted_count} old cache files")
        except Exception as e:
            print(f"Error cleaning cache: {e}")
    
    def run(self):
        """Main watchdog loop"""
        print("Watchdog Monitor started")
        print(f"Monitoring: {self.config['BROKER']['symbol']}")
        print("Press Ctrl+C to stop\n")
        
        try:
            while True:
                # Check if within trading hours
                if not self.is_within_trading_hours():
                    print(f"[{datetime.now().strftime('%d/%m/%Y %I:%M:%S %p')}] Outside trading hours, sleeping...")
                    time.sleep(self.check_interval)
                    continue
                
                # Check for manual stop flag
                if self.check_manual_stop_flag():
                    print(f"[{datetime.now().strftime('%d/%m/%Y %I:%M:%S %p')}] Manual stop flag detected, not restarting")
                    time.sleep(self.check_interval)
                    continue
                
                # FIXED: Grace period during startup to prevent duplicate launches
                time_since_startup = time.time() - self.startup_time
                if time_since_startup < self.startup_grace_period:
                    remaining = int(self.startup_grace_period - time_since_startup)
                    print(f"[{datetime.now().strftime('%d/%m/%Y %I:%M:%S %p')}] Startup grace period ({remaining}s remaining)...")
                    time.sleep(10)
                    continue
                
                # Check if bot is running
                bot_running = self.is_bot_running()
                bot_recently_started = self.is_bot_recently_started()
                
                if not bot_running:
                    # FIXED: Don't restart if bot just started (another instance may be launching)
                    if bot_recently_started:
                        print(f"[{datetime.now().strftime('%d/%m/%Y %I:%M:%S %p')}] Bot recently started, waiting for confirmation...")
                        time.sleep(10)
                        continue
                    
                    # Only restart if bot was previously running (actual crash)
                    if self.last_bot_running:
                        print(f"[{datetime.now().strftime('%d/%m/%Y %I:%M:%S %p')}] Bot crashed, restarting...")
                        if self.start_bot():
                            # Wait for bot to start
                            time.sleep(10)
                            if self.is_bot_running():
                                print("Bot confirmed running")
                                self.last_bot_running = True
                            else:
                                print("Warning: Bot may not have started successfully")
                    else:
                        print(f"[{datetime.now().strftime('%d/%m/%Y %I:%M:%S %p')}] Bot not running (manual start expected)")
                        self.last_bot_running = False
                else:
                    print(f"[{datetime.now().strftime('%d/%m/%Y %I:%M:%S %p')}] Bot is running")
                    self.last_bot_running = True
                
                # Cleanup old cache files
                self.cleanup_old_cache()
                
                time.sleep(self.check_interval)
        
        except KeyboardInterrupt:
            print("\nWatchdog stopped by user")
        except Exception as e:
            print(f"Watchdog error: {e}")

def main():
    """Entry point"""
    config_file = sys.argv[1] if len(sys.argv) > 1 else 'config.json'
    
    if not os.path.exists(config_file):
        print(f"Error: Config file not found: {config_file}")
        return 1
    
    watchdog = WatchdogMonitor(config_file)
    watchdog.run()
    
    return 0

if __name__ == '__main__':
    sys.exit(main())
