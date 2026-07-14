# Fusion Sniper Bot (v5.0)

Automated MetaTrader 5 trading bot for **XAUUSD (gold)**, written in Python, running on Windows.

The bot connects to an MT5 terminal, evaluates a configurable strategy engine, manages risk,
filters high-impact economic news, and reports through Telegram. Around it sits an ops layer
built to survive an unattended reboot: a single-instance lock, a heartbeat, a headless
watchdog, a dead-man switch, and a scheduled-task startup chain.

> ### Status: PAPER ONLY. No demonstrated edge.
>
> The strategy has been backtested under realistic conditions (DST-correct clock, swap,
> commission, spread and slippage in the fill, live gates, news blackouts) and put through a
> **pre-registered** experiment. Both steps came back negative:
>
> - The M15 breakout entry does **not** beat entering at a random time in the trend direction
>   (permutation `P(random >= actual) = 0.42`).
> - The H4-trend + ATR-trailing wrapper — tested as a hypothesis in its own right, with the
>   acceptance criteria and the rejection branch written down *before* any variant was run —
>   **failed the in-sample eligibility gate on all three variants**. H1 was rejected, and the
>   out-of-sample window was left unspent.
>
> There is no evidence of an edge anywhere in this strategy: not in the signal, not in the
> wrapper. See [`docs/experiments/`](docs/experiments/). The bot therefore runs in **paper
> mode**, and the work has moved to the ops layer and to finding a hypothesis worth testing.
> Nothing here is claimed to be profitable.

> ### Credentials
>
> `config.json` holds broker credentials, the Telegram token and the dead-man URL. It is
> gitignored and must **never** be committed. Start from
> [`config.example.json`](config.example.json).

---

## Strategy engines

The active engine is selected by `STRATEGY.engine`. Two exist.

### `momentum` — the active engine

An H4-trend-filtered M15 breakout with a ratcheting ATR trailing exit. It lives in
[`modules/momentum_strategy.py`](modules/momentum_strategy.py), which has **no MetaTrader5
dependency** and is imported by *both* the live bot and the backtester — so the bot provably
makes the same decisions that were tested.

| | Rule | Config key |
|---|---|---|
| Trend filter | H4 close above its EMA(50) ⇒ longs allowed; below ⇒ shorts | `h4_ema` |
| Entry | M15 close breaks the prior 20-bar high/low, in the trend direction only | `breakout_lookback` |
| Stop | `1.5 × ATR(M15)`. **No fixed take-profit.** | `sl_atr_mult` |
| Exit | Ratcheting ATR trailing stop, `3.0 × ATR`, arming once price is `1.0 × ATR` in profit. Only ever moves favourably. | `trail_atr_mult`, `trail_activation_atr` |
| Session | Entries only between 07:00 and 18:00 UK. One position at a time. | `session_start_uk`, `session_end_uk` |
| Sizing | Risk-based: lots derived from the stop distance. `flat` (fixed GBP per trade, for backtest parity) or `percent_equity`. | `sizing_mode`, `risk_flat_gbp`, `risk_percent` |

No scalp mode, no breakeven, no fixed target — this engine ignores the legacy `TRADING`
stop/breakeven/trailing keys and the ATR scalp/volatility mode entirely.

### `smc` — retained, inactive, slated for retirement

The original engine: a CHoCH-style structure bias with Fair-Value-Gap rejection entries, plus
a legacy indicator stack (EMA, RSI, ADX, Stochastic, Bollinger). With `engine: "momentum"`,
**none of its signal logic runs.**

It has not been deleted because it is not yet safely deletable: `main_bot.py` still constructs
`FusionStrategy` unconditionally at startup, and `tools/backtest.py` and `tools/backtest_htf.py`
import it — and the momentum backtester depends on helpers from both. Retiring it is a
refactor with its own tests, not a cleanup, and it is queued as such. Its config sections
(`STRATEGY.SMC`, the indicator keys, `trend_filter`) stay until then.

---

## Paper / live safety model

The bot **refuses to start** if it cannot tell which mode it is in.

