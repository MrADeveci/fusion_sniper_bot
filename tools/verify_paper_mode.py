"""Safety check: PAPER/LIVE resolution, hard-refuse, and relaunch passthrough.

Asserts the load-bearing invariant: LIVE is reachable ONLY via an explicit
SYSTEM.paper_mode: false with no --paper flag. Every ambiguous config (key absent,
or a value that is not a JSON boolean) must refuse to start with exit code 2.

Constructs no bot and touches no broker -- FusionSniperBot.__init__ is bypassed, so
MT5 is never initialised. Run: python tools/verify_paper_mode.py
"""
import contextlib
import importlib.util
import io
import json
import logging
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

logging.basicConfig(level=logging.WARNING, format="      %(levelname)s | %(message)s")

spec = importlib.util.spec_from_file_location("mb", os.path.join(ROOT, "main_bot.py"))
mb = importlib.util.module_from_spec(spec)
sys.modules["mb"] = mb
spec.loader.exec_module(mb)          # __main__ guard => no bot is constructed
Bot = mb.FusionSniperBot

PAPER, LIVE, REFUSE = "PAPER", "LIVE", "REFUSE"

CASES = [
    # label,                        SYSTEM block,            --paper, expected
    ("paper_mode=true   + no CLI ", {"paper_mode": True},     False, PAPER),
    ("paper_mode=true   + --paper", {"paper_mode": True},     True,  PAPER),
    ("paper_mode=false  + --paper", {"paper_mode": False},    True,  PAPER),   # CLI-only paper
    ("paper_mode=false  + no CLI ", {"paper_mode": False},    False, LIVE),    # ONLY path to LIVE
    ("KEY ABSENT        + no CLI ", {},                       False, REFUSE),
    ("KEY ABSENT        + --paper", {},                       True,  REFUSE),
    ('paper_mode="true" (string) ', {"paper_mode": "true"},   False, REFUSE),
    ('paper_mode="false"(string) ', {"paper_mode": "false"},  False, REFUSE),
    ("paper_mode=0      (int)    ", {"paper_mode": 0},        False, REFUSE),
    ("paper_mode=null            ", {"paper_mode": None},     False, REFUSE),
    ("SYSTEM block missing       ", None,                     False, REFUSE),
]

fails = []

print("=" * 72)
print("A) main_bot: mode resolution + hard-refuse on an ambiguous config")
print("=" * 72)
for label, sysblock, cli, expect in CASES:
    bot = object.__new__(Bot)                  # bypass __init__ (it would need MT5)
    bot.config = {} if sysblock is None else {"SYSTEM": sysblock}
    bot.config_file = "config.json"
    bot.logger = logging.getLogger("bot")

    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            bot._resolve_paper_mode(cli)
        got, code = (PAPER if bot.paper_mode else LIVE), None
    except SystemExit as e:
        got, code = REFUSE, e.code

    ok = (got == expect) and (code == 2 if expect is REFUSE else True)
    fails.append(label) if not ok else None
    detail = f" (exit {code})" if got is REFUSE else ""
    print(f"  {label} -> {got}{detail}  [{'PASS' if ok else 'FAIL: expected ' + expect}]")

# The invariant this whole module exists to protect.
live_paths = [c[0] for c in CASES if c[3] is LIVE]
assert live_paths == ["paper_mode=false  + no CLI "], f"LIVE reachable via {live_paths}"
print("\n  INVARIANT OK: LIVE only via explicit paper_mode:false and no --paper")

print("\n" + "=" * 72)
print("B) relaunch passthrough (watchdog start_bot + telegram /start)")
print("=" * 72)
from services.telegram_command_handler import TelegramCommandHandler   # noqa: E402
from services.watchdog_monitor import WatchdogMonitor                  # noqa: E402

for label, sysblock, expect_flag in [
    ("paper_mode=true ", {"paper_mode": True},   " --paper"),
    ("paper_mode=false", {"paper_mode": False},  ""),
    ("KEY ABSENT      ", {},                     ""),          # bot then refuses at startup
    ('"true" (string) ', {"paper_mode": "true"}, ""),          # non-bool: bot refuses too
]:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"BROKER": {"symbol": "XAUUSD"}, "SYSTEM": sysblock}, f)

    wd = object.__new__(WatchdogMonitor)
    wd.config_file = path
    tg = object.__new__(TelegramCommandHandler)
    tg.config_file = path
    tg.logger = logging.getLogger("tg")

    wflag, tflag = wd._paper_flag(), tg._paper_flag()
    ok = wflag == expect_flag == tflag
    fails.append(label) if not ok else None
    print(f"  {label}  watchdog={wflag!r:11} telegram={tflag!r:11} [{'PASS' if ok else 'FAIL'}]")
    os.unlink(path)

print("\n" + "=" * 72)
print("RESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
