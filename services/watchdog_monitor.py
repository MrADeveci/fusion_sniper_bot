"""
Watchdog Monitor - Fusion Sniper Trading Bot v5.0.0
Monitors bot health and restarts if necessary

LIFECYCLE ISOLATION (13/07/2026 incident)
-----------------------------------------
This process must NEVER share a lifecycle with anything it supervises.

On 13/07 the watchdog, the Telegram handler, the bot and an unrelated editor session were
all consoles of ONE WindowsTerminal.exe. That is not an accident of how they were started:
HKCU:\\Console\\%%Startup DelegationTerminal is the all-zero GUID ("let Windows decide"),
which on Win11 hands EVERY new console -- including a python.exe started by Task Scheduler
-- to Windows Terminal. The watchdog then put the bot in the same window with `wt -w 0`.
When that one window went away at 06:38, all four died together and nothing was left alive
to restart anything. MT5 survived only because it is a GUI app and was never hosted by a
terminal at all.

So: the watchdog is now launched with pythonw.exe -- NO console, therefore no terminal
host, therefore nothing to close. It cannot be killed by a window it does not have. It
supervises the bot AND the handler, which may keep living in `wt` tabs precisely because
the watchdog can resurrect them. The watchdog's own death is covered by the dead-man
switch (BetterStack), which is the only thing outside this machine.

Having no console also means print() goes nowhere under pythonw, so all output is a real
rotating file log: logs/watchdog.log.
"""

import time
import subprocess
import os
import sys
import json
import shutil
import logging
import logging.handlers
import requests
import psutil
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.liveness import (check_liveness, lock_path, redact_token,   # noqa: E402
                              STOPPED, ALIVE, HUNG)
from modules.telegram_notifier import TelegramNotifier                   # noqa: E402

HANDLER_SCRIPT = 'services\\telegram_command_handler.py'
HANDLER_MARKER = 'telegram_command_handler'   # how we recognise it in a command line

# A launch goes wt -> cmd -> python, so the python process does not appear in the process
# list for a few seconds. Until it does, "not running" is indistinguishable from "starting",
# and a watchdog that cannot tell them apart launches a second one. Two handlers polling one
# bot token is a getUpdates 409 storm and duplicate replies to every command. So: having
# just launched it, do not relaunch it for this long.
HANDLER_START_GRACE = 60


def setup_logging(log_dir='logs'):
    """Rotating file log, plus a console handler ONLY if we actually have a console.

    Under pythonw.exe sys.stdout is None, so an unconditional StreamHandler would blow up
    on the first log record -- the watchdog would die at startup and take the whole
    recovery story with it.
    """
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log = logging.getLogger('Watchdog')
    log.setLevel(logging.INFO)
    if log.handlers:
        return log

    fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    fh = logging.handlers.RotatingFileHandler(
        Path(log_dir) / 'watchdog.log', maxBytes=2_000_000, backupCount=5, encoding='utf-8')
    fh.setFormatter(fmt)
    log.addHandler(fh)

    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)

    return log


