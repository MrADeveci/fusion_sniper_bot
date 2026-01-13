# Fusion Sniper Bot

Automated trading bot for MetaTrader 5, written in Python.  
Fusion Sniper is designed to run multiple symbols and accounts using separate instances.  
It is currently tuned and used for:

- XAUUSD (Gold)
- BTCUSD (Bitcoin)
- EURUSD (Euro vs USD)

The bot runs on Windows, connects directly to MT5, applies a configurable technical strategy, manages risk, filters high impact economic news and sends rich Telegram notifications and health updates.

> **Important**
>
> The bot is driven by a `config.json` file that contains broker credentials and other sensitive data.  
> Do **not** commit your live config file to GitHub.  
> Keep your real `config.json` local and private and use an example file for documentation.

---

## Features

- Fully automated trading for XAUUSD and BTCUSD via separate instances
- Config driven behaviour. most settings live in `config.json`
- Technical strategy module with independent BUY and SELL conditions
- ATR based stops with smart breakeven and Chandelier style trailing stop
- Optional volatility detection and scalping mode based on ATR
- Daily profit target and daily loss limit with automatic *pause* when hit
- Centralised risk management and position sizing
- High impact economic news filter using ForexFactory XML feed including Holiday events
- Trade statistics tracking to JSON. win rate, best or worst trades and more
- Telegram notification module for all key bot events
- Telegram command handler service for remote control and health checks
- Watchdog monitor that keeps the bot running and cleans up stale cache
- Structured logging and status files suitable for external monitoring tools

---

## Repository structure

The repo contains the core trading engine, strategy and risk modules, Telegram services and some legacy components.

```text
fusion_sniper_bot/
  main_bot.py
  config.json           # local only. do not commit

  modules/
    __init__.py
    strategy.py
    risk_manager.py
    news_filter.py
    telegram_notifier.py
    trade_statistics.py

  services/
    telegram_command_handler.py
    watchdog_monitor.py

```

- `main_bot.py` . main Fusion Sniper trading bot
- `config.json` . local configuration file containing credentials, risk settings and behaviour flags
- `modules/` . core functional modules
  - `strategy.py` . `FusionStrategy` technical rules and signal generation
  - `risk_manager.py` . `RiskManager` for position sizing and risk limits
  - `news_filter.py` . `EconomicNewsFilter` for calendar based trading blocks
  - `telegram_notifier.py` . `TelegramNotifier` for sending messages
  - `trade_statistics.py` . `TradeStatistics` for performance tracking
- `services/` . supporting long running services
  - `telegram_command_handler.py` . `TelegramCommandHandler` for remote control
  - `watchdog_monitor.py` . `WatchdogMonitor` to keep the bot healthy

---

## Main components

### `main_bot.py` . FusionSniperBot

Main entry point and orchestration layer.

- Connects to MetaTrader 5 via the `MetaTrader5` Python package
- Loads and validates `config.json`
- Sets up logging and writes a status file at `logs/bot_status.json`
- Instantiates:
  - `FusionStrategy` from `modules/strategy.py`
  - `RiskManager` from `modules/risk_manager.py`
  - `EconomicNewsFilter` from `modules/news_filter.py`
  - `TradeStatistics` from `modules/trade_statistics.py`
  - `TelegramNotifier` from `modules/telegram_notifier.py`
- Core responsibilities:
  - Pulls price data from MT5
  - Builds pandas DataFrames for the strategy
  - Evaluates BUY and SELL conditions independently
  - Opens and manages positions subject to risk rules
  - Applies ATR based stops, smart breakeven and trailing stop logic
  - Enforces daily profit targets and daily loss limits including equity based checks
  - Respects news and holiday blackout windows from `EconomicNewsFilter`
  - Updates trade statistics and sends Telegram updates for key events

### `modules/strategy.py` . FusionStrategy

Technical analysis strategy module.

- Uses the `ta` library for indicators:
  - EMAs for trend and structure
  - RSI
  - Stochastic Oscillator
  - ADX
  - Bollinger Bands
- Evaluates BUY and SELL blocks separately and returns:
  - Signal direction (buy or sell or flat)
  - Condition counts
  - Textual reasoning details for logging or Telegram
