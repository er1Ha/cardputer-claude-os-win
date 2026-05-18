# Local relay (Windows)

A drop-in replacement for the Cloudflare Worker that runs entirely on your
own PC. The Cardputer talks to this over your home LAN; the relay shells
out to the locally installed `claude` and `codex` CLIs, so every chat
turn consumes your **Claude Pro/Max** or **ChatGPT Plus/Team/Pro**
subscription quota — not pay-as-you-go API credit.

## What it does

- Exposes the same surface as `worker/` (`/ask`, `/ask-text`, `/reset`,
  plus `/codex` and `/usage`).
- Reads `~/.claude/projects/**/*.jsonl` to sum token use in the last 5h
  and 7d windows.
- Reads `~/.codex/sessions/**/rollout-*.jsonl` for Codex's official
  `rate_limits` events (`primary` = 5h window, `secondary` = 7d
  window) and surfaces the same `used_percent` + `resets_at` numbers
  ChatGPT shows you in the browser.

## Requirements

- Windows + Python 3.10+ (no third-party deps; stdlib only).
- `claude` CLI installed and logged into a Pro/Max account.
  ```powershell
  npm install -g @anthropic-ai/claude-code
  claude   # finish OAuth on first run
  ```
- `codex` CLI installed and logged into a ChatGPT account.
  ```powershell
  npm install -g @openai/codex
  codex    # finish OAuth on first run
  ```

## Run

```powershell
cd local_relay
py -3 relay.py --secret "<your-DEVICE_SECRET>"
```

Generate a secret with `openssl rand -base64 32` if you don't have one.

On startup it prints the LAN URL the Cardputer should use. If your PC
has a VPN/Clash-style proxy that hijacks DNS, the auto-detected IP may
be a fake-IP-range address (e.g. `198.18.x.x`); run `ipconfig` to find
the real Wi-Fi/Ethernet adapter IP and use that instead.

## Test (no Cardputer needed)

```powershell
$h = @{ "x-device-secret" = "<your-secret>"; "content-type" = "application/json" }

# Usage dashboard data
Invoke-RestMethod -Uri http://127.0.0.1:8787/usage -Headers $h | ConvertTo-Json -Depth 4

# Claude chat (one-shot, uses Pro/Max quota)
Invoke-RestMethod -Uri http://127.0.0.1:8787/ask   -Method POST -Headers $h `
                  -Body (@{ prompt = "say hi in 5 words" } | ConvertTo-Json)

# Codex chat (uses ChatGPT quota)
Invoke-RestMethod -Uri http://127.0.0.1:8787/codex -Method POST -Headers $h `
                  -Body (@{ prompt = "say hi in 5 words" } | ConvertTo-Json)
```

## Endpoints

| Method | Path                | Body                  | Returns                       |
| ------ | ------------------- | --------------------- | ----------------------------- |
| GET    | `/`                 | —                     | `{"ok": true}` (health)       |
| GET    | `/usage`            | —                     | `{claude: {h5,d7}, codex: {h5,d7}}` |
| POST   | `/ask` / `/ask-text` / `/claude` | `{"prompt":"..."}` | `{transcript, response}` (via `claude -p`) |
| POST   | `/codex`            | `{"prompt":"..."}`    | `{transcript, response}` (via `codex exec`) |
| POST   | `/reset`            | —                     | no-op (local relay is stateless) |

All endpoints except `GET /` require `x-device-secret: <secret>`.

## Cardputer config

Once the Cardputer arrives, point `buddy/device/apps/config.py` at the
relay:

```python
WORKER_BASE   = "http://192.168.x.y:8787"   # LAN IP from ipconfig
DEVICE_SECRET = "<same secret you passed to relay.py>"
```

## Tuning the Claude caps

Claude's quota system doesn't expose `used_percent` like Codex does, so
this script estimates it by summing tokens from your local session
log against a configurable ceiling. Defaults: 200k tokens/5h, 2M
tokens/7d (rough Max plan figures). Adjust via flags:

```powershell
py -3 relay.py --secret <s> --claude-5h-cap 500000 --claude-7d-cap 5000000
```

Codex numbers come straight from the CLI's own log and ignore these
flags entirely.

## Firewall

First time the relay listens on `0.0.0.0`, Windows Defender will pop
up. Allow it on **Private** networks so the Cardputer (on the same
Wi-Fi) can reach it. Do **not** allow Public networks.

## Background / autostart

The relay runs in the foreground. To keep it alive after closing the
PowerShell window:

- Use `pythonw.exe relay.py ...` to launch with no console window.
- Or wrap it as a Task Scheduler task with trigger "At log on".
- Or install [NSSM](https://nssm.cc/) and register it as a Windows
  service.

Setup scripts for these are not included yet.
