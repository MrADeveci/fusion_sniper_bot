<#
.SYNOPSIS
  Registers the At-Logon startup chain that brings the trading stack back after a reboot.

.DESCRIPTION
  Creates three scheduled tasks, all triggered "At log on" of the CURRENT user:

    1. FusionSniper-MT5        +60s   the portable MT5 terminal (network needs a moment)
    2. FusionSniper-Watchdog   +90s   watchdog_monitor.py  -- this is what starts the BOT
    3. FusionSniper-Telegram   +90s   telegram_command_handler.py

  There is deliberately NO task for main_bot.py. The watchdog owns the bot's lifecycle:
  its cold-start logic brings the bot up, and bot_state.json restores position state. A
  second starter would race the watchdog to launch a bot. (The instance lock added in
  "safety: instance lock, heartbeat liveness, offset persistence" means the loser now
  exits cleanly instead of double-trading -- but racing at all is still wrong.)

  Each task restarts on failure (3 attempts, 1 min apart) and is allowed to run on battery.

.PARAMETER WhatIf
  Show what would be created without creating anything.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ops\setup_startup_chain.ps1 -WhatIf
  powershell -ExecutionPolicy Bypass -File scripts\ops\setup_startup_chain.ps1
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [string]$BotDir = 'C:\fusion_sniper_bot',
  [string]$ConfigFile = 'config.json'
)

$ErrorActionPreference = 'Stop'

$cfgPath = Join-Path $BotDir $ConfigFile
if (-not (Test-Path $cfgPath)) { throw "Config not found: $cfgPath" }
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json

$mt5 = $cfg.BROKER.mt5_path
$portable = $cfg.BROKER.portable
$python = (Get-Command python).Source
$user = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Test-Path $mt5)) { throw "MT5 terminal not found at $mt5 (BROKER.mt5_path)" }

Write-Host "Bot dir  : $BotDir"
Write-Host "Python   : $python"
Write-Host "MT5      : $mt5  (portable=$portable)"
Write-Host "Run as   : $user"
Write-Host ""

# Restart-on-failure + sane unattended settings, shared by all three tasks.
function New-Settings {
  New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable
}

function Register-One {
  param($Name, $Exe, $Args, $WorkDir, $DelaySpec, $Description)

  $action = New-ScheduledTaskAction -Execute $Exe -Argument $Args -WorkingDirectory $WorkDir
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
  $trigger.Delay = $DelaySpec           # ISO 8601, e.g. PT60S

  if ($PSCmdlet.ShouldProcess($Name, "Register scheduled task")) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $Name `
      -Action $action -Trigger $trigger -Settings (New-Settings) `
      -RunLevel Limited -Description $Description | Out-Null
    Write-Host "  registered: $Name  (delay $DelaySpec)"
  } else {
    Write-Host "  WHATIF  $Name : $Exe $Args   (delay $DelaySpec)"
  }
}

# 1. MT5 terminal. /portable matches BROKER.portable=true, so it uses ITS OWN config
#    directory next to the exe -- which is where the saved broker credentials live.
$mt5Args = if ($portable) { '/portable' } else { '' }
Register-One -Name 'FusionSniper-MT5' -Exe $mt5 -Args $mt5Args `
  -WorkDir (Split-Path $mt5 -Parent) -DelaySpec 'PT60S' `
  -Description 'Fusion Sniper: portable MT5 terminal (60s post-logon delay for network)'

# 2. Watchdog. THIS is what starts the bot -- see the note above about no main_bot task.
Register-One -Name 'FusionSniper-Watchdog' -Exe $python `
  -Args "services\watchdog_monitor.py $ConfigFile" -WorkDir $BotDir -DelaySpec 'PT90S' `
  -Description 'Fusion Sniper: watchdog (owns bot lifecycle + dead-man switch)'

# 3. Telegram command handler.
Register-One -Name 'FusionSniper-Telegram' -Exe $python `
  -Args "services\telegram_command_handler.py $ConfigFile" -WorkDir $BotDir -DelaySpec 'PT90S' `
  -Description 'Fusion Sniper: Telegram command handler'

Write-Host ""
Write-Host "Done. Verify with:  scripts\ops\verify_recovery.ps1"
Write-Host "NOTE: tasks fire At Logon. Without auto-logon the machine will sit at the"
Write-Host "      login screen after a reboot and NONE of this runs."
