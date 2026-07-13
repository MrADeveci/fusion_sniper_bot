# Reboot recovery

Windows sometimes installs updates and reboots to the login screen even with auto-updates
"off". When that happens unattended, everything stops: broker-side stop losses still
protect open positions, but **trailing stops, daily caps, and all monitoring die silently**.
The position sits there with a stop that never moves.

This document describes the chain that makes the stack come back with no human present, and
makes *silence itself* raise an alarm.

Two ideas are worth separating:

- **Recovery** — the machine comes back on its own (auto-logon → startup chain → watchdog →
  bot). Reduces the length of an outage.
- **Detection** — you find out that it happened. The dead-man switch is the only layer that
  works when *everything else is dead*, because it alarms on the ABSENCE of a signal.

Recovery without detection is the dangerous combination: it hides outages instead of fixing
them.

---

## Layers, and what each one actually covers

| Layer | Covers | Blind to |
|---|---|---|
| Broker-side SL | Catastrophic price move while the bot is down | Everything else — the stop never trails |
| Instance lock | Two bots trading the same symbol | The bot being down |
| Bot heartbeat (`bot_status.json`) | Bot hung or dead — *if the watchdog is alive* | Watchdog dead, machine down |
| Watchdog | Bot crashed / hung → restart + Telegram alert | Watchdog itself dead, machine down |
| **Startup chain** | Machine rebooted → everything comes back | Machine at login screen (no auto-logon) |
| **Dead-man switch** | **Machine off, login screen, watchdog dead or wedged** | Nothing — silence is the alarm |
| BetterStack TCP port monitor (Ahmet's) | Machine/network reachable | **Reports "up" while the box sits at the login screen** — services start *before* logon. Do NOT treat it as covering the trading stack. |

---

## 1. Dead-man's switch (implemented, active)

A BetterStack **Heartbeat** monitor (expected period 5 min, grace ~10 min). The **watchdog**
GETs the ping URL at the end of every cycle it completes.

```
config.json  ->  SYSTEM.deadman_url
```

- **SECRET.** Anyone holding the URL can forge liveness. It lives ONLY in the gitignored
  `config.json`. `config.example.json` carries a placeholder. Never commit the real URL.
- Absent key ⇒ feature off.
- The GET has a 5s timeout; failures are logged and swallowed. A monitoring outage must
  never take down the thing it is monitoring.
- **It pings on out-of-hours and manual-stop cycles too.** That is deliberate: those are
  still cycles the watchdog is consciously minding. If it only pinged while trading, the
  switch would fire every weekend, and an alarm that cries wolf gets muted — which is how a
  dead-man switch quietly dies.

Because the *watchdog* sends it, the pings stop in every unminded state: machine off, stuck
at the login screen, watchdog killed, watchdog wedged.

**Test:** stop the watchdog, wait out the grace period, confirm BetterStack alerts.

## 2. Recovery alert (implemented, active)

On startup the watchdog sends Telegram:

```
🔄 Watchdog started — machine may have rebooted
🤖 Bot: stopped (watchdog will start it)
💓 Last bot heartbeat before now: 412s ago
📝 Mode: PAPER (simulated)
🖥 Machine up: 2 min (booted 13/07 05:31)
   ⚠️ Machine booted recently — this looks like a reboot.
```

So a reboot is never silent, even when recovery works perfectly.

---

## 3. Startup chain (scripts written; NOT yet applied)

`scripts/ops/setup_startup_chain.ps1` registers three **At Logon** tasks for the current
user, each with restart-on-failure (3 attempts, 1 min apart):

| Task | Delay | What |
|---|---|---|
| `FusionSniper-MT5` | +60s | the instance's portable `terminal64.exe` (`/portable`) — delay lets the network come up |
| `FusionSniper-Watchdog` | +90s | `services/watchdog_monitor.py` |
| `FusionSniper-Telegram` | +90s | `services/telegram_command_handler.py` |

**There is deliberately no task for `main_bot.py`.** The watchdog owns the bot's lifecycle:
its cold-start logic starts the bot, and `bot_state.json` restores position/paper state. A
second starter would race the watchdog. (The instance lock means the loser now exits
cleanly rather than double-trading — but racing at all is still wrong.)

**Prerequisite: auto-logon.** These are *At Logon* triggers. Without auto-logon the machine
sits at the login screen after a reboot and none of this runs. That is precisely the failure
this document exists to fix, and precisely the state the TCP port monitor reports as "up".

## 4. Auto-logon (script written; NOT applied — needs a decision)

`scripts/ops/setup_autologon.ps1`.

> **SECURITY TRADE-OFF — the machine boots to an UNLOCKED desktop.** Anyone with physical
> access gets the logged-in session: the MT5 terminal with saved broker credentials, and
> `config.json` with the account password and Telegram token in plaintext. **Acceptable only
> if the machine is physically secure.**

The registry method (`AutoAdminLogon` + `DefaultPassword`) stores the password **in
plaintext** in the registry. Prefer **Sysinternals Autologon**, which stores it as an LSA
secret:

```
Autologon64.exe -accepteula <user> <domain> <password>
```

If the box is *not* physically secure, do not enable auto-logon — run the stack as a Windows
Service under a service account instead (no interactive logon needed).

## 5. Windows Update window (script written; NOT applied)

`scripts/ops/setup_update_window.ps1`. Two layers:

1. **Active hours** (all editions) — Windows won't auto-restart inside them. Max window is
   **18 hours**, so active hours *cannot* cover a 24h trading day. Necessary, not sufficient.
2. **Group policy** (Pro/Enterprise only — this machine is **Win 11 Pro**, so available):
   `NoAutoRebootWithLoggedOnUsers=1`, `AUOptions=4`, `ScheduledInstallDay=7` (Saturday),
   `ScheduledInstallTime=3`.

**None of this makes reboots impossible.** A forced update can still restart the box
mid-week. That is exactly why layers 1–4 exist: the machine must *survive* a reboot it never
agreed to, not merely avoid one.

---

## Verifying

```powershell
powershell -ExecutionPolicy Bypass -File scripts\ops\verify_recovery.ps1
```

Read-only. Reports auto-logon state, the three tasks and their last result, running
processes (MT5 / watchdog / handler / bot), bot heartbeat age, instance lock, and the update
window.

## Replicating to another instance

1. Copy the bot folder; set `BROKER.symbol` / `magic_number` (the instance lock is keyed
   `bot_{symbol}_{magic}.lock`, so distinct values are what allow two instances to coexist).
2. Put that instance's own `deadman_url` in its `config.json` (a **separate** BetterStack
   heartbeat monitor per instance — sharing one URL would let a live instance mask a dead one).
3. Give the instance its **own portable MT5** and log it into the broker once so credentials
   are saved. Verify it reconnects unattended: kill `terminal64.exe`, relaunch, confirm it
   reaches the broker with no login prompt.
4. Run `setup_startup_chain.ps1` with `-BotDir` pointing at the new folder, and rename the
   tasks per instance.
5. Auto-logon and the update window are **machine-wide** — set once, not per instance.

## Known gaps

- **MT5 saved credentials are load-bearing and untested here.** If the terminal comes up on a
  login prompt after a cold start, the bot connects to nothing and the whole chain is
  cosmetic. This must be proven per instance (step 3 above).
- Auto-logon is the single point of failure for the entire recovery chain. If it is not
  enabled, everything downstream is inert.
- A forced Windows update can still reboot mid-week; recovery, not prevention, is the answer.
