# Fusion Sniper Bot — Code Audit (read-only)

Scope: `main_bot.py`, `modules/strategy.py`, `modules/risk_manager.py`,
`modules/news_filter.py`, `modules/trade_statistics.py`, `modules/telegram_notifier.py`,
`services/telegram_command_handler.py`, `services/watchdog_monitor.py`.

No source files were changed. Findings only. Line numbers are at time of audit.

---

## CRITICAL

### C1 — MT5 connection loss is never detected or reconnected (silent stop)
`main_bot.py` run loop 1557-1729; no reconnect anywhere; `mt5` calls only None-guarded.
- **Problem:** If the MT5 terminal drops its broker connection mid-session, every `mt5.*`
  call (`symbol_info_tick`, `positions_get`, `copy_rates_from_pos`, `order_send`) returns
  None. The guarded helpers swallow that and return/skip, so the loop keeps spinning while
  doing **nothing** — no new trades, and crucially **no breakeven / trailing / scalp-exit
  management** of open positions. There is no `mt5.initialize()` retry and the
  `notify_connection_lost()` alert (telegram_notifier.py:350) is **never called**.
- **Runtime:** A dropped connection during an open position silently disables all bot-side
  exit management; the position rides only on its broker-side SL/TP until the connection
  returns (if ever). No alert is sent.
- **Fix (1-line):** Add a connection check (`mt5.terminal_info().connected`) each loop that
  calls `mt5.initialize()`/`mt5.login()` on failure and fires `notify_connection_lost`.

### C2 — Watchdog only restarts a bot it has *already seen running*; and it dies on any error
`services/watchdog_monitor.py:238` (`if self.last_bot_running:`) and 262 (`except Exception: print`).
- **Problem:** `last_bot_running` starts `False`. The restart branch (238-247) only fires if
  the watchdog previously observed the bot up. If the bot is already down when the watchdog
  starts, or the watchdog process restarts after a crash, it logs *“Bot not running (manual
  start expected)”* forever and never relaunches. Separately, the `run()` loop’s top-level
  `except` (262) **exits** the loop on any unexpected error, so the watchdog itself can die
  silently and stop monitoring.
- **Runtime:** The safety net that is supposed to recover a crashed live bot frequently
  won’t — exactly when it’s needed (cold start, or post-reboot when nothing was “seen”).
- **Fix:** Restart whenever `not bot_running and not manual_stop` (drop the `last_bot_running`
  precondition) and wrap the loop body so exceptions `continue` instead of exiting.

### C3 — Unhandled exception in the main loop kills the bot (no per-iteration recovery)
`main_bot.py:1731-1736` — single top-level `try/except` around the whole `while` loop.
- **Problem:** Helper methods have their own `try/except`, but any exception raised directly
  in the run-loop body (e.g. an unexpected None shape at 1685 `entry_rates[-1]['time']`, or a
  KeyError) propagates to 1733, logs once, and calls `shutdown()` — the process exits and
  relies on the watchdog (see C2) to come back.
- **Runtime:** A single transient/edge error terminates live trading instead of skipping one
  iteration; recovery then depends on the unreliable watchdog.
- **Fix:** Wrap the loop body in an inner `try/except` that logs and `continue`s (keep the
  outer one for clean shutdown).

---

## HIGH

### H1 — `modify_position` failures are silent (breakeven/trailing can quietly stop working)
`main_bot.py:1517-1532`; callers 1471-1483 (breakeven), 1504-1513 (trailing).
- **Problem:** `modify_position` returns `result and result.retcode == DONE` but **logs
  nothing on failure** and has **no retry**. Callers only log on *success*. A rejected SL
  modify (requote, off-quotes, market closed, invalid stops) is invisible.
- **Runtime:** Breakeven/trailing silently fail to move the stop; the position keeps its
  original (wider) SL while logs imply nothing is wrong. Risk management degrades unnoticed.
