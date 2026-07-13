<#
.SYNOPSIS
  Read-only check of the reboot-recovery chain. Changes nothing.

.DESCRIPTION
  Run this AFTER a reboot to confirm the stack came back, or any time to see what is
  configured. Every check is read-only.

  The chain, in the order it must hold:

    reboot -> auto-logon -> FusionSniper-MT5 (+60s)      -> terminal64.exe
                         -> FusionSniper-Watchdog (+90s) -> pythonw watchdog_monitor.py
                                                            -> main_bot.py            (wt tab)
                                                            -> telegram_command_handler.py (wt tab)

  The watchdog is the only supervisor and the only process that must not be in a terminal.
  If it is running as python.exe rather than pythonw.exe it has a console, which on this
  machine means Windows Terminal is hosting it -- and it will die with that window, exactly
  as it did on 13/07. That check is FAIL, not a warning.

.EXAMPLE
  powershell -File scripts\ops\verify_recovery.ps1
#>
param([string]$BotDir = 'C:\fusion_sniper_bot')

$ErrorActionPreference = 'Continue'
function Ok($b) { if ($b) { "[ OK ]" } else { "[FAIL]" } }

$os = Get-CimInstance Win32_OperatingSystem
$upMin = ((Get-Date) - $os.LastBootUpTime).TotalMinutes
Write-Host "=== MACHINE ==="
# NOTE: never `Write-Host "..." -f $x` -- -f binds to Write-Host's -ForegroundColor, not to
# the format operator, and the call dies with "cannot convert to ConsoleColor". Format first.
Write-Host ("  booted   : {0}  ({1:N0} min ago)" -f $os.LastBootUpTime, $upMin)
Write-Host "  edition  : $($os.Caption)"
Write-Host ""

Write-Host "=== AUTO-LOGON  (no auto-logon => the machine sits at the lock screen and NOTHING runs) ==="
$w = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$aal = (Get-ItemProperty $w -Name AutoAdminLogon -ErrorAction SilentlyContinue).AutoAdminLogon
$dun = (Get-ItemProperty $w -Name DefaultUserName -ErrorAction SilentlyContinue).DefaultUserName
Write-Host "  $(Ok ($aal -eq '1')) AutoAdminLogon = $aal   user = $dun"
Write-Host ""

Write-Host "=== SCHEDULED TASKS (At Logon) ==="
foreach ($n in 'FusionSniper-MT5', 'FusionSniper-Watchdog') {
  $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
  if ($t) {
    $i = Get-ScheduledTaskInfo -TaskName $n
    $exe = ($t.Actions | Select-Object -First 1).Execute
    # 267011 = 0x00041303 = SCHED_S_TASK_HAS_NOT_RUN. A task that has never run has never
    # been proven, no matter how correct its definition looks.
    $ran = $i.LastTaskResult -ne 267011
    Write-Host "  $(Ok $true) $n  state=$($t.State)"
    Write-Host "         exe    : $exe"
    Write-Host "         $(Ok $ran) lastRun=$($i.LastRunTime)  result=0x$('{0:X8}' -f $i.LastTaskResult)"
  } else {
    Write-Host "  $(Ok $false) $n  NOT REGISTERED"
  }
}
foreach ($gone in 'FusionSniper-Telegram', 'FusionSniper-Stack') {
  if (Get-ScheduledTask -TaskName $gone -ErrorAction SilentlyContinue) {
    Write-Host "  $(Ok $false) $gone still registered -- superseded, it would start a SECOND unsupervised process"
  }
}
Write-Host ""

Write-Host "=== PROCESSES ==="
$mt5 = Get-Process terminal64 -ErrorAction SilentlyContinue
Write-Host "  $(Ok ($null -ne $mt5)) MT5 terminal64.exe  $(if ($mt5) { "(PID $($mt5.Id -join ','))" })"

