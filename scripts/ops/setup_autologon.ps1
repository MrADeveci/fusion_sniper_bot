<#
.SYNOPSIS
  Enable Windows auto-logon so a reboot reaches the desktop with no human present.

.DESCRIPTION
  ############################  READ THIS BEFORE RUNNING  ############################
  #
  #  SECURITY TRADE-OFF. Auto-logon means the machine BOOTS TO AN UNLOCKED DESKTOP.
  #  Anyone with physical access to the box has your logged-in session -- including the
  #  MT5 terminal with saved broker credentials, and config.json with the account
  #  password and Telegram token in plaintext.
  #
  #  This is ONLY acceptable if the machine is physically secure.
  #
  #  Worse, the registry method below stores the account password in PLAINTEXT at
  #  HKLM\...\Winlogon\DefaultPassword, readable by any local administrator.
  #
  #  PREFER Sysinternals Autologon (https://learn.microsoft.com/sysinternals/downloads/autologon),
  #  which stores the password as an LSA secret instead of plaintext:
  #      Autologon64.exe -accepteula <user> <domain> <password>
  #  Use this script only if you cannot use that tool.
  #
  ###################################################################################

  Mitigation worth doing either way: set the machine to lock the screen but stay logged
  in is NOT possible here (a locked session still runs the tasks, but auto-logon is what
  gets you past the login screen in the first place). If the box is not physically
  secure, do NOT enable auto-logon -- use a different recovery strategy (e.g. run the
  stack as a Windows Service under a service account, which needs no interactive logon).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\ops\setup_autologon.ps1 -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [string]$UserName = $env:USERNAME,
  [string]$Domain = $env:USERDOMAIN
)

$ErrorActionPreference = 'Stop'
$key = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'

Write-Host ""
Write-Host "  !!  AUTO-LOGON: the machine will boot to an UNLOCKED desktop." -ForegroundColor Yellow
Write-Host "  !!  Only proceed if this machine is PHYSICALLY SECURE." -ForegroundColor Yellow
Write-Host "  !!  The password is stored in PLAINTEXT in the registry by this method." -ForegroundColor Yellow
Write-Host "  !!  Sysinternals Autologon64.exe is the safer alternative (LSA secret)." -ForegroundColor Yellow
Write-Host ""

$sec = Read-Host "Password for $Domain\$UserName (leave EMPTY to abort)" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
  [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))

if ([string]::IsNullOrWhiteSpace($plain)) {
  Write-Host "Aborted - no password entered. Nothing changed."
  exit 1
}

if ($PSCmdlet.ShouldProcess("$Domain\$UserName", "Enable auto-logon")) {
  Set-ItemProperty -Path $key -Name 'AutoAdminLogon'    -Value '1'       -Type String
  Set-ItemProperty -Path $key -Name 'DefaultUserName'   -Value $UserName -Type String
  Set-ItemProperty -Path $key -Name 'DefaultDomainName' -Value $Domain   -Type String
  Set-ItemProperty -Path $key -Name 'DefaultPassword'   -Value $plain    -Type String
  # AutoLogonCount would make it one-shot; we deliberately do NOT set it (we want every boot).
  Remove-ItemProperty -Path $key -Name 'AutoLogonCount' -ErrorAction SilentlyContinue
  Write-Host "Auto-logon ENABLED for $Domain\$UserName."
  Write-Host "Reboot to test. To DISABLE: scripts\ops\disable_autologon.ps1"
} else {
  Write-Host "WHATIF: would set AutoAdminLogon=1, DefaultUserName=$UserName, DefaultDomainName=$Domain, DefaultPassword=<plaintext>"
}
