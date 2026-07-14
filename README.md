# Fusion Sniper Bot (v5.0)

Automated MetaTrader 5 trading bot written in Python.

The bot runs on Windows, connects directly to an MT5 terminal, evaluates a configurable
strategy, manages risk, filters high impact economic news, and sends Telegram notifications
and health updates.

The active focus is **XAUUSD (gold) on IC Markets**. The engine itself is symbol agnostic and
has also been run on BTCUSD and EURUSD, but current development is gold only.

> **Important: credentials**
>
> `config.json` holds live broker credentials and Telegram tokens. It must **never** be
> committed. It is listed in `.gitignore`. Keep your real `config.json` local and use the
> trimmed example in this document as a starting point, with placeholder values.

---

## Status and direction

v5.0 is a reliability and correctness overhaul, not a strategy change. The strategy's edge is
still being validated using an offline backtester and a paper trading mode. Run the bot on a
demo account or in paper mode before any live use.

This software is **not** claimed to be profitable or production proven. See the disclaimer at
the end.

---

## Strategy

**Live mode is SMC only** (`smc_only`):

- A CHoCH style structure bias computed on the **M15** bias timeframe.
- Fair Value Gap (FVG) rejection entries on the **M1** entry timeframe.
- ATR taken from **M15** for stop and target sizing.
- Entries require the signal direction to match the structure bias (v5.0 strict bias direction
  match): a buy only when the bias is bullish, a sell only when bearish.

The legacy indicator stack (EMA, RSI, ADX, Stochastic, Bollinger Bands) is retained as a
fallback. It is used only when SMC is disabled and by the backtester. It is **not** used in the
live `smc_only` configuration.

---

## Risk and trade management

- ATR based stop loss and take profit.
- Smart breakeven (move the stop to lock a small profit once price advances).
- Chandelier style trailing stop.
- Optional scalp/volatility mode driven by ATR.
- Minimum stop distance floor (v5.0) so a very small ATR cannot create an over tight stop.
- Daily profit target with an automatic pause once reached.
- Daily and weekly loss caps, plus a maximum drawdown limit.
- High impact economic news and holiday filter using the ForexFactory/faireconomy feed.

---

## What is new in v5.0

- **Connection resilience:** MT5 connection loss is detected, with automatic reconnect and a
  Telegram alert.
- **Loop recovery:** the main loop recovers from transient errors instead of exiting. The
  watchdog now restarts the bot from a cold start and survives its own errors.
- **Robust order execution:** filling mode is selected from the symbol, volume is normalised to
  the broker's step/min/max, SL/TP are rounded to tick size, sends use bounded retries, and the
  deviation and order comment are read from config.
- **No silent stop failures:** stop loss, breakeven and trailing modifications now log failures
  and retry rather than failing silently.
- **State persistence:** entry ATR, the breakeven flag and the cooldown clock persist across
  restarts in `logs/bot_state.json`.
- **News timezone fix:** the ForexFactory/faireconomy feed is published in UTC, and event times
  are now parsed and compared in UTC.
- **Reporting and config hygiene:** a single authoritative daily loss check, per trade reporting
  now net of commission and swap, config keys aligned so documented settings take effect, and all
  version strings unified to v5.0.
- **Paper mode:** a new dry run mode (see below).

---

## Paper mode

Paper mode runs the full live loop, connection, signal evaluation and position management, but
simulates orders instead of sending them, so the bot can be shaken down against a live connection
with no money at risk.

Enable it in either of two ways:

- Set `SYSTEM.paper_mode: true` in `config.json`, or
- Pass the `--paper` flag on the command line.

```bash
python main_bot.py config.json --paper
```

Startup logs and a Telegram notice make paper mode unmistakable. Commission is not modelled in
paper P&L, so paper results read slightly optimistic.

---

## Backtesting

The bot ships with offline tooling for measuring expectancy and running out of sample
(walk forward) tests. None of this connects to the broker for trading.

- `tools/export_data.py` pulls historical XAUUSD M1 and M15 bars from the MT5 terminal into
  `data/` (gitignored).
- `tools/backtest.py` is an offline backtester that **reuses** the real strategy and risk modules
  and mirrors the live gating and exits (including the strict bias direction match and the
  minimum stop distance floor).

