"""Broker clock for the backtests: server wall-clock <-> true UTC, honouring US DST.

The CSV bar timestamps are broker SERVER wall-clock. The old engines assumed a fixed
UTC+2, which is wrong for ~8 months of the year and silently shifted the trading
session by an hour.

ICMarkets-style servers are anchored so that 17:00 New York is always 00:00 server.
That makes the server offset a function of US Eastern DST, not a constant:

    server = America/New_York + 7h      (always)
      -> New York on EST (UTC-5): server = UTC+2   (winter)
      -> New York on EDT (UTC-4): server = UTC+3   (summer)

So we recover true UTC by subtracting 7h to get a naive New York wall-clock, localising
THAT to America/New_York (which applies the DST rules for the actual date), and
converting to UTC. New York's DST switch happens at 02:00 ET on a Sunday, which is
17:00 ET Saturday .. i.e. inside the weekend market close, so no bar ever lands on an
ambiguous or non-existent local time. The guards are set defensively regardless.

Confirmed against tools/diag_momentum_week.py, which observed the server running UTC+3
in June (summer).
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

try:
    from zoneinfo import ZoneInfo
except ImportError:                                    # pragma: no cover
    from backports.zoneinfo import ZoneInfo           # type: ignore

NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
UTC = timezone.utc

SERVER_MINUS_NY_HOURS = 7      # 17:00 NY == 00:00 server


def server_naive_to_ny(times) -> pd.DatetimeIndex:
    """Naive server wall-clock -> tz-aware America/New_York."""
    et_naive = pd.DatetimeIndex(times) - pd.Timedelta(hours=SERVER_MINUS_NY_HOURS)
    return et_naive.tz_localize(NY, ambiguous=False, nonexistent="shift_forward")


def server_clock(times):
    """Return (utc_epoch, uk_hour, offset_hours) for naive server timestamps.

    utc_epoch    : true UTC epoch seconds (NOT the server-epoch the CSV loader makes)
    uk_hour      : Europe/London local hour, the session gate's timebase
    offset_hours : server-minus-UTC for that bar (2 in winter, 3 in summer)
    """
    ny = server_naive_to_ny(times)
    utc = ny.tz_convert(UTC)
    uk_hour = ny.tz_convert(LONDON).hour.to_numpy()
    utc_epoch = (utc.astype("int64") // 10**9).to_numpy()
    server_epoch = (pd.DatetimeIndex(times).astype("int64") // 10**9).to_numpy()
    offset_hours = ((server_epoch - utc_epoch) // 3600).astype(int)
    return utc_epoch, uk_hour, offset_hours


def uk_day_bounds_to_utc_epoch(date_str: str, end: bool = False) -> int:
    """Window boundary as a timezone-EXPLICIT UTC epoch.

    Dates are given in the session's own timezone (Europe/London), not in naive local
    time. `end=True` returns the epoch of the START of the following day, so the end
    date is inclusive.
    """
    d = datetime.fromisoformat(date_str).replace(tzinfo=LONDON)
    if end:
        d = d + pd.Timedelta(days=1)
    return int(d.timestamp())


def utc_epoch_to_dt(ep) -> datetime:
    """UTC epoch -> aware UTC datetime (for trade logs)."""
    return datetime.fromtimestamp(int(ep), tz=UTC)