- `SYSTEM.paper_mode` must be present and a **JSON boolean**. Missing, or a string, or `null`
  ⇒ the bot writes a refusal to stderr and exits `2`. It will not infer a trading mode from an
  ambiguous config.
- **LIVE is only reachable through an explicit `"paper_mode": false`.**
- `--paper` on the command line forces paper on. Paper wins if *either* source asks for it, so
  a relaunch that drops the flag — a watchdog restart, a Telegram `/start` — cannot silently go
  live while the config says `true`.
- Startup logs a banner stating the effective mode **and its source**. If paper is active via
  the CLI flag only, it says so loudly, because that is the one configuration a restart could
  undo.
- Simulated fills are written to `trade_statistics_{symbol}_paper.json`, never the live file.
  The paper ledger models commission and swap, so paper P&L uses the same cost model as the
  backtest.

```bash
python main_bot.py config.json --paper      # simulated orders
python main_bot.py config.json              # mode comes from SYSTEM.paper_mode
```

---

## Risk and trade management

- Daily loss cap, daily profit target (pauses trading), max drawdown percent; weekly caps
  optional.
- Extreme-ATR entry skip (`atr_max_for_trading`).
- High-impact news and holiday blackout from the ForexFactory/faireconomy feed, parsed in UTC.
- Broker-correct execution: filling mode read from the symbol, volume normalised to the
  broker's step/min/max, SL/TP rounded to tick size, bounded retries on send, minimum stop
  distance floor.
- Entry ATR, position state and the cooldown clock persist across restarts in
  `logs/bot_state.json`.

The gate ablation in [`docs/experiments/bt_realism.txt`](docs/experiments/bt_realism.txt)
measures what each of these gates is actually worth — including finding that the trade cooldown
provably cannot bind on an M15 engine, which is why it is set to `0`.

---

## Operations

The bot is one process in a supervised stack. Full detail in
[`docs/REBOOT_RECOVERY.md`](docs/REBOOT_RECOVERY.md).

**Single-instance lock** — `logs/bot_{symbol}_{magic}.lock`. A second bot on the same
symbol+magic refuses to start. The lock is authoritative for identity; the heartbeat is
authoritative for whether that process is still working.

**Heartbeat + tombstone** — the bot writes `logs/bot_status.json` every loop. PID alive but
heartbeat stale ⇒ **hung**, which is not the same as stopped, and is restarted. On a clean
shutdown it first writes `logs/bot_last_seen.json` — the final heartbeat — so a graceful reboot
can still be told *how long the stack was down*.

**Watchdog** (`services/watchdog_monitor.py`) — runs under **`pythonw.exe`**: no console, so it
has no terminal host and cannot be closed by shutting a window. It supervises *both* the bot and
the Telegram handler, restarts either if it dies or hangs, and its log is its only voice.

**Dead-man switch** — the watchdog GETs a BetterStack heartbeat URL at the end of every cycle.
**Silence is the alarm**: the ping stops if the machine is off, sitting at the login screen, or
the watchdog is dead. It is the only layer that still works when everything else is dead. (A TCP
port monitor cannot do this — it reports "up" while the box sits at the login screen.)

**Startup chain** — auto-logon, then two scheduled tasks at logon: `FusionSniper-MT5` (the
terminal), then `FusionSniper-Watchdog`, which brings up the handler and the bot. From cold boot
to trading with no human present. `scripts/ops/verify_recovery.ps1` asserts every link.

**Update window** — Windows Update is pinned to install on **Saturday** (market closed) via
`scripts/ops/setup_update_window.ps1`. This makes a routine mid-session reboot unlikely; it does
not make reboots impossible, which is exactly why the recovery chain exists.

**Telegram** — `/start`, `/stop`, `/status`, `/positions`, `/daily`, `/health`, `/stats`,
`/news`, `/help`. Restricted to `authorized_user_ids`.

### Single instance

One symbol, one MT5 terminal, one config, one magic number, one Telegram bot. The engine is not
symbol-specific, and BTCUSD and EURUSD were run in the past, but they are a historical note —
everything current is XAUUSD.

