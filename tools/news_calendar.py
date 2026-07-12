"""High-impact USD economic calendar for backtest news blackouts (2021-2026).

The live bot blocks NEW entries inside NEWS_FILTER.buffer_before/after_minutes of a
High-impact USD event (modules/news_filter.py, feed = ForexFactory weekly XML). That
feed only publishes the CURRENT week, so it cannot be replayed historically.

Event times are declared in US Eastern and converted per-date through zoneinfo, so the
UK/UTC time of each blackout is correct in every DST regime rather than pinned to a
single "13:30 UK" that is wrong for part of the year.

If DATES_ARE_APPROXIMATE is True, the dates below are a documented APPROXIMATION (the
standard release calendar), not a scraped historical feed -- results say so explicitly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

try:
    from zoneinfo import ZoneInfo
except ImportError:                                    # pragma: no cover
    from backports.zoneinfo import ZoneInfo           # type: ignore

NY = ZoneInfo("America/New_York")

# Release times in US EASTERN (converted per-date, so DST is handled).
NFP_TIME_ET = (8, 30)      # Employment Situation
CPI_TIME_ET = (8, 30)      # CPI
FOMC_TIME_ET = (14, 0)     # FOMC statement

# Blackout buffer, from config NEWS_FILTER.buffer_before/after_minutes
BUFFER_BEFORE_MIN = 30
BUFFER_AFTER_MIN = 30

# ---------------------------------------------------------------------------
# ACTUAL published release dates, 2021-01-01 .. 2026-06-30. Not rule-derived.
#
# Sources (compiled 2026-07-12):
#   FOMC : federalreserve.gov/monetarypolicy/fomccalendars.htm (2021-2027 on one page).
#          Decision day = 2nd day of each 2-day meeting; statement 14:00 ET throughout.
#   NFP  : BLS names every archived release with its real publication date
#   CPI    (news.release/archives/empsit_MMDDYYYY.htm, cpi_MMDDYYYY.htm). Enumerated via
#          the Wayback CDX index with filter=statuscode:200, cross-checked against the
#          BLS schedule pages and bls.gov/bls/2025-lapse-revised-release-dates.htm.
#
# Why a rule would be WRONG here (these are the dates a first-Friday/~13th
# approximation invents or misses):
#   - 2025 US government shutdown: the October Employment Situation and the October CPI
#     were CANCELLED and never published. September's jobs report slipped to Nov 20 and
#     November's to Dec 16; September CPI slipped to Oct 24 and November CPI to Dec 18.
#     2025 therefore has 11 NFP and 11 CPI prints, not 12.
#   - A 2026 lapse pushed the Jan-2026 jobs report to Wed 2026-02-11 and Jan-2026 CPI to
#     2026-02-13.
#   - 2026-05-08 NFP genuinely fell on the SECOND Friday.
# Excluded on purpose: the 2025-08-22 FOMC notation vote (a procedural inter-meeting
# vote, not a scheduled 14:00 ET rate decision).
# ---------------------------------------------------------------------------
DATES_ARE_APPROXIMATE = False

NFP_DATES: list[str] = [
    "2021-01-08", "2021-02-05", "2021-03-05", "2021-04-02", "2021-05-07", "2021-06-04",
    "2021-07-02", "2021-08-06", "2021-09-03", "2021-10-08", "2021-11-05", "2021-12-03",
    "2022-01-07", "2022-02-04", "2022-03-04", "2022-04-01", "2022-05-06", "2022-06-03",
    "2022-07-08", "2022-08-05", "2022-09-02", "2022-10-07", "2022-11-04", "2022-12-02",
    "2023-01-06", "2023-02-03", "2023-03-10", "2023-04-07", "2023-05-05", "2023-06-02",
    "2023-07-07", "2023-08-04", "2023-09-01", "2023-10-06", "2023-11-03", "2023-12-08",
    "2024-01-05", "2024-02-02", "2024-03-08", "2024-04-05", "2024-05-03", "2024-06-07",
    "2024-07-05", "2024-08-02", "2024-09-06", "2024-10-04", "2024-11-01", "2024-12-06",
    # 2025: 11 prints. Oct Employment Situation CANCELLED (shutdown).
    "2025-01-10", "2025-02-07", "2025-03-07", "2025-04-04", "2025-05-02", "2025-06-06",
    "2025-07-03", "2025-08-01", "2025-09-05", "2025-11-20", "2025-12-16",
    "2026-01-09", "2026-02-11", "2026-03-06", "2026-04-03", "2026-05-08", "2026-06-05",
]

CPI_DATES: list[str] = [
    "2021-01-13", "2021-02-10", "2021-03-10", "2021-04-13", "2021-05-12", "2021-06-10",
    "2021-07-13", "2021-08-11", "2021-09-14", "2021-10-13", "2021-11-10", "2021-12-10",
    "2022-01-12", "2022-02-10", "2022-03-10", "2022-04-12", "2022-05-11", "2022-06-10",
    "2022-07-13", "2022-08-10", "2022-09-13", "2022-10-13", "2022-11-10", "2022-12-13",
    "2023-01-12", "2023-02-14", "2023-03-14", "2023-04-12", "2023-05-10", "2023-06-13",
    "2023-07-12", "2023-08-10", "2023-09-13", "2023-10-12", "2023-11-14", "2023-12-12",
    "2024-01-11", "2024-02-13", "2024-03-12", "2024-04-10", "2024-05-15", "2024-06-12",
    "2024-07-11", "2024-08-14", "2024-09-11", "2024-10-10", "2024-11-13", "2024-12-11",
    # 2025: 11 prints. Oct CPI CANCELLED (shutdown; data not collectable retroactively).
    "2025-01-15", "2025-02-12", "2025-03-12", "2025-04-10", "2025-05-13", "2025-06-11",
    "2025-07-15", "2025-08-12", "2025-09-11", "2025-10-24", "2025-12-18",
    "2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10", "2026-05-12", "2026-06-10",
]

FOMC_DATES: list[str] = [
    "2021-01-27", "2021-03-17", "2021-04-28", "2021-06-16",
    "2021-07-28", "2021-09-22", "2021-11-03", "2021-12-15",
    "2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15",
    "2022-07-27", "2022-09-21", "2022-11-02", "2022-12-14",
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14",
    "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12",
    "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
]


def _first_friday(y, m):
    d = datetime(y, m, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def _approx_dates(start_year=2021, end_year=2026, end_month=6):
    """Documented approximation, used when no historical feed is obtainable.

    NFP  : first Friday of the month (the BLS norm; real releases deviate occasionally)
    CPI  : ~13th of the month, nearest weekday (the BLS norm is the 10th-15th)
    FOMC : the 8 scheduled meetings/yr are NOT derivable from a rule, so we approximate
           with the usual cadence (mid/late Jan, Mar, May, Jun, Jul/Aug, Sep, Nov, Dec).
    """
    nfp, cpi = [], []
    for y in range(start_year, end_year + 1):
        for m in range(1, 13):
            if y == end_year and m > end_month:
                break
            nfp.append(_first_friday(y, m).strftime("%Y-%m-%d"))
            c = datetime(y, m, 13)
            while c.weekday() >= 5:
                c += timedelta(days=1)
            cpi.append(c.strftime("%Y-%m-%d"))
    return nfp, cpi


def _event_epoch(date_str, hhmm):
    h, mnt = hhmm
    dt = datetime.fromisoformat(date_str).replace(hour=h, minute=mnt, tzinfo=NY)
    return int(dt.timestamp())


def build_blackouts(before_min=BUFFER_BEFORE_MIN, after_min=BUFFER_AFTER_MIN) -> np.ndarray:
    """Sorted (start, end) UTC-epoch blackout windows."""
    nfp, cpi, fomc = NFP_DATES, CPI_DATES, FOMC_DATES
    if not nfp or not cpi:
        a_nfp, a_cpi = _approx_dates()
        nfp = nfp or a_nfp
        cpi = cpi or a_cpi
    rows = []
    for ds, t in ((nfp, NFP_TIME_ET), (cpi, CPI_TIME_ET), (fomc, FOMC_TIME_ET)):
        for d in ds:
            ep = _event_epoch(d, t)
            rows.append((ep - before_min * 60, ep + after_min * 60))
    if not rows:
        return np.zeros((0, 2), dtype=np.int64)
    arr = np.array(sorted(rows), dtype=np.int64)
    return arr


def _note():
    n_nfp = len(NFP_DATES) or len(_approx_dates()[0])
    n_cpi = len(CPI_DATES) or len(_approx_dates()[1])
    n_fomc = len(FOMC_DATES)
    src = ("APPROXIMATED (no downloadable historical feed obtained)"
           if DATES_ARE_APPROXIMATE else
           "ACTUAL published release dates (federalreserve.gov FOMC calendar; BLS release")
    lines = [
        f"source: {src}",
    ]
    if not DATES_ARE_APPROXIMATE:
        lines.append("        archives enumerated via the Wayback CDX index, cross-checked against the")
        lines.append("        BLS schedule + 2025 lapse-revised-release-dates pages)")
    lines += [
        f"events: NFP {n_nfp}, CPI {n_cpi}, FOMC {n_fomc}",
        f"window: -{BUFFER_BEFORE_MIN}min / +{BUFFER_AFTER_MIN}min around each release "
        f"(config NEWS_FILTER buffers)",
        "times : NFP/CPI 08:30 US-Eastern, FOMC 14:00 US-Eastern, converted per-date via",
        "        zoneinfo (so the UK clock time of each blackout is right in both DST regimes)",
    ]
    if DATES_ARE_APPROXIMATE:
        lines.append("CAVEAT: NFP approximated as the first Friday and CPI as ~the 13th. Real")
        lines.append("        releases deviate from that rule, so individual blackouts can be")
        lines.append("        misplaced by a day. Treat the news gate's effect as indicative.")
    else:
        lines.append("note  : reflects the 2025 shutdown (Oct NFP + Oct CPI were CANCELLED and are")
        lines.append("        correctly absent) and the 2026 lapse (Jan-26 jobs report -> Wed 11 Feb).")
    return "\n".join(lines)


CALENDAR_NOTE = _note()
