# install_task.ps1 — Windows equivalent of mac/install_launchd.sh
#
# Registers claude_pull.py as a scheduled task that runs every 60 seconds
# in the background, idempotently. Run this once after editing your config.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install_task.ps1            # install
#   powershell -ExecutionPolicy Bypass -File install_task.ps1 -Uninstall # remove
#
# No admin rights required — the task runs as the current user.

param(
    [switch]$Uninstall,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$TaskName   = "ClaudePagerPull"
$Here       = $PSScriptRoot
$VbsPath    = Join-Path $Here "run_silent.vbs"
$PyPath     = Join-Path $Here "claude_pull.py"
$ConfigDir  = Join-Path $env:USERPROFILE ".config\claude-pager"
$ConfigPath = Join-Path $ConfigDir "config.json"
$OutDir     = Join-Path $env:USERPROFILE "ClaudeRuns"

# ---------- Uninstall ----------

if ($Uninstall) {
    Write-Host "Removing task: $TaskName"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Done. (Config at $ConfigPath kept; delete manually if needed.)"
    exit 0
}

# ---------- Sanity checks ----------

if (-not (Test-Path $VbsPath)) {
    Write-Error "Missing $VbsPath. Make sure install_task.ps1 sits next to run_silent.vbs and claude_pull.py."
    exit 1
}
if (-not (Test-Path $PyPath)) {
    Write-Error "Missing $PyPath."
    exit 1
}

$pythonw = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if (-not $pythonw) {
    Write-Error "pythonw.exe not on PATH. Install Python 3.9+ from python.org with 'Add to PATH' checked."
    exit 1
}

# ---------- Stub config ----------

if (-not (Test-Path $ConfigPath)) {
    New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
    @"
{
  "worker_base": "https://REPLACE-ME.workers.dev",
  "device_secret": "REPLACE_ME",
  "out_dir": "~/ClaudeRuns",
  "notify": true
}
"@ | Out-File -FilePath $ConfigPath -Encoding utf8 -NoNewline

    Write-Host ""
    Write-Host "Wrote stub config to:"
    Write-Host "  $ConfigPath"
    Write-Host ""
    Write-Host "Edit it (set worker_base + device_secret), then re-run this script."
    exit 0
}

# ---------- Config validation ----------

$cfgText = Get-Content $ConfigPath -Raw
if ($cfgText -match "REPLACE") {
    Write-Error "Refusing to install: $ConfigPath still has REPLACE-ME placeholders. Edit it first."
    exit 1
}

# Make sure the JSON parses, to catch typos before launchd would spam errors.
try {
    $cfg = $cfgText | ConvertFrom-Json
} catch {
    Write-Error "$ConfigPath isn't valid JSON: $_"
    exit 1
}

# ---------- Re-register task ----------

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing task before re-registering..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Write-Host "Registering task: $TaskName"
Write-Host "  vbs:     $VbsPath"
Write-Host "  script:  $PyPath"
Write-Host "  config:  $ConfigPath"
Write-Host "  out dir: $OutDir"

$action = New-ScheduledTaskAction `
    -Execute "wscript.exe" `
    -Argument "`"$VbsPath`"" `
    -WorkingDirectory $Here

# Trigger: run at logon, then repeat every 60 seconds indefinitely.
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Seconds 60) `
    -RepetitionDuration ([TimeSpan]::FromDays(3650))).Repetition

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Claude Pager artifact puller. Polls Cloudflare Worker every 60s." | Out-Null

Write-Host ""
Write-Host "Installed task: $TaskName"
Write-Host ""

# ---------- Kick once, like macOS launchctl kickstart -k ----------

if (-not $NoStart) {
    Write-Host "Kicking the task once so you don't wait 60s for first run..."
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
}

Write-Host ""
Write-Host "Status:    Get-ScheduledTask -TaskName $TaskName"
Write-Host "Run now:   Start-ScheduledTask -TaskName $TaskName"
Write-Host "Stop:      Stop-ScheduledTask -TaskName $TaskName"
Write-Host "Uninstall: powershell -ExecutionPolicy Bypass -File install_task.ps1 -Uninstall"
Write-Host ""
Write-Host "Run it manually anytime:"
Write-Host "  python `"$PyPath`" -v"