Replicating the stack for a second symbol is **planned, not built**. The pieces that make it
possible are in place (per-instance lock keyed on symbol+magic, `{symbol}`-templated stats and
log paths, a portable per-instance MT5), but nothing here has been run as a fleet, and no
multi-instance orchestration exists.

---

## The lab

Offline tooling. None of it connects to the broker to trade.

| Tool | Purpose |
|---|---|
| `tools/export_data.py` | Pull XAUUSD M1/M15 bars from the MT5 terminal into `data/` |
| `tools/backtest_mom.py` | The momentum realism engine: DST-correct server clock, swap (incl. triple-Wednesday), commission, spread and slippage in the fill, short stops on the ask, gap-throughs at the bar open, live gates, news blackouts |
| `tools/backtest_trend_trail.py` | The pre-registered trend+trail experiment |
| `tools/backtest.py`, `tools/backtest_htf.py` | The older SMC/indicator backtester; still the source of shared helpers |
| `tools/sweep_mom_v2.py`, `tools/sweep_mom_filter.py` | Parameter sweeps |
| `tools/news_calendar.py` | NFP/CPI/FOMC blackouts from actual published release dates |
| `tools/btclock.py` | Broker-server clock and DST handling |
| `tools/verify_paper_mode.py` | Asserts the paper-mode guarantees hold |

Two disciplines are deliberate. **Shared code, not reimplemented code**:
`modules/momentum_strategy.py` and `modules/broker_costs.py` are imported by both the bot and
the backtester, so the lab cannot drift from what the bot runs. And **pre-registration**: a
hypothesis, its variants, its acceptance criteria and its rejection branch are committed
*before* the run, so a negative result cannot be quietly re-tuned into a positive one. The
rejection in `bt_trend_trail.txt` is that discipline working as intended.

```bash
python tools/export_data.py                 # pull bars from the logged-in terminal
python tools/backtest_mom.py --report       # full matrix + significance -> bt_realism.txt
python tools/backtest_mom.py --slip 2.0     # slippage sensitivity (multiple of spread)
```

The in-sample (2021–2023) and out-of-sample (2024–2026) windows are fixed constants in the
engine, not command-line flags — deliberately, so a window cannot be nudged after seeing a
result.

---

## Repository structure

```text
fusion_sniper_bot/
  main_bot.py                    # the bot: MT5 connection, engine selection, execution, gates
  config.example.json            # template; copy to config.json (gitignored)

  modules/
    momentum_strategy.py         # ACTIVE engine. Shared with the backtester. No MT5 import.
    strategy.py                  # FusionStrategy: SMC + indicators. Inactive; awaiting retirement.
    risk_manager.py              # position limits, drawdown, ATR stop sizing
    news_filter.py               # ForexFactory calendar blackouts (UTC)
    broker_costs.py              # swap + commission. Shared by bot and backtester.
    trade_statistics.py          # performance tracking to JSON (separate paper file)
    telegram_notifier.py         # outbound messages
    instance_lock.py             # single-instance lock (symbol + magic)
    liveness.py                  # lock/heartbeat/tombstone: STOPPED vs ALIVE vs HUNG
    atomic_json.py               # atomic writes; a reader never sees a half-written file

  services/
    watchdog_monitor.py          # headless (pythonw) supervisor + dead-man ping
    telegram_command_handler.py  # remote control

  scripts/ops/
    setup_autologon.ps1          # auto-logon (without it, a reboot stops at the lock screen)
    setup_startup_chain.ps1      # scheduled tasks: MT5 -> watchdog -> handler + bot
    setup_update_window.ps1      # pin Windows Update reboots to Saturday
    verify_recovery.ps1          # assert every link in the chain

  tools/                         # the lab (see above)

  docs/
    CODE_AUDIT.md                # v5.0 reliability/correctness audit
    REBOOT_RECOVERY.md           # the unattended-restart chain, layer by layer
    experiments/                 # the evidence: realism rebuild + pre-registered experiment

  logs/    # gitignored: daily logs, bot_status.json, bot_state.json, bot_last_seen.json, .lock
  cache/   # gitignored: news cache
  data/    # gitignored: exported bars
  MT5/     # gitignored: portable terminal (binaries + saved credentials)
```