- **Fix:** Log `result.retcode`/`mt5.last_error()` on non-DONE and retry once.

### H2 — `open_trade` order send: hardcoded filling mode, no normalisation, no rounding, no retry
`main_bot.py:1344-1369` (also handler `_close_all_positions:610`).
- **Problem:** `type_filling` is **hardcoded `ORDER_FILLING_IOC`** (1356), not derived from
  `symbol_info.filling_mode`. Volume is the raw config `lot_size` — **not normalised** to
  `volume_step`/`volume_min`/`volume_max`. SL/TP from `calculate_atr_based_stops` are **not
  rounded** to `symbol_info.digits`/tick size. Non-DONE retcodes return immediately with **no
  retry** and no requote handling. (Broker min-distance *is* partially handled, 1318-1336.)
- **Runtime:** On a broker/symbol that rejects IOC, or with unrounded stops / odd lot steps,
  **every entry is rejected** — silently no trades. (It happens to work on the current broker
  with IOC, so this is latent, not currently firing.)
- **Fix:** Pick filling from `symbol_info.filling_mode`, `round()` SL/TP to digits, snap volume
  to `volume_step`, and retry on requote/price-changed retcodes.

### H3 — In-flight position state is lost/recomputed on restart
`main_bot.py:1168-1183` (`update_tracked_positions`).
- **Problem:** On restart, open positions are re-registered with `breakeven_applied=False`
  and `entry_atr = self.calculate_atr()` — i.e. the **current** ATR, not the ATR at entry.
  Cooldown (`last_trade_time`) and the per-position breakeven flag live only in memory.
- **Runtime:** After any restart, breakeven can re-trigger, and breakeven/trailing distances
  are computed from the wrong ATR; cooldown resets so the bot may re-enter immediately.
- **Fix:** Persist per-ticket `{entry_atr, breakeven_applied}` (and `last_trade_time`) to disk
  and reload them in `update_tracked_positions`.

### H4 — Two independent daily-loss systems with different definitions
`modules/risk_manager.py:46-98` (`can_trade`/`get_daily_profit`) vs
`main_bot.py:783-1017` (`check_daily_profit`), both gating entries (main_bot:1659-1671).
- **Problem:** `RiskManager.can_trade` computes today’s P&L as **gross** `sum(deal.profit)`
  over a **local-midnight** window (no `broker_timezone_offset`), compared to `max_daily_loss`.
  `main_bot.check_daily_profit` uses **net** (`profit − commission + swap`) over a
  **broker-time** window **plus** an equity-drawdown branch. It also enforces
  `max_drawdown_percent` (7.5%) independently. Two different thresholds/windows/definitions
  decide “can we trade”.
- **Runtime:** Conflicting pauses/!pauses; one system can block while the other allows, and the
  effective daily-loss cutoff is ambiguous and timezone-inconsistent.
- **Fix:** Make `RiskManager.can_trade` defer to (or share) `check_daily_profit`’s net/broker
  calculation instead of its own gross/local one.

### H5 — News filter compares naive feed times to UK local `datetime.now()` (no timezone)
`modules/news_filter.py:99-101` (parse), 216-234 (`should_avoid_trading`), 247 (`get_upcoming_events`).
- **Problem:** ForexFactory feed timestamps are parsed with `datetime.strptime` into **naive**
  datetimes and compared directly to `datetime.now()` (PC = UK local, BST/GMT). The feed’s
  times are in its own (FF-account) timezone, typically US-Eastern — no conversion is done.
- **Runtime:** Avoidance windows are offset by hours (≈5–6h vs US-Eastern). The bot can trade
  straight through high-impact news and/or “avoid” at the wrong times. (`broker_timezone_offset`
  is **not** applied here either.)
- **Fix:** Localise parsed event times to the feed timezone and convert to the same clock used
  for `now` before comparing.

---

## MEDIUM