$py = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue
$wd  = $py | Where-Object { $_.CommandLine -match 'watchdog_monitor' }
$tg  = $py | Where-Object { $_.CommandLine -match 'telegram_command_handler' }
$bot = $py | Where-Object { $_.CommandLine -match 'main_bot' }

Write-Host "  $(Ok ($null -ne $wd))  watchdog_monitor.py         $(if ($wd) { "(PID $($wd.ProcessId -join ','))" })"
if ($wd) {
  # THE check. pythonw = no console = no terminal host = cannot be closed with a window.
  $detached = $wd | ForEach-Object { $_.Name -eq 'pythonw.exe' }
  Write-Host "         $(Ok ($detached -notcontains $false)) image = $($wd.Name -join ',')  (must be pythonw.exe -- python.exe means it lives in a terminal and shares its fate)"
}
Write-Host "  $(Ok ($null -ne $tg))  telegram_command_handler.py $(if ($tg) { "(PID $($tg.ProcessId -join ','))" }) <- started BY the watchdog"
Write-Host "  $(Ok ($null -ne $bot)) main_bot.py                 $(if ($bot) { "(PID $($bot.ProcessId -join ','))" }) <- started BY the watchdog"
Write-Host ""

Write-Host "=== WATCHDOG LOG  (its only voice now it has no console) ==="
$wl = Join-Path $BotDir 'logs\watchdog.log'
if (Test-Path $wl) {
  $wlAge = ((Get-Date) - (Get-Item $wl).LastWriteTime).TotalSeconds
  Write-Host ("  {0} last write {1:N0}s ago" -f (Ok ($wlAge -lt 420)), $wlAge)
  Get-Content $wl -Tail 3 | ForEach-Object { Write-Host "         | $_" }
} else {
  Write-Host "  $(Ok $false) no logs\watchdog.log -- the watchdog has never started"
}
Write-Host ""

Write-Host "=== BOT STATE ==="
$sf = Join-Path $BotDir 'logs\bot_status.json'
if (Test-Path $sf) {
  $s = Get-Content $sf -Raw | ConvertFrom-Json
  $age = ((Get-Date) - [datetime]$s.heartbeat).TotalSeconds
  Write-Host ("  {0} heartbeat {1:N0}s old  (pid {2}, paper={3})" -f (Ok ($age -lt 360)), $age, $s.pid, $s.paper_mode)
} else {
  Write-Host "  $(Ok $false) no bot_status.json -- bot has not started"
}
$lock = Get-ChildItem (Join-Path $BotDir 'logs') -Filter 'bot_*.lock' -ErrorAction SilentlyContinue
Write-Host "  $(Ok ($null -ne $lock)) instance lock: $(if ($lock) { $lock.Name } else { 'none' })"
Write-Host ""

Write-Host "=== WINDOWS UPDATE WINDOW ==="
$ux = 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings'
$hs = (Get-ItemProperty $ux -Name ActiveHoursStart -ErrorAction SilentlyContinue).ActiveHoursStart
$he = (Get-ItemProperty $ux -Name ActiveHoursEnd -ErrorAction SilentlyContinue).ActiveHoursEnd
Write-Host "  active hours     : ${hs}:00 - ${he}:00"
$au = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
$day = (Get-ItemProperty $au -Name ScheduledInstallDay -ErrorAction SilentlyContinue).ScheduledInstallDay
$nar = (Get-ItemProperty $au -Name NoAutoRebootWithLoggedOnUsers -ErrorAction SilentlyContinue).NoAutoRebootWithLoggedOnUsers
$dayName = @{0='every day';1='Sunday';2='Monday';3='Tuesday';4='Wednesday';5='Thursday';6='Friday';7='Saturday'}[[int]$day]
Write-Host "  $(Ok ($day -eq 7)) scheduled install day = $day ($dayName)"
Write-Host "  $(Ok ($nar -eq 1)) NoAutoRebootWithLoggedOnUsers = $nar"
