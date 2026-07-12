"""One-shot migration: pull PAPER trades out of the LIVE statistics file.

Paper mode was writing simulated trades into logs/trade_statistics_{symbol}.json, the same
file the live bot uses. The live win rate, profit factor and history are therefore polluted
by trades that never happened. Simulated tickets are >= 90000000 (PAPER_TICKET_BASE), so
they can be identified exactly.

This moves them to logs/trade_statistics_{symbol}_paper.json and rebuilds BOTH files'
aggregates by replaying each trade through TradeStatistics.update_overall_stats -- the
bot's own aggregation code -- so the rebuilt numbers cannot drift from how the bot computes
them. The original file is backed up first.

Idempotent: running it twice is a no-op (the second run finds no paper trades to move).

Run: python tools/migrate_paper_stats.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = r"C:\fusion_sniper_bot"
sys.path.insert(0, ROOT)
from modules.atomic_json import write_json_atomic          # noqa: E402
from modules.trade_statistics import TradeStatistics       # noqa: E402

PAPER_TICKET_BASE = 90000000


def is_paper(trade):
    t = trade.get("ticket")
    return isinstance(t, int) and t >= PAPER_TICKET_BASE


def rebuild(config, trades, paper):
    """Replay trades through the bot's own aggregator -> a consistent stats dict."""
    ts = TradeStatistics(config, paper=paper)
    ts.stats = ts.create_new_stats()
    for tr in trades:
        ts.update_overall_stats(tr)
    ts.stats["trade_history"] = list(trades)
    return ts.stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(os.path.join(ROOT, "config.json")) as f:
        config = json.load(f)
    symbol = config["BROKER"]["symbol"]

    live_path = Path(ROOT) / "logs" / f"trade_statistics_{symbol}.json"
    paper_path = Path(ROOT) / "logs" / f"trade_statistics_{symbol}_paper.json"

    if not live_path.exists():
        print(f"No live stats file at {live_path}; nothing to migrate.")
        return

    with open(live_path) as f:
        live = json.load(f)
    history = live.get("trade_history", [])

    paper_trades = [t for t in history if is_paper(t)]
    live_trades = [t for t in history if not is_paper(t)]

    print(f"{live_path.name}: {len(history)} trades "
          f"-> {len(paper_trades)} PAPER (ticket >= {PAPER_TICKET_BASE}), "
          f"{len(live_trades)} live")
    if paper_trades:
        tix = sorted({t['ticket'] for t in paper_trades})
        dupes = len(paper_trades) - len(tix)
        print(f"  paper tickets: {tix}"
              + (f"   ({dupes} RECYCLED ticket(s) -- the restart bug this release fixes)"
                 if dupes else ""))
        pnl = sum(float(t.get('profit') or 0) for t in paper_trades)
        print(f"  paper P&L being moved out of the live file: {pnl:+.2f}")

    if not paper_trades:
        print("Nothing to migrate (already clean).")
        return

    # merge with any paper trades already recorded
    existing_paper = []
    if paper_path.exists():
        with open(paper_path) as f:
            existing_paper = json.load(f).get("trade_history", [])
        print(f"  existing paper file has {len(existing_paper)} trade(s); merging")

    merged_paper = existing_paper + paper_trades
    new_paper = rebuild(config, merged_paper, paper=True)
    new_live = rebuild(config, live_trades, paper=False)

    print(f"\n  -> {paper_path.name}: {new_paper['total_trades']} trades, "
          f"win rate {new_paper['win_rate']:.1f}%")
    print(f"  -> {live_path.name}: {new_live['total_trades']} trades "
          f"(live-only; was {live.get('total_trades', 0)})")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = live_path.with_name(f"{live_path.name}.premigration_{stamp}")
    shutil.copy2(live_path, backup)
    print(f"\n  backed up original -> {backup.name}")

    write_json_atomic(paper_path, new_paper)
    write_json_atomic(live_path, new_live)
    print("  written atomically. Done.")


if __name__ == "__main__":
    main()
