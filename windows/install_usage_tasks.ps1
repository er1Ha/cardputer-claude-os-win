# install_usage_tasks.ps1 — register two Task Scheduler entries that
# refresh Claude + Codex usage in the local relay every minute.
#
# Run once in an *elevated* PowerShell (right-click → "Run as
# Administrator"). After that, the relay's /usage endpoint stays
# current with what the official Claude/Codex CLIs report, and the
# LIVE preview (and Cardputer) always sees real numbers.
#
# Remove later with:
#   Unregister-ScheduledTask -TaskName "CardputerClaudeUsage"  -Confirm:$false
#   Unregister-ScheduledTask -TaskName "CardputerCodexUsage"   -Confirm:$false

param(
    [string]$RelayUrl     = "http://127.0.0.1:8787",
    [string]$DeviceSecret = "",
    [string]$RepoRoot     = (Split-Path -Parent $PSScriptRoot),
    [string]$Python       = "py"
)

if (-not $DeviceSecret) {
    $cfgPath = Join-Path $env:USERPROFILE ".config\claude-pager\config.json"
    if (Test-Path $cfgPath) {
        try {
            $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
            if ($cfg.device_secret) { $DeviceSecret = $cfg.device_secret }
        } catch {}
    }
}
if (-not $DeviceSecret) {
    Write-Error "Provide -DeviceSecret '<secret>' or set device_secret in ~/.config/claude-pager/config.json"
    exit 1
}

$claudeScript = Join-Path $RepoRoot "windows\claude_usage.py"
$codexScript  = Join-Path $RepoRoot "windows\codex_usage.py"
foreach ($p in @($claudeScript, $codexScript)) {
    if (-not (Test-Path $p)) {
        Write-Error "Missing script: $p (run from inside the repo so RepoRoot resolves correctly, or pass -RepoRoot)"
        exit 1
    }
}

function Register-UsageTask {
    param([string]$Name, [string]$Script)

    $argList = "-3 `"$Script`" --relay `"$RelayUrl`" --secret `"$DeviceSecret`""
    $action  = New-ScheduledTaskAction -Execute $Python -Argument $argList
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
                  -RepetitionInterval (New-TimeSpan -Minutes 1) `
                  -RepetitionDuration ([TimeSpan]::FromDays(365))
    $settings = New-ScheduledTaskSettingsSet `
                  -AllowStartIfOnBatteries `
                  -DontStopIfGoingOnBatteries `
                  -StartWhenAvailable `
                  -MultipleInstances IgnoreNew `
                  -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName $Name `
        -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
        -Description "Push official Claude/Codex subscription usage into the Cardputer relay every minute." `
        -Force | Out-Null

    Write-Host "registered task: $Name → $Python $argList"
}

Register-UsageTask -Name "CardputerClaudeUsage" -Script $claudeScript
Register-UsageTask -Name "CardputerCodexUsage"  -Script $codexScript

Write-Host ""
Write-Host "Done. The tasks start firing within the next minute."
Write-Host "Verify with:"
Write-Host "  Get-ScheduledTaskInfo CardputerClaudeUsage"
Write-Host "  Get-ScheduledTaskInfo CardputerCodexUsage"
Write-Host "Or manually trigger now:"
Write-Host "  Start-ScheduledTask CardputerClaudeUsage; Start-ScheduledTask CardputerCodexUsage"
