# Windows artifact sync

This directory is the Windows equivalent of [`../mac/`](../mac/) — it provides
a background sync that polls the Cloudflare Worker every 60 seconds for
completed agent sessions and downloads their artifacts to your machine.

```
windows/
├── claude_pull.py     # The puller. Identical to mac/claude-pull.
├── run_silent.vbs     # Launches claude_pull.py via pythonw, no console.
├── install_task.ps1   # Registers / unregisters the Task Scheduler job.
└── README.md
```

## What gets installed where

| Item | Location |
|------|----------|
| Scheduled task | Task Scheduler → `\` → **ClaudePagerPull** |
| Config | `%USERPROFILE%\.config\claude-pager\config.json` |
| Downloads | `%USERPROFILE%\ClaudeRuns\<session-title>-<id>\` |
| State | `%USERPROFILE%\ClaudeRuns\.state.json` |

The script is **identical** to `mac/claude-pull` — only the extension differs
(`.py` so Windows's file-association can pick the right interpreter).
`notify()` already handles platform branching internally; see the
`if sys.platform == "win32":` block.

## Install

Open **PowerShell** (not Command Prompt) and from the repo root run:

```powershell
powershell -ExecutionPolicy Bypass -File windows\install_task.ps1
```

First run writes a stub config and exits:

```
Wrote stub config to:
  C:\Users\you\.config\claude-pager\config.json
Edit it (set worker_base + device_secret), then re-run this script.
```

Edit the config:

```powershell
notepad $env:USERPROFILE\.config\claude-pager\config.json
```

Fill in your Worker URL and device secret (the same values you put on the
Cardputer), save, and re-run the installer:

```powershell
powershell -ExecutionPolicy Bypass -File windows\install_task.ps1
```

This time it will:

1. Validate the config is no longer stubbed.
2. Register a scheduled task that runs `run_silent.vbs` at logon, repeating
   every 60 seconds.
3. Kick the task once so the first run happens immediately, not 60 seconds
   from now (mirrors `launchctl kickstart -k` on macOS).

The task runs as your normal user — no admin rights, no UAC prompts.

## Verify

```powershell
# Task status
Get-ScheduledTask -TaskName ClaudePagerPull
Get-ScheduledTaskInfo -TaskName ClaudePagerPull

# Run manually (foreground, with output) to debug
python windows\claude_pull.py -v

# Watch for new directories appearing
Get-ChildItem $env:USERPROFILE\ClaudeRuns
```

When a session completes, you should see a Windows toast notification
("Claude session done — <title>"). The toast is rendered via PowerShell
calling into `Windows.UI.Notifications`, no extra packages needed.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File windows\install_task.ps1 -Uninstall
```

This removes the scheduled task but **keeps** your config file and
downloaded artifacts. Delete them manually if desired:

```powershell
Remove-Item -Recurse $env:USERPROFILE\.config\claude-pager
Remove-Item -Recurse $env:USERPROFILE\ClaudeRuns
```

## Troubleshooting

**"pythonw.exe not on PATH"** — re-run the Python installer and tick "Add
Python to PATH", or manually add `%LOCALAPPDATA%\Programs\Python\Python3XX\`
to your PATH.

**Task is registered but never runs** — open Task Scheduler GUI
(`taskschd.msc`), find `ClaudePagerPull` in the library, right-click → Run.
If it errors, the History tab shows why. Common cause: VBS file moved
and the task is pointing at a path that no longer exists; re-run the
installer to refresh.

**Toast notifications not appearing** — check Windows Settings → System →
Notifications. Make sure notifications are on globally and that "Windows
PowerShell" / your script source isn't muted. Some "Focus Assist" /
"Do Not Disturb" settings suppress toasts silently.

**"ExecutionPolicy" error when running install_task.ps1** — use the
`-ExecutionPolicy Bypass` flag shown in the install command above. This
applies *only* to that one script invocation; it doesn't permanently
change your system policy.

**Worker calls keep returning 401** — your `device_secret` doesn't match
the one configured in the Worker. Edit `config.json` and re-run the task
once (`Start-ScheduledTask -TaskName ClaudePagerPull`).

## Why a VBS wrapper?

`pythonw.exe` runs Python without a console window — perfect for
background tasks. But Task Scheduler shows a brief command-prompt flash
when invoking `.exe` directly under some configurations. Routing through
`wscript.exe run_silent.vbs` reliably produces zero visual artifacts on
every Windows 10/11 build I've tested.

If you'd rather invoke `pythonw.exe` directly and skip the VBS, edit
`install_task.ps1` and change the `New-ScheduledTaskAction` line to:

```powershell
$action = New-ScheduledTaskAction `
    -Execute "pythonw.exe" `
    -Argument "`"$PyPath`"" `
    -WorkingDirectory $Here
```
