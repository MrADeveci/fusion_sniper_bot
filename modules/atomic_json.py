"""Crash-safe JSON persistence.

The bot's state and statistics files were written with a plain open(..., 'w'), which
truncates the file before the new content lands. A crash, a kill, or a power loss during
that window leaves a truncated or empty file -- and the loader then swallowed the error
and silently started from scratch, losing the state it was supposed to protect.

write_json_atomic()   : write to a temp file in the same directory, flush + fsync, then
                        os.replace() onto the target. os.replace is atomic on Windows and
                        POSIX, so a reader either sees the whole old file or the whole new
                        one -- never a half-written one.
read_json_quarantine(): on a corrupt/unreadable file, move it aside with a timestamp
                        suffix and return None, so the damage is preserved for inspection
                        instead of being overwritten by the next save.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


def write_json_atomic(path, data, indent=2):
    """Write JSON to `path` atomically. Returns True on success."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = None
    try:
        # NamedTemporaryFile in the SAME directory: os.replace across filesystems fails.
        with tempfile.NamedTemporaryFile(
            mode="w", dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp",
            delete=False, encoding="utf-8",
        ) as tf:
            tmp_name = tf.name
            json.dump(data, tf, indent=indent, default=str)
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(tmp_name, str(p))      # atomic
        tmp_name = None
        return True
    finally:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)       # never leave temp turds behind
            except OSError:
                pass


def read_json_quarantine(path, logger=None):
    """Read JSON. On corruption, rename the file aside and return None.

    Returns (data, quarantined_path_or_None). data is None if the file is missing or was
    quarantined. A MISSING file is not an error -- it is a first run.
    """
    p = Path(path)
    if not p.exists():
        return None, None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"expected a JSON object, got {type(data).__name__}")
        return data, None
    except Exception as e:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bad = p.with_name(f"{p.name}.corrupt_{stamp}")
        try:
            os.replace(str(p), str(bad))
        except OSError:
            bad = None
        msg = (f"CORRUPT {p.name}: {e}. "
               + (f"Moved aside to {bad.name}; starting fresh." if bad
                  else "Could NOT move it aside."))
        if logger:
            logger.error(msg)
        else:
            print(msg)
        return None, bad