`config.json`, `logs/`, `cache/`, `data/`, `MT5/` and `results*/` are gitignored.

---

## Configuration

Copy `config.example.json` to `config.json` and fill in the credentials. The example is the
authoritative key set — every section below exists in it.

| Section | What it controls |
|---|---|
| `BROKER` | Account, server, symbol, `magic_number`, path to the portable MT5 terminal |
| `TRADING` | Order execution (deviation, retries, commission per lot, min stop distance), trading hours, and the legacy stop/breakeven/trailing/volatility keys the momentum engine does not use |
| `RISK` | Daily loss cap, drawdown percent, max positions, optional weekly caps |
| `STRATEGY` | **`engine`** (`"momentum"` \| `"smc"`), the `momentum` block, and the retained SMC/indicator keys |
| `TELEGRAM` / `TELEGRAM_HANDLER` | Token, chat, `authorized_user_ids`, handler paths and thresholds |
| `NEWS_FILTER` | Feed URL, UTC parsing, blackout buffers, impact levels, caching |
| `STATISTICS` | Stats file path (`{symbol}`-templated), MAE/MFE and exit-reason tracking |
| `WATCHDOG` | Check interval, trading hours, cache retention |
| `SYSTEM` | **`paper_mode`** (required, boolean), `deadman_url` (secret), log paths and retention, loop intervals |

The engine block that actually runs:

```json
"STRATEGY": {
  "engine": "momentum",
  "momentum": {
    "h4_ema": 50,
    "breakout_lookback": 20,
    "atr_period": 14,
    "sl_atr_mult": 1.5,
    "trail_atr_mult": 3.0,
    "trail_activation_atr": 1.0,
    "session_start_uk": 7,
    "session_end_uk": 18,
    "sizing_mode": "flat",
    "risk_flat_gbp": 50,
    "risk_percent": 0.5,
    "gbpusd": 1.34,
    "price_digits": 2
  }
}
```

Change the parameters here, not the module defaults — the live bot builds the strategy from
config, and a module default that never applies live is exactly how the lab once ended up
validating a parameterisation the bot did not run.

---

## Requirements

- **Windows.** The MT5 Python API, the process control and the ops scripts are Windows-only.
- An MT5 terminal installed and logged in on the same machine.
- Python 3.11+.

```bash
pip install MetaTrader5 numpy pandas ta requests psutil tzdata
```

(`ta` is used only by the legacy SMC engine; `psutil` by the watchdog; `tzdata` supplies the
timezone database that `zoneinfo` uses for the news filter and the broker clock.)

---

## Running

The MT5 terminal must be open and logged in first.

In normal operation you do not launch anything by hand: the **scheduled-task startup chain**
brings up MT5 and the watchdog at logon, and the watchdog starts the handler and the bot. To run
the pieces manually:

```bash
python services/watchdog_monitor.py config.json     # supervises the two below
python services/telegram_command_handler.py config.json
python main_bot.py config.json --paper
```

Check the stack is healthy:

```powershell
.\scripts\ops\verify_recovery.ps1
```

---

## Development

- Develop in this clone; never edit code in a live runtime folder while a bot is running.
- Work on a branch, review the diff, then merge.
- The bot and the backtester must keep importing the *same* strategy and cost modules. If you
  find yourself reimplementing a rule in the lab, that is the bug.

---

## Safety and disclaimer

**This bot has no demonstrated edge.** Backtesting under realistic costs and a pre-registered
experiment both returned negative results, and it runs in paper mode for that reason. Do not
read the existence of this code as evidence that it makes money. It does not currently have a
reason to believe it would.

- Test on a demo account or in paper mode. `SYSTEM.paper_mode: false` is the only thing standing
  between this and real orders.
- Keep `magic_number` unique per instance so bots cannot act on each other's trades.
- Broker-side stop losses survive the bot dying; **trailing stops, daily caps and all monitoring
  do not.** That asymmetry is what the ops layer exists to address, and it is why silence is
  treated as an alarm.

Trading carries a significant risk of loss. This software is provided as is, with no guarantee of
profit and no warranty of any kind. Nothing here is financial advice. You are responsible for any
use of it on a live account.
