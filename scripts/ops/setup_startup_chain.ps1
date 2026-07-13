<#
.SYNOPSIS
  Registers the At-Logon startup chain that brings the trading stack back after a reboot.

.DESCRIPTION
  Creates TWO scheduled tasks, both triggered "At log on" of the CURRENT user:

    1. FusionSniper-MT5        +60s   the portable MT5 terminal (network needs a moment)
    2. FusionSniper-Watchdog   +90s   watchdog_monitor.py, via pythonw.exe

  That is the whole startup chain. There is no task for the bot and none for the Telegram
  handler: the WATCHDOG starts both, so it can also restart both. Anything with its own
  task is a process nothing supervises -- which is exactly how the handler stayed dead on
  13/07 while the watchdog that could have revived it had never been asked to.

  WHY pythonw.exe, NOT python.exe
  -------------------------------
  python.exe gets a console. On this machine HKCU:\Console\%%Startup DelegationTerminal is
  the all-zero GUID ("let Windows decide"), which on Win11 hands every new console to
  Windows Terminal -- INCLUDING one started by Task Scheduler. So a python.exe watchdog is
  not an independent process at all: it is a tab in a shared window, and it dies when that
  window does. On 13/07 the watchdog, the handler, the bot and an editor session were all
  consoles of the same WindowsTerminal.exe. It went away at 06:38 and took all four; the
  stack stayed down because the one thing that could have restarted it was inside the thing
  that died. MT5 survived only because it is a GUI app with no terminal host.

  pythonw.exe has NO console, therefore no terminal host, therefore no window that can be
  closed. It logs to logs\watchdog.log instead. The bot and handler may still live in `wt`
  tabs -- they are allowed to be fragile, because the watchdog outlives them and rebuilds
  them within one check interval. The watchdog's own death is covered by the dead-man
  switch (BetterStack), the only monitor that is not on this machine.

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
$pythonw = Join-Path (Split-Path $python -Parent) 'pythonw.exe'
$user = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Test-Path $mt5)) { throw "MT5 terminal not found at $mt5 (BROKER.mt5_path)" }
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found next to $python -- the watchdog needs a console-less interpreter" }

Write-Host "Bot dir  : $BotDir"
Write-Host "Python   : $python"
Write-Host "Pythonw  : $pythonw  (watchdog: no console => no terminal host)"
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
  # NOTE: do NOT name a parameter $Args -- it is a PowerShell automatic variable and the
  # binder refuses to bind a string to it.
  param($Name, $Exe, $Arguments, $WorkDir, $DelaySpec, $Description)

  if ([string]::IsNullOrEmpty($Arguments)) {
    $action = New-ScheduledTaskAction -Execute $Exe -WorkingDirectory $WorkDir
  } else {
    $action = New-ScheduledTaskAction -Execute $Exe -Argument $Arguments -WorkingDirectory $WorkDir
  }
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
  $trigger.Delay = $DelaySpec           # ISO 8601, e.g. PT60S

  if ($PSCmdlet.ShouldProcess($Name, "Register scheduled task")) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $Name `
      -Action $action -Trigger $trigger -Settings (New-Settings) `
      -RunLevel Limited -Description $Description | Out-Null
    Write-Host "  registered: $Name  (delay $DelaySpec)"
  } else {
    Write-Host "  WHATIF  $Name : $Exe $Arguments   (delay $DelaySpec)"
  }
}

# 1. MT5 terminal. /portable matches BROKER.portable=true, so it uses ITS OWN config
#    directory next to the exe -- which is where the saved broker credentials live.
$mt5Args = if ($portable) { '/portable' } else { '' }
Register-One -Name 'FusionSniper-MT5' -Exe $mt5 -Arguments $mt5Args `
  -WorkDir (Split-Path $mt5 -Parent) -DelaySpec 'PT60S' `
  -Description 'Fusion Sniper: portable MT5 terminal (60s post-logon delay for network)'

# 2. The watchdog -- the ONLY supervisor, and the only other task.
#
# pythonw.exe, so it has no console and therefore cannot be hosted by (and killed with)
# Windows Terminal. See the header. It starts and supervises BOTH the bot and the Telegram
# handler, as tabs in a shared terminal window that it is free to rebuild whenever it dies.
Register-One -Name 'FusionSniper-Watchdog' -Exe $pythonw `
  -Arguments "services\watchdog_monitor.py $ConfigFile" -WorkDir $BotDir -DelaySpec 'PT90S' `
  -Description 'Fusion Sniper: watchdog (detached, no console; owns bot + handler lifecycles)'

# Tasks from earlier designs must go. FusionSniper-Telegram in particular: the watchdog now
# starts the handler, so leaving its task registered would start a SECOND, unsupervised one.
# FusionSniper-Stack was a wt-based launcher that this supersedes and that never shipped.
foreach ($old in 'FusionSniper-Telegram', 'FusionSniper-Stack') {
  if (Get-ScheduledTask -TaskName $old -ErrorAction SilentlyContinue) {
    if ($PSCmdlet.ShouldProcess($old, "Unregister superseded task")) {
      Unregister-ScheduledTask -TaskName $old -Confirm:$false
      Write-Host "  removed superseded task: $old"
    }
  }
}

Write-Host ""
Write-Host "Done. Verify with:  scripts\ops\verify_recovery.ps1"
Write-Host "NOTE: tasks fire At Logon. Without auto-logon the machine will sit at the"
Write-Host "      login screen after a reboot and NONE of this runs."