### M1 — Handler crashes at startup if MT5 not connected
`services/telegram_command_handler.py:134` — `mt5.account_info().login` with no None-check
(after `mt5.initialize()` at 129). If `account_info()` is None → `AttributeError`, handler dies.

### M2 — Handler initialises MT5 with no path/login
`telegram_command_handler.py:129` — `mt5.initialize()` (no `path=`, no `login`). It may attach
to a different/default terminal than the portable one in `config.BROKER.mt5_path`, so
`/status`,`/daily`,`/positions` could read the wrong account. (main_bot uses the configured path.)

### M3 — Per-trade values are GROSS, but daily/weekly caps are NET
`main_bot.py:1207-1239` and `trade_statistics.update_overall_stats` use `exit_deal.profit`
(gross, no commission). Caps (`check_daily_profit`/`check_weekly_limits`) use net. Telegram
trade messages and `trade_statistics_*.json` therefore overstate profit vs the cap logic.

### M4 — `/stats` session keys never match → always shows 0
`telegram_command_handler.py:979` reads `sessions.get('London')`/`get('NewYork')`, but
`trade_statistics` stores `london`/`new_york` (trade_statistics.py:80-85). NY isn’t even shown.

### M5 — Bot-state/news detection reads a log file that never exists
`telegram_command_handler.py:471, 652` — `logs/{symbol.lower()}_bot.log`. Real logs are
`logs/XAUUSD_DDMMYYYY.log` (main_bot rotation). So `/health` and `/status` “News/cooldown”
detection silently never triggers.

### M6 — Config-key mismatches → silent fallback to defaults (works only by coincidence)
- `trade_statistics.py:29,32` read `log_file`/`max_trades_history`; config has
  `STATISTICS.stats_file_path`/`max_history`. Defaults happen to equal config values.
- `risk_manager.py:26` reads `RISK.confidence_based_sizing`; config has
  `confidence_based_scaling`. Confidence sizing config is never read.
- `telegram_command_handler.py:89` reads `TELEGRAM.api_timeout`; config has
  `api_timeout_seconds`. Also 99/100/113-120 read `bot_status_file`, `manual_stop_flag_file`,
  `log_active_threshold_minutes`, `margin_safe_level`, `news_forecast_hours`,
  `max_news_events_display` at root, but config nests them under `paths`/`health_thresholds`/
  `display` with different names → defaults used.
- **Runtime:** Changing those config values has no effect; a future edit (e.g. enabling
  `confidence_based_scaling`) will silently do nothing.

### M7 — Telegram sends are synchronous (up to 10s) inside the trading loop
`telegram_notifier.py:60` and handler `send_message:187`. Every open/close/news notification
blocks the loop up to the request timeout; `send_test_connection` blocks at startup. If the
API is slow, position management is delayed.

### M8 — Log spam → multi-MB daily logs
`main_bot.py:1638` (mode line every loop) and 1652 (“Scanning market…” every loop). With
`active_loop_interval=1s` while in a position this writes every second (observed daily logs of
14–27 MB). Risks disk fill and slows I/O.

### M9 — `check_daily_profit` runs twice per loop
`main_bot.py:1608` and `1611` both call it (results assigned to `target_reached` and
`daily_paused`). Doubles the `history_deals_get` query and logging each iteration.

### M10 — `starting_equity_today` seeded from balance, not equity
`main_bot.py:898` sets it to `account_info().balance`. The equity-drawdown check (901-903)
does `balance − equity`; if positions are open at the day boundary, the first reading is off.

### M11 — Hard-kill on `/stop` can interrupt mid-operation
`telegram_command_handler.py:301-322` uses `taskkill /F /T`. The bot’s `finally: shutdown()`
won’t run; a kill landing during `order_send`/file write could leave a stale `bot_status.json`
or a half-logged state. (Positions are intentionally left open.)