class WatchdogMonitor:
    """Monitor and restart bot if it crashes"""

    def __init__(self, config_file='config.json'):
        """Initialize watchdog with config"""
        self.config_file = config_file

        # Logging BEFORE anything that can fail, so a bad config is not a silent death.
        self.log = setup_logging()

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

        # Dead-man's switch. SECRET: anyone with this URL can forge liveness, so it lives
        # only in the gitignored config.json. Absent => feature off.
        self.deadman_url = system_config.get('deadman_url') or None
        self.last_deadman_ok = None

        # Track last known bot state to prevent duplicate restarts
        self.last_bot_running = False
        self.startup_grace_period = 30  # Wait 30 seconds before first restart attempt
        self.startup_time = time.time()
        self.handler_started_at = 0.0   # see HANDLER_START_GRACE

        self.log.info("Watchdog Monitor initialized")
        self.log.info(f"Check interval: {self.check_interval}s")
        self.log.info(f"Startup grace period: {self.startup_grace_period}s")
        self.log.info(f"Trading hours: {self._format_trading_hours()}")
        self.log.info(f"Console attached: {sys.stdout is not None} "
                      f"(pythonw => no console => no terminal host => cannot be closed)")

    def load_config(self):
        """Load bot configuration"""
        try:
            with open(self.config_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            self.log.error(f"Error loading config: {e}")
            sys.exit(1)

    def _format_trading_hours(self):
        """Format trading hours for display"""
        saturday_status = "Closed" if self.saturday_closed else "Open"

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

    def bot_liveness(self):
        """(state, info): STOPPED | ALIVE | HUNG.

        The old check ran `tasklist /FI "PID eq {pid}"` through a shell and substring-
        matched the output, so ANY process that inherited the dead bot's recycled PID made
        it look alive -- and the watchdog would then never restart a bot that had died.
        It also could not see a bot that was alive but wedged. Now: PID exists AND the
        image is python.exe AND the heartbeat is fresh. A live LOCK also counts as running.
        """
        return check_liveness(self.config, self.bot_status_file,
                              lock_path(self.config, self.log_dir))

    def is_bot_running(self):
        """Backwards-compatible boolean: hung still counts as RUNNING (do not double-start)."""
        state, _ = self.bot_liveness()
        return state != STOPPED

    # ------------------------------------------------------------------
    # PROCESS DISCOVERY
    #
    # The bot has a lock + heartbeat and is checked through modules.liveness. The Telegram
    # handler has neither, so it is found the only other honest way: a live python process
    # whose command line names its script. Never a bare PID -- PIDs are recycled.
    # ------------------------------------------------------------------
    def find_python_process(self, marker):
        """PID of a live python process whose command line contains `marker`, else None."""
        me = os.getpid()
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if p.info['pid'] == me:
                    continue
                if not (p.info['name'] or '').lower().startswith('python'):
                    continue
                if marker in ' '.join(p.info['cmdline'] or []):
                    return p.info['pid']
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return None

    # ------------------------------------------------------------------
    # DEAD-MAN'S SWITCH
    #
    # Every other alarm in this stack can only fire if something is still alive to fire it.
    # A dead-man switch inverts that: the watchdog must keep proving it is alive, and
    # SILENCE is the alarm. It therefore covers the states nothing else does -- machine
    # powered off, sat at the login screen after a reboot, watchdog killed, watchdog wedged.
    #
    # On 13/07 it was the ONLY thing that noticed the stack was gone. Everything else that
    # could have raised the alarm was inside the terminal window that died.
    #
    # The ping fires at the end of EVERY completed cycle, including out-of-trading-hours and
    # manual-stop cycles. That is deliberate: those are still cycles the watchdog is
    # consciously minding. If we only pinged while trading, the switch would fire every
    # weekend and be trained out of you as noise -- the classic way a dead-man switch dies.
    #
    # The URL is a secret (anyone holding it can forge liveness), so it lives in the
    # gitignored config.json as SYSTEM.deadman_url and is NEVER committed. Absent = off.
    # ------------------------------------------------------------------
    def deadman_ping(self):
        """GET the heartbeat URL. Failures are logged and swallowed -- a monitoring
        outage must never take down the thing it is monitoring."""
        if not self.deadman_url:
            return False
        try:
            r = requests.get(self.deadman_url, timeout=5)
            if r.status_code >= 300:
                self.log.warning(f"Dead-man ping returned HTTP {r.status_code}")
                return False
            self.last_deadman_ok = datetime.now()
            self.log.info("Dead-man ping OK")
            return True
        except Exception as e:
            self.log.warning(f"Dead-man ping FAILED (continuing): {e}")
            return False

    def startup_recovery_alert(self):
        """Announce that the watchdog has (re)started -- which, unattended, usually means
        the machine rebooted. Reports what it found so a reboot is never silent."""
        state, info = self.bot_liveness()
        label = {ALIVE: "running", HUNG: "running but HUNG", STOPPED: "stopped"}.get(state, "unknown")
        if state == STOPPED:
            label = "stopped by flag (will NOT auto-start)" if self.check_manual_stop_flag() \
                    else "stopped (watchdog will start it)"

        age = info.get('heartbeat_age')
        if age is None:
            age_txt = "none found (no prior heartbeat)"
        else:
            age_txt = f"{age:.0f}s ago"

        boot = ""
        try:
            import ctypes
            up_s = ctypes.windll.kernel32.GetTickCount64() / 1000.0
            boot = f"\n🖥 Machine up: {up_s/60:.0f} min (booted {datetime.now() - timedelta(seconds=up_s):%d/%m %H:%M})"
            if up_s < 600:
                boot += "\n<b>⚠️ Machine booted recently — this looks like a reboot.</b>"
        except Exception:
            pass

        paper = ""
        try:
            if self.config.get('SYSTEM', {}).get('paper_mode') is True:
                paper = "\n📝 Mode: PAPER (simulated)"
        except Exception:
            pass

        msg = (f"🔄 <b>Watchdog started</b> — machine may have rebooted\n\n"
               f"🤖 Bot: <b>{label}</b>\n"
               f"💓 Last bot heartbeat before now: {age_txt}"
               f"{paper}{boot}\n\n"
               f"⏰ {datetime.now().strftime('%d/%m/%Y %I:%M:%S %p')}")
        self.alert(msg)
        self.log.info(f"Recovery alert sent. Bot: {label}, last heartbeat: {age_txt}")

    def alert(self, text):
        """Fire a Telegram alert. Never let a notification failure kill the watchdog."""
        try:
            tg = self.config.get('TELEGRAM', {})
            if not tg.get('enabled', False) or not tg.get('bot_token'):
                return
            notifier = TelegramNotifier(tg['bot_token'], tg['chat_id'], enabled=True)
            notifier.send_message(text)
        except Exception as e:
            self.log.error("Could not send Telegram alert: "
                           f"{redact_token(e, self.config.get('TELEGRAM', {}).get('bot_token'))}")

    def kill_bot(self, pid):
        """Kill a hung bot. List form, no shell, PID forced to int."""
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=10, shell=False)
            self.log.info(f"Killed hung bot PID {pid}")
            return True
        except Exception as e:
            self.log.error(f"Failed to kill PID {pid}: {e}")
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

    def _paper_flag(self):
        """SAFETY: mirror SYSTEM.paper_mode into the relaunch command.

        Re-read from disk so a restart reflects the config the bot is about to load,
        not the copy cached when this watchdog started. Without this, a restart would
        drop a --paper that was only ever passed on the command line and go LIVE.
        """
        try:
            paper = self.load_config().get('SYSTEM', {}).get('paper_mode') is True
        except SystemExit:
            raise
        except Exception as e:
            self.log.warning(f"Could not re-read paper_mode ({e}); launching without --paper")
            return ''
        return ' --paper' if paper else ''

    # ------------------------------------------------------------------
    # LAUNCHING SUPERVISED PROCESSES
    #
    # Preferred: a tab in the shared Windows Terminal window, because a human needs to be
    # able to watch these. That window is allowed to be fragile -- if it dies, the watchdog
    # (which is NOT in it) notices within one cycle and rebuilds it.
    #
    # Fallback: if wt.exe cannot be resolved -- it is a Store execution alias under
    # %LOCALAPPDATA%\Microsoft\WindowsApps and is not guaranteed to be on PATH in every
    # launch context -- start the process windowless instead. A missing terminal must never
    # mean an unmanaged position. Both components log to files regardless, so nothing is
    # lost but the pretty tab.
    # ------------------------------------------------------------------
    def _launch(self, what, title, tab_color, console_color, command, bot_dir):
        wt = shutil.which('wt')
        if wt:
            try:
                subprocess.Popen(
                    [wt, '-w', '0', 'nt', '--title', title, '--tabColor', tab_color,
                     '-d', bot_dir, 'cmd', '/c', f'color {console_color} && {command}'],
                    cwd=bot_dir,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                self.log.info(f"{what} started as a Windows Terminal tab '{title}'")
                return True
            except Exception as e:
                self.log.warning(f"{what}: wt launch failed ({e}); falling back to windowless")

        else:
            self.log.warning(f"{what}: wt.exe not on PATH; falling back to windowless")

        try:
            subprocess.Popen(
                ['cmd', '/c', command],
                cwd=bot_dir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.log.info(f"{what} started windowless (no tab; see its log file)")
            return True
        except Exception as e:
            self.log.error(f"{what}: FAILED to start at all: {e}")
            return False

    def bot_dir(self):
        return os.path.dirname(os.path.abspath(self.config_file))

    def start_bot(self):
        """Start the bot"""
        cfg = os.path.basename(self.config_file)
        paper_flag = self._paper_flag()
        self.log.info(f"Starting bot with config: {self.config_file}")
        self.log.info(f"Launch mode: {'PAPER (--paper)' if paper_flag else 'LIVE (no --paper)'}")
        return self._launch(
            what='Bot',
            title=f"Fusion Sniper Bot - {self.config['BROKER']['symbol']}",
            tab_color='#00FF00', console_color='0A',
            command=f'python main_bot.py {cfg}{paper_flag}',
            bot_dir=self.bot_dir(),
        )

    def handler_running(self):
        return self.find_python_process(HANDLER_MARKER) is not None

    def start_handler(self):
        """Start the Telegram command handler.

        The handler used to be its own scheduled task, which meant NOTHING supervised it:
        the watchdog only ever watched the bot. It died with everything else on 13/07 and
        would have stayed dead until the next logon. It is now the watchdog's second child.
        """
        cfg = os.path.basename(self.config_file)
        self.log.info("Starting Telegram command handler")
        return self._launch(
            what='Telegram handler',
            title='Telegram Handler',
            tab_color='#0088FF', console_color='0B',
            command=f'python {HANDLER_SCRIPT} {cfg}',
            bot_dir=self.bot_dir(),
        )

    def supervise_handler(self):
        """Restart the handler if it is gone. Cheap, so it runs every cycle."""
        pid = self.find_python_process(HANDLER_MARKER)
        if pid:
            self.log.info(f"Telegram handler is running (PID {pid})")
            return

        starting_for = time.time() - self.handler_started_at
        if starting_for < HANDLER_START_GRACE:
            self.log.info(f"Telegram handler launched {starting_for:.0f}s ago, not yet visible "
                          f"-- waiting rather than starting a second one")
            return

        self.log.warning("Telegram handler not running -- restarting it")
        if self.start_handler():
            self.handler_started_at = time.time()

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
                self.log.info(f"Cleaned up {deleted_count} old cache files")
        except Exception as e:
            self.log.error(f"Error cleaning cache: {e}")

    def run(self):
        """Main watchdog loop"""
        self.log.info("Watchdog Monitor started")
        self.log.info(f"Monitoring: {self.config['BROKER']['symbol']}")
        self.log.info(f"Dead-man switch: "
                      f"{'ENABLED' if self.deadman_url else 'disabled (SYSTEM.deadman_url absent)'}")

        # Announce the (re)start BEFORE the first cycle: if the machine rebooted, this is
        # the message that tells you it happened.
        self.startup_recovery_alert()
        self.deadman_ping()          # resume pings immediately, don't wait a full cycle

        # Bring the handler up straight away, but the BOT only on the first post-grace cycle
        # (>=30s later). Both attach with `wt -w 0`, which means "use the most recent window
        # or make one" -- fire them together on a cold boot with no window yet and they can
        # each decide to create one. The grace period already separates them, so just let it.
        self.supervise_handler()

        # v5.0.0 (C2): the loop body is wrapped so a transient error logs and CONTINUES
        # instead of killing the watchdog. KeyboardInterrupt still exits cleanly.
        while True:
            cycle_ok = True
            try:
                # The handler is supervised in ALL cycles, including out-of-hours ones: it
                # serves /status and /start, which is exactly what you reach for when the
                # market is shut and you want to know where things stand.
                self.supervise_handler()

                # Check if within trading hours
                if not self.is_within_trading_hours():
                    self.log.info("Outside trading hours, sleeping...")
                    time.sleep(self.check_interval)
                    continue

                # Check for manual stop flag
                if self.check_manual_stop_flag():
                    self.log.info("Manual stop flag detected, not restarting")
                    time.sleep(self.check_interval)
                    continue

                # Grace period during startup to prevent duplicate launches
                time_since_startup = time.time() - self.startup_time
                if time_since_startup < self.startup_grace_period:
                    remaining = int(self.startup_grace_period - time_since_startup)
                    self.log.info(f"Startup grace period ({remaining}s remaining)...")
                    time.sleep(10)
                    continue

                # Check if bot is running (identity-verified + heartbeat)
                state, info = self.bot_liveness()
                bot_running = state != STOPPED
                bot_recently_started = self.is_bot_recently_started()

                # HUNG: the process is alive but its heartbeat has gone stale. This is the
                # case the old PID-only check could never see -- the bot sat there wedged
                # and the watchdog reported it healthy forever. Alert, kill, then let the
                # normal restart path bring it back.
                if state == HUNG and not bot_recently_started:
                    age = info.get('heartbeat_age')
                    age_txt = f"{age:.0f}s" if age is not None else "never written"
                    ts = datetime.now().strftime('%d/%m/%Y %I:%M:%S %p')
                    self.log.warning(f"BOT HUNG: PID {info.get('pid')} alive but heartbeat "
                                     f"{age_txt} old (max {info.get('max_age')}s). Killing and restarting.")
                    self.alert(
                        f"⚠️ <b>BOT HUNG</b> — {self.config['BROKER']['symbol']}\n\n"
                        f"PID {info.get('pid')} is alive but its heartbeat is <b>{age_txt}</b> old "
                        f"(max {info.get('max_age')}s).\n"
                        f"The process is wedged, not working. Killing it and restarting.\n\n"
                        f"⏰ {ts}")
                    self.kill_bot(info.get('pid'))
                    time.sleep(3)                 # let the OS reap it and the lock go stale
                    if self.start_bot():
                        self.log.info("Restarted after hang")
                    time.sleep(self.check_interval)
                    continue

                if not bot_running:
                    # Don't restart if bot just started (another instance may be launching)
                    if bot_recently_started:
                        self.log.info("Bot recently started, waiting for confirmation...")
                        time.sleep(10)
                        continue

                    # v5.0.0 (C2): restart whenever the bot is not running and no manual
                    # stop flag is present -- INCLUDING a cold start where we never saw it
                    # running. (Previously gated on self.last_bot_running, which meant a
                    # bot down at watchdog start was never relaunched.)
                    self.log.warning("Bot not running, starting...")
                    if self.start_bot():
                        time.sleep(10)
                        if self.is_bot_running():
                            self.log.info("Bot confirmed running")
                            self.last_bot_running = True
                        else:
                            self.log.warning("Bot may not have started successfully")
                else:
                    self.log.info(f"Bot is running (PID {info.get('pid')}, "
                                  f"heartbeat {info.get('heartbeat_age', 0):.0f}s old)")
                    self.last_bot_running = True

                # Cleanup old cache files
                self.cleanup_old_cache()

                time.sleep(self.check_interval)

            except KeyboardInterrupt:
                self.log.info("Watchdog stopped by user")
                break
            except Exception as e:
                # Log and keep monitoring rather than exiting the watchdog.
                cycle_ok = False
                self.log.error(f"Watchdog loop error (continuing): {e}", exc_info=True)
                time.sleep(self.check_interval)
            finally:
                # Ping only when the cycle COMPLETED. A cycle that threw is not proof the
                # watchdog is minding anything, so we stay silent and let the switch fire.
                # `continue` inside the try still reaches this finally -- which is what makes
                # the out-of-hours and manual-stop cycles ping too.
                if cycle_ok:
                    self.deadman_ping()


def main():
    """Entry point"""
    config_file = sys.argv[1] if len(sys.argv) > 1 else 'config.json'

    if not os.path.exists(config_file):
        setup_logging().error(f"Config file not found: {config_file}")
        return 1

    watchdog = WatchdogMonitor(config_file)
    watchdog.run()

    return 0


if __name__ == '__main__':
    sys.exit(main())