- Key configuration via `config["STRATEGY"]`:
  - `min_conditions_required`
  - EMA periods
  - RSI overbought or oversold thresholds
  - Stochastic and ADX thresholds
  - Bollinger Band settings where applicable
  - Additional technical analysis parameters such as swing detection and candle structure

### `modules/risk_manager.py` . RiskManager

Risk and money management module.

- Uses MT5 account information to enforce:
  - Maximum open positions per bot
  - Maximum total volume or exposure per symbol
  - Maximum daily loss in currency terms
  - Maximum drawdown percentage based on equity or balance
- Validates potential trades before entry:
  - Position sizing rules
  - Stop loss and take profit sanity checks relative to entry price
- Reads configuration from `config["RISK"]` and supporting values from `config["TRADING"]`

### `modules/news_filter.py` . EconomicNewsFilter

High impact economic news and holiday filter. used to avoid trading during volatile periods and certain calendar events.

- Fetches news from a ForexFactory XML feed defined in `config["NEWS_FILTER"]`
- Filters by:
  - Currency list. typically `["USD"]` for XAUUSD and BTCUSD CFDs
  - Impact levels. for example `["High", "Holiday"]`
  - Future time window around each event
- Caches events to `cache/news_events.json` and reuses them when possible
- Provides:
  - Methods to check if trading is allowed at the current time
  - A list of upcoming events and holidays within a chosen horizon for display in Telegram

### `modules/trade_statistics.py` . TradeStatistics

Tracks and persists historical performance to JSON.

- Stores per trade information such as:
  - Profit or loss
  - Exit reason
  - Order type
  - Size
- Maintains aggregate statistics including:
  - Total trades
  - Win and loss counts and win rate
  - Total profit and loss
  - Best and worst trade
  - Streaks and exit reason breakdowns
- The statistics file path and history length are read from the relevant section in `config.json`

### `modules/telegram_notifier.py` . TelegramNotifier

Thin wrapper around the Telegram Bot HTTP API using `requests`.

- Sends HTML formatted or Markdown formatted messages to a configured chat
- Used by `main_bot.py` to send:
  - Startup and shutdown notifications
  - New trade opened and closed
  - Daily summaries
  - Daily profit or loss limit alerts
  - Error messages or warnings
- Configuration comes from `config["TELEGRAM"]`:
  - `bot_token`
  - `chat_id`
  - `enabled`

### `services/telegram_command_handler.py` . TelegramCommandHandler

Separate long running process that polls Telegram for commands and interacts with a single bot instance.

- Reads the same `config.json` as the main bot in that folder
- Uses:
  - `config["BROKER"]` for symbol and magic number
  - `config["TELEGRAM"]` for API credentials and authorised user IDs
  - `config["TELEGRAM_HANDLER"]` for handler specific settings
- Core functionality:
  - Long polls the Telegram Bot API for updates
  - Restricts access to `authorized_user_ids`
  - Provides the following commands:
    - `/start` . start the trading bot
    - `/stop` . stop the trading bot
    - `/news` . show upcoming news and holiday events
    - `/status` . show current bot status and basic account information
    - `/positions` . list current open positions relevant to the bot
    - `/daily` . show daily performance summary and key statistics
    - `/health` . show a combined health view including last heartbeat, margin levels and watchdog status
  - Reads `logs/bot_status.json` and other files to determine the current bot state
  - Can start or stop the main bot process using Windows commands
- Intended to run alongside the bot on the same machine. one handler per instance or per account

### `services/watchdog_monitor.py` . WatchdogMonitor

Lightweight watchdog process that ensures the main bot stays healthy.

- Reads its settings from `config["WATCHDOG"]` and `config["SYSTEM"]`
- Regularly checks:
  - Whether the main bot process is running. using the PID from `logs/bot_status.json`
  - Whether the current time is within configured trading hours
- If the bot is not running during trading hours and no manual stop flag is present:
  - Automatically restarts the bot using `subprocess`
- Cleans up old cache files such as stale news events
- Windows specific. uses `tasklist` and `subprocess.CREATE_NO_WINDOW`

---

## Requirements

