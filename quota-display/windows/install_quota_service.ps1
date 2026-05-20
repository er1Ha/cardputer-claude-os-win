# install_quota_service.ps1 - keep the quota dashboard server running on Windows.
#
# Run from an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File quota-display\windows\install_quota_service.ps1
#
# Remove later:
#   powershell -ExecutionPolicy Bypass -File quota-display\windows\install_quota_service.ps1 -Uninstall

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$Python = "pythonw.exe",
    [string]$TaskName = "CardputerQuotaServer",
    [string]$FirewallRuleName = "Cardputer Quota",
    [int]$Port = 8765,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from an elevated PowerShell so it can register the task and firewall rule."
    }
}

Assert-Admin

$serverScript = Join-Path $RepoRoot "quota-display\host\server.py"
$configPath = Join-Path $RepoRoot "quota-display\host\config.json"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName' and firewall rule '$FirewallRuleName'."
    exit 0
}

if (-not (Test-Path $serverScript)) {
    throw "Missing server script: $serverScript"
}

if (-not (Test-Path $configPath)) {
    $example = Join-Path $RepoRoot "quota-display\host\config.example.json"
    if (-not (Test-Path $example)) {
        throw "Missing config.json and config.example.json under quota-display\host."
    }
    Copy-Item $example $configPath
    Write-Host "Created local config: $configPath"
}

$pythonCmd = Get-Command $Python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    throw "Could not find '$Python' on PATH. Pass -Python with the full pythonw.exe path."
}

$argList = "`"$serverScript`""
$action = New-ScheduledTaskAction -Execute $pythonCmd.Source -Argument $argList -WorkingDirectory $RepoRoot

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration ([TimeSpan]::FromDays(3650))

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -Hidden

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName `
    -Action $action `
    -Trigger @($logonTrigger, $repeatTrigger) `
    -Settings $settings `
    -Principal $principal `
    -Description "LAN quota dashboard server for the M5 Cardputer." `
    -Force | Out-Null

$existingRule = Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue
if ($existingRule) {
    Remove-NetFirewallRule -DisplayName $FirewallRuleName
}
New-NetFirewallRule `
    -DisplayName $FirewallRuleName `
    -Direction Inbound `
    -LocalPort $Port `
    -Protocol TCP `
    -Action Allow `
    -Profile Any | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host "Registered scheduled task: $TaskName"
Write-Host "Added firewall rule: $FirewallRuleName TCP/$Port"
Write-Host "Dashboard: http://localhost:$Port/"
Write-Host "M5 heartbeat: http://<this-computer-lan-ip>:$Port/api/heartbeat"