```bash
# 1) Export bars from the running, logged in MT5 terminal
python tools/export_data.py

# 2) Sanity check the engine over a short window
python tools/backtest.py --validate

# 3) A dated window, current live (smc_only) config
python tools/backtest.py --start 2025-07-01 --end 2026-06-05

# 4) Edge experiments
python tools/backtest.py --start 2025-07-01 --end 2026-06-05 --no-scalp
python tools/backtest.py --start 2025-07-01 --end 2026-06-05 --no-scalp --no-caps
```

`--no-scalp` removes the scalp quick profit cap, `--no-caps` removes the daily and weekly pause,
and `--start`/`--end` set the window. Results are written to a `results/` folder (gitignored) and
a gross and net summary is printed.

---

## Repository structure

```text
fusion_sniper_bot/
  main_bot.py
  config.json              # local only, gitignored
  README.md

  modules/
    strategy.py            # FusionStrategy: SMC + fallback indicator stack
    risk_manager.py        # RiskManager: position limits, drawdown, stop sizing
    news_filter.py         # EconomicNewsFilter: calendar based trading blocks
    telegram_notifier.py   # TelegramNotifier: outbound messages
    trade_statistics.py    # TradeStatistics: performance tracking to JSON

  services/
    telegram_command_handler.py  # remote control and status commands
    watchdog_monitor.py          # keeps the bot running, cleans stale cache

  tools/
    export_data.py         # pull MT5 history to data/
    backtest.py            # offline backtester (reuses strategy + risk)

  docs/
    CODE_AUDIT.md          # v5.0 reliability/correctness audit
    REBOOT_RECOVERY.md     # unattended restart: auto-logon -> tasks -> watchdog -> bot
    experiments/           # backtest evidence for why this bot is not live yet

  logs/                    # gitignored: logs, bot_status.json, bot_state.json,
                           #             bot_last_seen.json (last heartbeat before a clean stop)
  cache/                   # gitignored: news cache
  data/                    # gitignored: exported bars
```

`config.json`, `logs/`, `cache/`, `data/` and `results/` are gitignored.

---

## Configuration

Settings live in `config.json` alongside `main_bot.py`. The full key set is extensive; below is a
trimmed, illustrative example showing the main sections and the new v5.0 keys. Replace every
`xxxx` placeholder with your own values and never commit the real file.

```json
{
  "_comment": "Fusion Sniper Trading Bot - v5.0.0",

  "BROKER": {
    "account": xxxxxxxx,
    "password": "xxxxxxxx",
    "server": "ICMarketsSC-MT5-4",
    "symbol": "XAUUSD",
    "magic_number": xxxxxx,
    "mt5_path": "C:\\fs_live_xauusd_xxxxxxxx\\MT5\\terminal64.exe",
    "portable": true,
    "broker_timezone_offset": 2
  },

  "TRADING": {
    "timeframe": "M15",
    "entry_timeframe": "M1",
    "bias_timeframe": "M15",
    "atr_timeframe": "M15",
    "lot_size": 0.20,
    "max_positions": 2,

    "use_atr_based_stops": true,
    "stop_loss_atr_multiple": 0.4,
    "take_profit_atr_multiple": 2.0,

    "use_smart_breakeven": true,
    "breakeven_profit_multiple": 0.6,
    "breakeven_lock_profit_multiple": 0.3,

    "use_trailing_stop": true,
    "trailing_stop_type": "chandelier",
    "trailing_stop_atr_multiple": 2.0,
    "min_profit_for_trail_activation": 1.8,

    "daily_profit_target": 20,

    "volatility_detection": {
      "enabled": true,
      "atr_period": 14,
      "atr_scalp_threshold": 2.0,
      "scalp_profit_target_gbp": 13.87,
      "skip_trading_when_atr_extreme": true,
      "atr_max_for_trading": 20.0
    },

    "order_execution": {
      "deviation": 10,
      "comment": "fusion_sniper_v5",
      "market_data_bars": 500,
      "order_send_retries": 2,
      "min_stop_distance_usd": 0.50
    },

    "trading_hours": {
      "saturday_closed": true,
      "sunday_closed": true,
      "monday_open_hour": 1,
      "friday_close_hour": 23
    }
  },

  "RISK": {
    "max_daily_loss": 60,
    "max_daily_loss_currency": "GBP",
    "loss_limit_by_equity": true,
    "weekly_limits_enabled": true,
    "max_weekly_profit": 65,
    "max_weekly_loss": 0,
    "week_start_day": "monday",
    "max_drawdown_percent": 7.5,
    "max_positions_per_bot": 2
  },

  "STRATEGY": {
    "min_conditions_required": 4,

    "_comment_smc": "Live SMC settings (smc_only)",
    "SMC": {
      "enabled": true,
      "smc_only": true,
      "strict_bias": true,
      "persist_bias": true,
      "fractal_left_right": 2,
      "use_fvg_entries": true,
      "fvg_max_age_bars": 40,
      "fvg_min_size_atr_mult": 0.15,
      "fvg_rejection_wick_ratio": 0.5,
      "fvg_require_candle_direction": true
    }
  },

  "TELEGRAM": {
    "bot_token": "xxxxxxxx",
    "chat_id": "xxxxxxxx",
    "authorized_user_ids": ["xxxxxxxx"],
    "enabled": true,
    "api_timeout_seconds": 10
  },

  "NEWS_FILTER": {
    "enabled": true,
    "api_url": "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
    "feed_timezone": "UTC",
    "buffer_before_minutes": 30,
    "buffer_after_minutes": 30,
    "impact_levels": ["High", "Holiday"],
    "monitored_currencies": ["USD"]
  },

  "SYSTEM": {
    "paper_mode": false,
    "log_directory": "logs",
    "main_loop_interval": 30,
    "active_loop_interval": 1,
    "paused_loop_interval": 120,
    "waiting_log_interval": 300
  }
}
```