- Operating system. Windows. the MetaTrader 5 Python API and system commands are written with Windows in mind
- Python. 3.9 or later recommended
- MetaTrader 5 desktop terminal installed and logged in on the same machine
- Python packages:
  - `MetaTrader5`
  - `numpy`
  - `pandas`
  - `ta` (technical analysis indicators)
  - `requests`

Install dependencies:

```bash
pip install MetaTrader5 numpy pandas ta requests
```

You may also want to create a virtual environment:

```bash
python -m venv .venv
.\.venv\Scriptsctivate
pip install -r requirements.txt  # if you add one
```

---

## Configuration . `config.json`

The project relies on a `config.json` file placed alongside `main_bot.py` in the repo folder or in each runtime folder.

This file is **not** committed to the repository because it contains live account details.  
Add at least the following to your `.gitignore`:

```text
config.json
logs/
cache/
```

### Example structure for one instance

The exact fields available are extensive. below is a trimmed example that shows the key sections and common options for a single symbol and account.  
You would create one config per account or per symbol.

```json
{
  "_comment": "Fusion Sniper Trading Bot - v4.2",
  
  "BROKER": {
    "account": xxxxxxxx,
    "password": "xxxxxxxxx",
    "server": "ICMarketsSC-MT5-4",
    "symbol": "XAUUSD",
    "magic_number": 000000,
    "mt5_path": "C:\\fs_live_xauusd_11173822\\MT5\\terminal64.exe",
    "timeout": 60000,
    "portable": true,
    "broker_timezone_offset": 2
  },
  
  "TRADING": {
    "timeframe": "M15",
    "lot_size": 0.7,

    "_comment_positions": "Maximum Concurrent Positions",
    "max_positions": 2,

    "_comment_stops": "Stop Loss & Take Profit Settings",
    "use_atr_based_stops": true,
    "stop_loss_atr_multiple": 0.4,
    "take_profit_atr_multiple": 2.0,

    "_comment_breakeven": "Smart Break-Even Settings",
    "use_smart_breakeven": true,
    "breakeven_profit_multiple": 0.6,
    "breakeven_lock_profit_multiple": 0.3,

    "_comment_trailing": "Trailing Stop Settings",
    "use_trailing_stop": true,
    "trailing_stop_type": "chandelier",
    "trailing_stop_atr_multiple": 2.0,
    "min_profit_for_trail_activation": 1.8,

    "_comment_cooldown": "Trade Cooldown Settings",
    "trade_cooldown_seconds": 60,

    "_comment_profit": "Daily Profit Target",
    "daily_profit_target": 55,

    "_comment_volatility": "Volatility Detection & Scalping",
    "volatility_detection": {
      "enabled": true,
      "atr_period": 14,
      "atr_scalp_threshold": 2.0,
      "scalp_profit_target_gbp": 26.87,
      "scalp_cooldown_seconds": 30,
      "normal_cooldown_seconds": 60,

      "_comment_limits": "ATR based risk limits",
      "skip_trading_when_atr_extreme": true,
      "atr_max_for_trading": 20.0
    },

    "_comment_orders": "Order Execution Settings",
    "order_execution": {
      "deviation": 10,
      "comment": "fusion_sniper_bot",
      "scalp_comment": "scalp_quick_profit",
      "emergency_rr_ratio": 2.0,
      "tolerance_pips": 2,
      "market_data_bars": 250
    },

    "_comment_hours": "Trading Hours - UK time (Monday 01:00 - Friday 23:00, Sunday CLOSED)",
    "trading_hours": {
      "saturday_closed": true,
      "sunday_closed": true,
      "monday_open_hour": 1,
      "sunday_open_hour": 23,
      "friday_close_hour": 23
    }
  },

  "RISK": {
    "_comment_daily": "Daily loss protection in account currency",
    "max_risk_per_trade": 1.5,
    "max_daily_loss": 150,
    "max_daily_loss_currency": "GBP",
    "loss_limit_by_equity": true,

    "_comment_weekly": "Weekly NET P&L caps in account currency. closed trades + swap - commission",
    "weekly_limits_enabled": true,
    "max_weekly_profit": 150,
    "max_weekly_loss": 175,
    "week_start_day": "monday",

    "max_drawdown_percent": 10.0,
    "max_positions_per_bot": 2,

    "_comment_confidence": "Confidence-Based Position Sizing",
    "confidence_based_scaling": {
      "enabled": false,
      "min_confidence": 0.6,
      "high_confidence_threshold": 0.8,
      "scaling_range": {
        "min_multiplier": 0.5,
        "max_multiplier": 1.0
      }
    }
  },

  "STRATEGY": {
    "_comment_ema": "EMA Settings",
    "ema_20_period": 20,
    "ema_50_period": 50,
    "ema_100_period": 100,
    "ema_200_period": 200,

    "_comment_rsi": "RSI Settings",
    "rsi_period": 14,
    "rsi_oversold": 40,
    "rsi_overbought": 60,

    "_comment_atr": "ATR Settings",
    "atr_period": 14,
    "atr_multiplier": 1.5,

    "_comment_macd": "MACD Settings",
    "macd_fast_period": 12,
    "macd_slow_period": 26,
    "macd_signal_period": 9,

    "_comment_bb": "Bollinger Bands Settings",
    "bb_period": 20,
    "bb_std_dev": 2,

    "_comment_conditions": "Signal Generation - Minimum conditions required for trade entry",
    "min_conditions_required": 4,
    "debug_signals": false,

    "_comment_technical": "Technical Analysis Parameters",
    "technical_analysis": {
      "swing_detection_threshold": 0.0015,
      "candle_body_ratio": 0.6,
      "exhaustion_wick_ratio": 2.0,
      "breakout_volume_threshold": 1.5,
      "support_resistance_tolerance": 0.0005,
      "min_distance_from_sr": 0.001
    },

    "_comment_trend": "Trend Filter Parameters",
    "trend_filter": {
      "enabled": true,
      "scope": "window",
      "window": { "weekday": 3, "start_hour": 0, "end_hour": 8 },
      "require_trend_flag": true,
      "buy_extra_conditions": 0,
      "sell_extra_conditions": 1
    }
  },

  "TELEGRAM": {
    "bot_token": "00000000000000000000000000000",
    "chat_id": "00000000000000",
    "authorized_user_ids": ["00000000000"],
    "enabled": true,

    "_comment_api": "Telegram API Settings",
    "api_timeout_seconds": 10
  },

  "TELEGRAM_HANDLER": {
    "_comment": "Telegram command handler settings",

    "_comment_paths": "File Paths",
    "paths": {
      "log_file": "logs/telegram_handler.log",
      "bot_status_file": "logs/bot_status.json",
      "manual_stop_flag": "logs/manual_stop.flag",
      "trade_statistics_file": "logs/trade_statistics_{symbol}.json",
      "news_events_file": "cache/news_events.json",
      "log_directory": "logs"
    },

    "_comment_timeouts": "Timeouts and Intervals",
    "command_timeout_seconds": 30,

    "_comment_intervals": "Check Intervals",
    "intervals": {
      "bot_startup_check": 1,
      "process_wait": 2
    },

    "_comment_health": "Health Check Thresholds",
    "health_thresholds": {
      "log_active_minutes": 5,
      "log_warning_minutes": 60,
      "margin_safe_level": 500,
      "margin_warning_level": 200
    },

    "_comment_display": "Display Settings",
    "display": {
      "news_forecast_hours": 24,
      "max_news_events": 5
    }
  },

  "NEWS_FILTER": {
    "enabled": true,
    "api_url": "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
    "buffer_before_minutes": 30,
    "buffer_after_minutes": 30,
    "check_interval_seconds": 300,
    "impact_levels": ["High", "Holiday"],
    "monitored_currencies": ["USD"],

    "_comment_notifications": "News Notification Settings",
    "notify_on_news_avoidance": true,

    "_comment_weekly": "Weekly Planning Summary (Sunday before market open)",
    "weekly_summary_enabled": true,
    "weekly_summary_day": 6,
    "weekly_summary_hour_gmt": 22,
    "holiday_buffer_hours": 12,

    "_comment_cache": "Cache Settings",
    "cache_directory": "cache",
    "cache_max_age_minutes": 10,
    "cache_file": "cache/news_events.json",
    "cache_validity_hours": 6,
    "cache_retention_days": 7,
    "api_timeout_seconds": 10,

    "_comment_retry": "Retry Logic",
    "max_retries": 3,
    "retry_delay_seconds": 2
  },

  "STATISTICS": {
    "enabled": true,
    "stats_file_path": "logs/trade_statistics_{symbol}.json",
    "max_history": 100,

    "_comment_tracking": "What to Track",
    "track_mae": true,
    "track_mfe": true,
    "track_session_performance": true,
    "track_exit_reasons": true
  },

  "WATCHDOG": {
    "_comment": "Watchdog monitor settings",
    "check_interval_seconds": 300,

    "_comment_hours": "Trading Hours - UK time (Monday 01:00 - Friday 23:00, Sunday CLOSED)",
    "trading_hours": {
      "saturday_closed": true,
      "sunday_closed": true,
      "monday_open_hour": 1,
      "sunday_open_hour": 23,
      "friday_close_hour": 23
    },

    "_comment_cache": "Cache Retention",
    "cache_retention_hours": 168
  },

  "SYSTEM": {
    "_comment": "System-wide settings",
    "_comment_paths": "File Paths",
    "log_directory": "logs",
    "bot_status_file": "logs/bot_status.json",

    "_comment_logging": "Logging Configuration",
    "log_format": "%(asctime)s - %(levelname)s - %(message)s",
    "log_retention_days": 7,

    "_comment_timing": "Loop Timing (seconds)",
    "main_loop_interval": 1,
    "paused_loop_interval": 300,
    "idle_sleep_interval": 60,
    "waiting_log_interval": 300
  }
}
```