### M12 — Strategy `strict_bias` gate checks non-NEUTRAL only, not direction
`main_bot.py:1713` allows any signal when bias ∈ {BULL,BEAR}; it does not require the signal
*direction* to match the bias. With the indicator stack this permits a BUY while bias is BEAR.

---

## LOW / tidy-up

### L1 — Dead config knob `trade_cooldown_seconds`
`main_bot.py:126` stores `self.trade_cooldown` but `is_in_cooldown` (1142) uses
`scalp_cooldown`/`normal_cooldown`. `self.trade_cooldown` and config `trade_cooldown_seconds`
are unused.

### L2 — Redundant/odd direction check in `handle_position_closure`
`main_bot.py:1228`: `if (direction=="SELL" and profit<0) or (direction=="BUY" and profit<0)` —
both branches are just `profit < 0`; direction is irrelevant. Confusing dead condition.

### L3 — Indicator-stack path is effectively dead under `smc_only=true`
`modules/strategy.py:497-632` (~135 lines). With `SMC.enabled=true, smc_only=true`, `analyze`
returns at 494/495 before the EMA/RSI/ADX/Stoch/BB block. Only exercised if SMC is turned off.

### L4 — Unused functions
- `telegram_notifier.py`: `notify_trailing_activated` (206), `notify_error` (277),
  `notify_connection_lost` (350), `send_daily_summary` (387), `notify_daily_progress` (442),
  `notify_trade_closed_with_progress` (470), `notify_target_reached` (507),
  `notify_midnight_reset` (546), `notify_friday_warning` (562) — none are called by the bot.
  (`notify_target_reached:535` also has a `gross/target*100` divide-by-zero if target=0.)
- `telegram_command_handler.py`: `_close_all_positions` (578) is never called (handle_stop
  hardcodes `positions_closed=0`); it also does `result.retcode` with no None-check (615).
- `notify_breakeven_activated` (193) is a `pass` no-op but is still called (1474) — harmless.

### L5 — Unused config keys / hardcoded equivalents
- `order_execution.deviation` (config 10) — `open_trade` hardcodes `10` (1352); scalp/close use 20.
- `order_execution.comment` (“fusion_sniper_bot”) — bot hardcodes “fusion_sniper_bot_v4” (1354).
- `order_execution.emergency_rr_ratio`, `order_execution.tolerance_pips` — never read.
- `RISK.confidence_based_scaling.*` — never read (see M6).
- `RISK.max_risk_per_trade` — stored in RiskManager but no sizing uses it (fixed lots).
- `swap_avoidance` machinery (main_bot 649-699, 1614-1617) present but `enabled=false`.

### L6 — Unused import
`trade_statistics.py:11` imports `Optional` (unused). (Other modules’ imports check out.)

### L7 — Version strings are inconsistent (for unifying to v5.0)
| File:line | String |
|---|---|
| `config.json:2` | `"Fusion Sniper Trading Bot - v4.4"` |
| `main_bot.py:2` | `"Fusion Sniper Bot - v4.4"` |
| `main_bot.py:209` | log `"Fusion Sniper Bot v4.1"` |
| `main_bot.py:102` | comment `"# V3.0: ATR-based break-even and trailing stop"` |
| `main_bot.py:1354` | order comment `"fusion_sniper_bot_v4"` |
| `modules/strategy.py:2` | `"Fusion Sniper Bot Strategy v4.4"` |
| `modules/telegram_notifier.py:2` | `"Telegram Notification Module v4.0"` |
| `modules/telegram_notifier.py:439` | `"NEW METHODS FOR DAILY PROFIT MANAGER (v3.0.0)"` |
| `modules/telegram_notifier.py:681` | `"Telegram Notifier Module v4.0"` |
| `services/telegram_command_handler.py:3` | `"Telegram Command Handler - Fusion Sniper Bot (v4.0)"` |

(Order tags also worth unifying: `order_execution.comment="fusion_sniper_bot"` vs hardcoded
`"fusion_sniper_bot_v4"` vs scalp `"scalp_quick_profit"`.)

