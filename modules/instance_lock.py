"""Single-instance lock, so two bots can never trade the same symbol at once.

Nothing previously stopped a second bot starting: the watchdog could relaunch a bot that
was merely slow to write its status file, /start could fire twice, or an operator could
double-click the .bat. Two processes on the same symbol and magic number means two sets of
orders, two trailing stops fighting over the same position, and a daily-loss limit that
each instance believes it alone is enforcing.

The lock is acquired with O_CREAT | O_EXCL, which is atomic at the OS level -- two racing
processes cannot both succeed. A lock whose PID is no longer a live python.exe is STALE
(the bot was killed, or the box lost power) and is replaced; a lock whose PID is genuinely
alive means we exit rather than trade alongside it.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from modules.liveness import process_is_python


class AlreadyRunning(Exception):
    """Another live instance holds the lock."""

    def __init__(self, pid, path):
        self.pid = pid
        self.path = path
        super().__init__(f"another live instance (PID {pid}) holds {path}")


class InstanceLock:
    def __init__(self, path, logger=None, is_alive=process_is_python):
        self.path = Path(path)
        self.logger = logger
        self.is_alive = is_alive
        self.acquired = False

    def _read_pid(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return int(json.load(f).get("pid"))
        except Exception:
            return None            # unreadable/garbage lock == stale

    def _log(self, level, msg):
        if self.logger:
            getattr(self.logger, level)(msg)
        else:
            print(msg)

    def acquire(self, extra=None):
        """Take the lock, or raise AlreadyRunning. Replaces a stale lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"pid": os.getpid(), "started_at": datetime.now().isoformat()}
        if extra:
            payload.update(extra)

        for _ in range(2):         # at most one stale-lock replacement, then give up
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                pid = self._read_pid()
                if pid and self.is_alive(pid):
                    raise AlreadyRunning(pid, self.path)
                self._log("warning",
                          f"Stale lock {self.path.name} (PID {pid} is not a live python.exe) "
                          "-- the previous instance died without cleaning up. Replacing it.")
                try:
                    os.unlink(str(self.path))
                except FileNotFoundError:
                    pass           # someone else cleaned it up; loop and retry
                continue
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                self.acquired = True
                self._log("info", f"Instance lock acquired: {self.path.name} (PID {os.getpid()})")
                return self

        raise AlreadyRunning(self._read_pid(), self.path)

    def release(self):
        """Remove the lock, but ONLY if it is still ours -- never delete another process's."""
        if not self.acquired:
            return
        try:
            if self._read_pid() == os.getpid():
                os.unlink(str(self.path))
                self._log("info", f"Instance lock released: {self.path.name}")
        except FileNotFoundError:
            pass
        except Exception as e:
            self._log("error", f"Could not release instance lock: {e}")
        finally:
            self.acquired = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False