For BTC you would use a separate config with:

- `"symbol": "BTCUSD"` (or your broker’s exact symbol)
- A different `magic_number`
- BTC specific risk and trading parameters
- A separate Telegram bot token and chat id if you want per account isolation

---

## Running the bot

### Single instance

From the project folder (or from a dedicated runtime folder that contains `main_bot.py` and `config.json`):

```bash
python main_bot.py config.json
```

In parallel run the Telegram command handler:

```bash
python services/telegram_command_handler.py
```

And optionally the watchdog:

```bash
python services/watchdog_monitor.py
```

In practice many users create a small `.bat` file that opens a Windows Terminal window with three tabs:

- Bot
- Telegram handler
- Watchdog

All pointed at the same folder.

### Multi instance layout. XAUUSD and BTCUSD

A common layout for running multiple accounts or symbols is:

```text
C:\fusion_sniper_bot\                    # main codebase linked to GitHub

C:\fs_live_xauusd_00000001\       # XAUUSD instance for account 00000001
  MT5\                                   # portable MT5 for that account
  main_bot.py
  config.json                            # XAU config
  modules  services  logs
C:\fs_live_btcusd_00000002\        # BTCUSD instance for account 00000002
  MT5\                                   # portable MT5 for that account
  main_bot.py
  config.json                            # BTC config
  modules  services  logs```

Workflow.

- Develop and version control the code in `C:\fusion_sniper_bot`
- When you are happy with a version. copy updated Python files into each runtime folder without overwriting:
  - `config.json`
  - `MT5\`
  - `logs\`
- Each runtime folder is started with its own `.bat` script that:
  - launches the local MT5 in portable mode
  - starts `main_bot.py`
  - starts `telegram_command_handler.py`
  - starts `watchdog_monitor.py`

Each instance reads its own config, connects to its own MT5 terminal and uses its own Telegram bot.

---

## Safety and testing

- Always start on a demo account and keep position sizes small until you trust the behaviour
- Ensure `magic_number` values are unique per instance so bots do not interfere with each other’s trades
- Double check `TRADING` and `RISK` settings whenever you clone a config for a new account or symbol
- Watch the logs and Telegram messages closely for the first few days of any new deployment

Fusion Sniper is designed to be heavily config driven.  
Most strategy, risk and timing changes can be made in `config.json` without editing Python code.  
Use that to your advantage when tuning XAUUSD and BTCUSD separately.