---

## Other issues found beyond the checklist

### O1 — ATR≈0 / very-small ATR edge (MEDIUM)
`risk_manager.calculate_atr_based_stops:108-116`: if `atr` is ~0, SL/TP collapse onto entry →
`validate_trade` fails and the trade is skipped (safe), but a merely *tiny* ATR yields an
extremely tight stop → near-instant stop-out. No floor on stop distance.

### O2 — None-deref risks outside main_bot’s guards (MEDIUM)
main_bot is mostly defensive, but the handler is not: `account_info().login` (M1),
`_close_all_positions` `result.retcode` (L4). Confirm `account_info()`/`order_send` results
before attribute access in the handler.

### O3 — Cross-process MT5 / race (LOW-MEDIUM)
The bot and the command handler each call `mt5.initialize()` in separate processes and both
issue `order_send`/`positions_get`. A `/stop` hard-kill (M11) during a bot `order_send` is an
uncontrolled interruption. Generally tolerated by MT5 but not coordinated.

### O4 — `wt`-dependent launch (LOW)
Both `watchdog.start_bot` (151) and handler `handle_start` (226) launch via Windows Terminal
(`wt`). If `wt` isn’t installed/in PATH, restart/launch fails with only a logged error.

### O5 — `monday_open_hour` is cosmetic in main_bot but enforced elsewhere (LOW)
`main_bot.is_within_trading_hours` never gates on `monday_open_hour` (it’s only in “closed”
*messages*), whereas `telegram_command_handler._is_within_trading_hours:563` and the watchdog
*do* treat Monday/Sunday open hours as gates. The three hour-checks disagree on Monday 00:00–01:00
and on which clock they use (all use local `datetime.now()` = UK).

---

## Quick status of the 13 named suspects

1. **order_send** — *Present.* IOC hardcoded; no fill-mode selection; no volume normalisation;
   no SL/TP digit rounding; no retry/requote handling. Min-distance partially handled. (H2)
2. **modify_position** — *Present.* No failure logging, no rounding, no retry; fails silently. (H1)
3. **None-handling** — *Mostly handled in main_bot* (defensive try/except + None checks);
   *gaps in the handler* (account_info().login, _close_all_positions). (M1, O2)
4. **Main-loop resilience** — *Partial.* Outer try/except exists but exits on error; no
   per-iteration recovery → crash-and-rely-on-watchdog. (C3)
5. **MT5 reconnect** — *Absent.* No detection/reconnect; management silently stops. (C1)
6. **Config-key mismatches** — *Confirmed present:* `confidence_based_sizing` vs
   `confidence_based_scaling`; `log_file`/`max_trades_history` vs `stats_file_path`/`max_history`;
   plus handler `api_timeout` and nested paths/health/display keys. (M6)
7. **Two daily-loss systems** — *Confirmed.* RiskManager gross/local vs main_bot net/broker+equity. (H4)
8. **Gross vs net** — *Confirmed.* Per-trade Telegram/stats gross; caps net. (M3)
9. **State across restart** — *Lost/recomputed.* breakeven flag resets, entry_atr recomputed
   with current ATR, cooldown reset. (H3)
10. **news_filter timezone** — *Confirmed bug.* Naive feed times vs UK `datetime.now()`, no
    conversion. (H5)
11. **max_weekly_loss = 0** — *Safe.* Treated as **disabled** (`max_weekly_loss > 0` guard at
    check_weekly_limits:1040,1101); never triggers on a loss.
12. **Dead/unused** — *Confirmed:* `trade_cooldown_seconds` (L1), redundant direction check
    (L2), indicator stack dead under smc_only (L3), many unused notifier methods +
    `_close_all_positions` (L4), unused import (L6).
13. **Version strings** — *Confirmed scattered* across v4.4/v4.1/v4.0/v3.0.0/V3.0 (table in L7).
