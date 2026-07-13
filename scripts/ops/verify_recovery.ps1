<#
.SYNOPSIS
  Read-only check of the reboot-recovery chain. Changes nothing.

.DESCRIPTION
  Run this AFTER a reboot to confirm the stack came back, or any time to see what is
  configured. Every check is read-only.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ops\verify_recovery.ps1
#>
param([string]$BotDir = 'C:\fusion_sniper_bot')

$ErrorActionPreference = 'Continue'
function Ok($b) { if ($b) { "[ OK ]" } else { "[FAIL]" } }

$os = Get-CimInstance Win32_OperatingSystem
$upMin = ((Get-Date) - $os.LastBootUpTime).TotalMinutes
Write-Host "=== MACHINE ==="
Write-Host "  booted   : $($os.LastBootUpTime)  ({0:N0} min ago)" -f $upMin
Write-Host "  edition  : $($os.Caption)"
Write-Host ""

Write-Host "=== AUTO-LOGON ==="
$w = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$aal = (Get-ItemProperty $w -Name AutoAdminLogon -ErrorAction SilentlyContinue).AutoAdminLogon
$dun = (Get-ItemProperty $w -Name DefaultUserName -ErrorAction SilentlyContinue).DefaultUserName
Write-Host "  $(Ok ($aal -eq '1')) AutoAdminLogon = $aal   user = $dun"
Write-Host ""

Write-Host "=== SCHEDULED TASKS (At Logon) ==="
foreach ($n in 'FusionSniper-MT5', 'FusionSniper-Watchdog', 'FusionSniper-Telegram') {
  $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
  if ($t) {
    $i = Get-ScheduledTaskInfo -TaskName $n
    Write-Host "  $(Ok $true) $n  state=$($t.State)  lastRun=$($i.LastRunTime)  result=$($i.LastTaskResult)"
  } else {
    Write-Host "  $(Ok $false) $n  NOT REGISTERED"
  }
}
Write-Host ""

Write-Host "=== PROCESSES ==="
$mt5 = Get-Process terminal64 -ErrorAction SilentlyContinue
Write-Host "  $(Ok ($null -ne $mt5)) MT5 terminal64.exe  $(if ($mt5) { "(PID $($mt5.Id -join ','))" })"
$py = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue
$wd = $py | Where-Object { $_.CommandLine -match 'watchdog_monitor' }
$tg = $py | Where-Object { $_.CommandLine -match 'telegram_command_handler' }
$bot = $py | Where-Object { $_.CommandLine -match 'main_bot' }
Write-Host "  $(Ok ($null -ne $wd))  watchdog_monitor.py         $(if ($wd) { "(PID $($wd.ProcessId -join ','))" })"
Write-Host "  $(Ok ($null -ne $tg))  telegram_command_handler.py $(if ($tg) { "(PID $($tg.ProcessId -join ','))" })"
Write-Host "  $(Ok ($null -ne $bot)) main_bot.py                 $(if ($bot) { "(PID $($bot.ProcessId -join ','))" }) <- started BY the watchdog"
Write-Host ""

Write-Host "=== BOT STATE ==="
$sf = Join-Path $BotDir 'logs\bot_status.json'
if (Test-Path $sf) {
  $s = Get-Content $sf -Raw | ConvertFrom-Json
  $age = ((Get-Date) - [datetime]$s.heartbeat).TotalSeconds
  Write-Host "  $(Ok ($age -lt 360)) heartbeat {0:N0}s old  (pid $($s.pid), paper=$($s.paper_mode))" -f $age
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
