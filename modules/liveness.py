"""Identity-verified liveness, shared by the bot, the watchdog and the Telegram handler.

The old check was `tasklist /FI "PID eq {pid}"` with shell=True and a substring match on
the output. That is wrong three ways:

  1. It matched ANY process with that PID. PIDs are recycled by the OS, so once the bot
     died, whatever process next inherited its PID -- a browser, a compiler, anything --
     made the bot look alive forever, and the watchdog would never restart it.
  2. The PID came from a JSON file and went into a shell string unquoted. A crafted or
     corrupt status file is command injection.
  3. A process can be alive and completely hung. Checking only the PID cannot tell a
     working bot from one wedged on a dead socket.

So liveness now requires ALL THREE of:
  - the PID exists,
  - its image name is python.exe (identity, not just a number), and
  - the heartbeat in bot_status.json is younger than 3x the loop interval.

PID alive + heartbeat stale  =>  HUNG (alert, then restart). Not the same as STOPPED.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

STOPPED = "stopped"
ALIVE = "alive"
HUNG = "hung"

HEARTBEAT_STALE_MULTIPLIER = 3


def redact_token(text, token=None):
    """Strip a Telegram bot token out of anything we are about to log.

    Exception text from `requests` routinely contains the full request URL, and the token
    is IN that URL -- so an unredacted traceback leaks the bot token into the log file.
    """
    s = str(text)
    if token:
        s = s.replace(str(token), "<BOT_TOKEN>")
    # Belt and braces: any 123456789:AA... shaped thing, even a token we weren't handed.
    # NO leading \b -- the token appears in URLs as ".../bot<token>/getUpdates", and there
    # is no word boundary between the 't' of "bot" and the token's first digit, so a \b
    # here would let the very case we care about slip through unredacted.
    return re.sub(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}", "<BOT_TOKEN>", s)


def process_is_python(pid, timeout=5) -> bool:
    """True only if `pid` exists AND its image is python.exe.

    PID is cast to int (so nothing from a JSON file can reach the command line as text)
    and the command is run in LIST form with shell=False -- no shell, nothing to inject.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FI", "IMAGENAME eq python.exe", "/NH"],
            capture_output=True, text=True, timeout=timeout, shell=False,
        )
    except Exception:
        return False
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        # "python.exe   12345 Console   1   30,000 K"
        if len(parts) >= 2 and parts[0].lower() == "python.exe" and parts[1] == str(pid):
            return True
    return False


def loop_interval_seconds(config) -> int:
    """The LONGEST the bot's loop can legitimately go between heartbeats.

    Not just main_loop_interval: the loop sleeps paused_loop_interval when a daily gate has
    paused it, and 60s when the market is closed. Taking the max of them all is what stops
    a healthy-but-idle bot being declared hung on a quiet Sunday.
    """
    s = config.get("SYSTEM", {}) if isinstance(config, dict) else {}
    return max(
        int(s.get("main_loop_interval", 30) or 30),
        int(s.get("paused_loop_interval", 120) or 120),
        int(s.get("idle_sleep_interval", 60) or 60),
        60,                                    # the closed-market branch sleeps 60s
    )


def heartbeat_max_age(config, multiplier=HEARTBEAT_STALE_MULTIPLIER) -> int:
    return multiplier * loop_interval_seconds(config)


def _read_json(path):
    try:
        p = Path(path)
        if not p.exists():
            return None
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def lock_path(config, log_dir="logs") -> Path:
    b = config.get("BROKER", {})
    return Path(log_dir) / f"bot_{b.get('symbol', 'UNKNOWN')}_{b.get('magic_number', 0)}.lock"


def check_liveness(config, status_file, lock_file=None, now=None):
    """-> (state, info). state is STOPPED | ALIVE | HUNG.

    The LOCK is authoritative for "is a process holding this bot's identity"; the STATUS
    file's heartbeat is authoritative for "is that process still doing work". A live lock
    means already-running even if the status file is missing entirely (e.g. the bot died
    before its first heartbeat), which is what stops /start and the watchdog racing a
    booting bot into a second instance.
    """
    now = now or datetime.now()
    lock_file = lock_file or lock_path(config)
    info = {"pid": None, "heartbeat_age": None,
            "max_age": heartbeat_max_age(config), "source": None}

    lock = _read_json(lock_file)
    status = _read_json(status_file)

    pid = None
    if lock and lock.get("pid"):
        pid, info["source"] = lock.get("pid"), "lock"
    elif status and status.get("pid"):
        pid, info["source"] = status.get("pid"), "status"
    info["pid"] = pid

    if not pid or not process_is_python(pid):
        return STOPPED, info

    hb = (status or {}).get("heartbeat")
    if not hb:
        # Holding the lock but has never written a heartbeat. Treat as HUNG rather than
        # STOPPED: something IS running under our identity and must not be duplicated.
        info["heartbeat_age"] = None
        return HUNG, info

    try:
        age = (now - datetime.fromisoformat(hb)).total_seconds()
    except Exception:
        return HUNG, info

    info["heartbeat_age"] = age
    if age > info["max_age"]:
        return HUNG, info
    return ALIVE, info
