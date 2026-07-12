"""Broker cost model (swap + commission), shared by the live paper ledger and the lab.

Single source of truth so paper-mode "net" means the same thing as backtest "net". The
backtester (tools/backtest_mom.py) and main_bot's paper ledger both import this, rather
than each carrying their own copy of the financing arithmetic.

SWAP. XAUUSD on this broker reports swap_mode = 1 (SYMBOL_SWAP_MODE_POINTS), so swap_long
/ swap_short are quoted in POINTS per lot per night:

    USD per lot per night = swap_points * point * contract_size

One rollover is charged per SERVER midnight the position is held across. No rollover is
charged on Saturday or Sunday -- weekend financing is exactly what the broker's triple-swap
day exists to collect, so charging the weekend too would double-count it. On the broker's
triple-swap day (symbol_info.swap_rollover3days, an ENUM_DAY_OF_WEEK) the rollover counts
three times.

Timestamps MUST be SERVER epochs (the clock MT5 quotes bars and ticks in), not UTC -- the
midnights being counted are the server's, and the server is not UTC.
"""
from __future__ import annotations

from datetime import datetime

# ENUM_DAY_OF_WEEK is Sunday=0..Saturday=6; Python's weekday() is Monday=0..Sunday=6.
_MT5_DOW_TO_PY = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}

SWAP_MODE_POINTS = 1


def triple_swap_weekday(swap_rollover3days) -> int:
    """symbol_info.swap_rollover3days (ENUM_DAY_OF_WEEK) -> Python weekday(). Default Wed."""
    try:
        return _MT5_DOW_TO_PY[int(swap_rollover3days)]
    except (KeyError, TypeError, ValueError):
        return 2      # Wednesday


def rollover_nights(entry_epoch_srv, exit_epoch_srv, triple_py_dow=2):
    """(single_nights, triple_nights) for the SERVER midnights crossed. Sat/Sun excluded."""
    d0 = int(entry_epoch_srv) // 86400
    d1 = int(exit_epoch_srv) // 86400
    singles = triples = 0
    for d in range(d0 + 1, d1 + 1):
        wd = datetime.utcfromtimestamp(d * 86400).weekday()   # weekday of the server date
        if wd >= 5:                                           # Sat/Sun: no rollover
            continue
        if wd == triple_py_dow:
            triples += 1
        else:
            singles += 1
    return singles, triples


def swap_cost(direction, lots, entry_epoch_srv, exit_epoch_srv,
              swap_long_pts, swap_short_pts, point=0.01, contract_size=100.0,
              fx_rate=1.0, swap_rollover3days=3):
    """Swap for one position, in the ACCOUNT currency.

    direction : "BUY" | "SELL"
    fx_rate   : profit-currency -> account-currency divisor (GBPUSD for USD profit, GBP
                account). Pass 1.0 if the profit currency IS the account currency.
    Returns (amount, single_nights, triple_nights). Negative = a cost.
    """
    n1, n3 = rollover_nights(entry_epoch_srv, exit_epoch_srv,
                             triple_swap_weekday(swap_rollover3days))
    if n1 == 0 and n3 == 0:
        return 0.0, 0, 0
    pts = float(swap_long_pts) if direction == "BUY" else float(swap_short_pts)
    per_lot = pts * point * contract_size            # profit-currency per lot per night
    amount = per_lot * float(lots) * (n1 + 3 * n3)
    return amount / float(fx_rate or 1.0), n1, n3


def commission_cost(lots, commission_per_lot):
    """Round-turn commission, in the account currency. Always a cost (>= 0 returned as
    the amount to SUBTRACT)."""
    return abs(float(commission_per_lot)) * float(lots)
