<#
.SYNOPSIS
  Confine Windows Update reboots to Saturday, when the market is closed.

.DESCRIPTION
  Two layers, both needed:

  1. ACTIVE HOURS (all editions). Windows will not auto-restart during active hours.
     The max window Windows allows is 18 hours, so it CANNOT cover a 24h trading day --
     active hours alone are necessary but NOT sufficient. We set 01:00-19:00, covering
     the 07:00-18:00 UK session plus margin.

  2. GROUP POLICY scheduled restart (Windows Pro/Enterprise only). This is the layer that
     actually pins the restart to Saturday:
        NoAutoRebootWithLoggedOnUsers = 1   (never yank a reboot out from under a session)
        AUOptions = 4                       (auto download + schedule the install)
        ScheduledInstallDay = 7             (7 = Saturday; 0 = every day)
        ScheduledInstallTime = 3            (03:00)
     Registry equivalent of Computer Config > Administrative Templates > Windows Components
     > Windows Update > "Configure Automatic Updates".

  This machine is Windows 11 Pro, so layer 2 is available. On Home, layer 2 does NOT exist
  and this script will say so rather than half-apply.

  NOTE: none of this makes reboots impossible -- a forced quality/security update can still
  restart the box outside the window. That is exactly why the rest of this work (auto-logon,
  startup chain, dead-man switch) exists: the machine must survive a reboot it did not ask
  for, not merely avoid one.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ops\setup_update_window.ps1 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [int]$ActiveStart = 1,
  [int]$ActiveEnd = 19,
  [int]$InstallDay = 7,      # 7 = Saturday
  [int]$InstallHour = 3
)

$ErrorActionPreference = 'Stop'

$edition = (Get-CimInstance Win32_OperatingSystem).Caption
Write-Host "Edition: $edition"
$isPro = $edition -match 'Pro|Enterprise|Education'

# ---- Layer 1: active hours (all editions) --------------------------------------
$ux = 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings'
if ($PSCmdlet.ShouldProcess("Active hours $ActiveStart..$ActiveEnd", "Set")) {
  New-Item -Path $ux -Force | Out-Null
  Set-ItemProperty -Path $ux -Name 'ActiveHoursStart'      -Value $ActiveStart -Type DWord
  Set-ItemProperty -Path $ux -Name 'ActiveHoursEnd'        -Value $ActiveEnd   -Type DWord
  Set-ItemProperty -Path $ux -Name 'SmartActiveHoursState' -Value 0            -Type DWord
  Write-Host "  active hours set: ${ActiveStart}:00 - ${ActiveEnd}:00 (no auto-restart in this window)"
} else {
  Write-Host "  WHATIF: active hours -> ${ActiveStart}:00-${ActiveEnd}:00"
}

# ---- Layer 2: scheduled restart day (Pro/Enterprise/Education ONLY) -------------
if (-not $isPro) {
  Write-Warning "This edition does NOT support the Windows Update group policy."
  Write-Warning "Layer 2 (pin restarts to Saturday) is UNAVAILABLE. Active hours alone"
  Write-Warning "cannot cover a full trading day (18h max), so reboots remain possible"
  Write-Warning "mid-week. NOT half-applying. Rely on the reboot-recovery chain instead."
  exit 2
}

$au = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
if ($PSCmdlet.ShouldProcess("Windows Update AU policy", "Schedule restarts for day $InstallDay at ${InstallHour}:00")) {
  New-Item -Path $au -Force | Out-Null
  Set-ItemProperty -Path $au -Name 'NoAutoRebootWithLoggedOnUsers' -Value 1            -Type DWord
  Set-ItemProperty -Path $au -Name 'AUOptions'                     -Value 4            -Type DWord
  Set-ItemProperty -Path $au -Name 'ScheduledInstallDay'           -Value $InstallDay  -Type DWord
  Set-ItemProperty -Path $au -Name 'ScheduledInstallTime'          -Value $InstallHour -Type DWord
  gpupdate /target:computer /force | Out-Null
  Write-Host "  policy set: install day=$InstallDay (7=Saturday), time=${InstallHour}:00,"
  Write-Host "              NoAutoRebootWithLoggedOnUsers=1"
} else {
  Write-Host "  WHATIF: AUOptions=4, ScheduledInstallDay=$InstallDay, ScheduledInstallTime=$InstallHour, NoAutoRebootWithLoggedOnUsers=1"
}