The example omits the legacy indicator keys (EMA/RSI/ADX/Stochastic/Bollinger), the
`TELEGRAM_HANDLER`, `WATCHDOG` and `STATISTICS` sections, and several other options. The complete,
authoritative key set lives in `config.json`.

---

## Requirements

- Windows. The MT5 Python API and the process control commands are written for Windows.
- An MT5 desktop terminal installed and logged in on the same machine.
- Python 3.11 or later.
- Python packages: `MetaTrader5`, `numpy`, `pandas`, `ta`, `requests`, and `tzdata` (provides the
  timezone database used by the standard library `zoneinfo` for the news filter).

```bash
pip install MetaTrader5 numpy pandas ta requests tzdata
```

---

## Running

The MT5 terminal for the account must be open and logged in first.

### Single instance

From a folder that contains `main_bot.py` and `config.json`:

```bash
# Live
python main_bot.py config.json

# Paper / dry run (no orders sent)
python main_bot.py config.json --paper
```

In parallel, run the Telegram command handler and, optionally, the watchdog:

```bash
python services/telegram_command_handler.py
python services/watchdog_monitor.py config.json
```

Telegram commands include `/start`, `/stop`, `/status`, `/positions`, `/daily`, `/health` and
`/news`. Access is restricted to `authorized_user_ids`.

### Multi instance layout

Each symbol or account runs as its own runtime folder with its own MT5 terminal, config, magic
number and Telegram bot:

```text
C:\fs_live_xauusd_xxxxxxxx\
  MT5\                     # portable MT5 for that account
  main_bot.py
  config.json              # this account's config (gitignored)
  modules\  services\  tools\  logs\
```

Each instance reads its own config, connects to its own MT5 terminal and uses its own magic
number so the instances do not interfere with each other.

---

## Development workflow

- Develop in a dev clone (for example `C:\fusion_sniper_bot`) that is separate from the live
  runtime folder (for example `C:\fs_live_xauusd_<account>`).
- Never edit code in the live folder while the bot is running.
- Make changes on a branch, review the diff, then merge.
- Version scheme: 5.0 is the baseline. Small fixes increment to 5.1, 5.2 and so on. A major
  structural change moves to 6.0.

---

## Safety and disclaimer

- Always test on a demo account or in paper mode first.
- Start with minimal position size and watch the logs and Telegram messages closely.
- Keep `magic_number` unique per instance so bots do not act on each other's trades.
- Re-check the `TRADING` and `RISK` sections whenever you clone a config for a new account or
  symbol.

Trading carries a significant risk of loss. This software is provided as is, with no guarantee of
profit and no warranty of any kind. Nothing here is financial advice. You are responsible for any
use of it on a live account.
