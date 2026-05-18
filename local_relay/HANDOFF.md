# Handoff — Claude official usage wiring

Status snapshot for whoever picks this up next (likely GPT).

## What's working now

Local relay `/usage` returns **official** Claude + Codex subscription
quotas. Verified on Windows:

```json
"claude": { "h5": {"pct":41,"source":"claude_oauth_api"},
            "d7": {"pct":32,"source":"claude_oauth_api"} },
"codex":  { "h5": {"source":"codex_app_server"},
            "d7": {"source":"codex_app_server"} }
```

Path that delivers each provider:

| Provider | Source | Triggered by |
| --- | --- | --- |
| Claude | `GET api.anthropic.com/api/oauth/usage` (Bearer from `~/.claude/.credentials.json`, UA=`claude-code/<ver>`, β=`oauth-2025-04-20`) | `windows/claude_usage.py` |
| Codex  | `codex app-server` JSON-RPC `account/rateLimits/read` | `windows/codex_usage.py` |

Both are kicked every minute by Task Scheduler tasks
`CardputerClaudeUsage` and `CardputerCodexUsage`, registered via
`windows/install_usage_tasks.ps1`. Tasks run windowless (`pyw`
launcher + `-Hidden` + `CREATE_NO_WINDOW` on the codex/claude child
processes).

## How the Claude OAuth path was found

The "official, software-interface, like Codex" answer for Claude is
`api.anthropic.com/api/oauth/usage`. It's undocumented but used by
every Claude Code HUD/statusline plugin (barkleesanders/claude-hud,
jarrodwatts/claude-hud, leeguooooo/claude-code-usage-bar, etc.). The
UA spoof `claude-code/<version>` is mandatory — other UAs get 429
during active sessions.

Response shape:

```
{
  five_hour:           { utilization: 0-100, resets_at: "<ISO>" },
  seven_day:           { ... },
  seven_day_opus:      { ... },
  seven_day_sonnet:    { ... },
  seven_day_oauth_apps:{ ... },
  extra_usage:         { is_enabled, utilization, used_credits, monthly_limit }
}
```

Fallback chain in `windows/claude_usage.py:main`:
1. OAuth (`payload_from_oauth_api`) — primary, no user setup needed
   beyond `claude login`.
2. `claude.ai/api/organizations/{uuid}/usage` with manual
   `sessionKey` cookie (`payload_from_desktop_usage_api`) — for
   revoked-token corner cases.
3. statusLine capture file `~/.claude/usage-status.json`.
4. JSONL token sum estimate against a configurable cap.

## Verification

```powershell
Start-ScheduledTask CardputerClaudeUsage
$h = @{ "x-device-secret" = "<secret>" }
Invoke-RestMethod -Uri http://127.0.0.1:8787/usage -Headers $h | ConvertTo-Json -Depth 6
```

Expect `claude.h5.source = "claude_oauth_api"` and `pct` matching
claude.ai/settings/usage within a minute.

For deeper debugging:

```cmd
py windows\claude_usage.py --relay http://127.0.0.1:8787 --secret <s> --debug
```

`--debug` prints which source path was taken and why each fallback
fired (`no OAuth token`, `HTTP 401`, unexpected payload keys, etc.).

## Branch / commits

All on `fix/claude-usage-session-key` (branched off
`claude/check-file-updates-6lPO6`):

| SHA | Change |
| --- | --- |
| `65ca628` | sessionKey path + auto org-uuid discovery + `--debug` |
| `a3e331a` | **OAuth path against `api.anthropic.com/api/oauth/usage`** |
| `1daab8b` | `install_usage_tasks.ps1` Task Scheduler installer |
| `bec921c` | tasks use `pyw` (no Python console) |
| `7f81961` | tasks marked `-Hidden` |
| `74219f3` | child subprocesses get `CREATE_NO_WINDOW` |

No conflicts against `claude/check-file-updates-6lPO6`; merges
fast-forward.

## TODO (in priority order)

1. **Mirror to `mac/claude-usage`.** Currently still the old
   JSONL-estimate version. OAuth token source order on macOS:
   `$CLAUDE_CODE_OAUTH_TOKEN` → `security find-generic-password -s
   "Claude Code-credentials" -w` (Keychain) →
   `~/.claude/.credentials.json` → `claudeAiOauth.accessToken`.

2. **OAuth refresh on 401.** `~/.claude/.credentials.json` has
   `expiresAt` + `refreshToken`. On a 401 from `/api/oauth/usage`,
   POST `https://console.anthropic.com/v1/oauth/token` with
   `grant_type=refresh_token&refresh_token=<...>&client_id=<from
   credentials>` and persist the new token. Low priority — running
   `claude` interactively also refreshes.

3. **Optional: collapse into relay.** Move `payload_from_oauth_api`
   into `local_relay/relay.py`'s `claude_window()` with a 60s in-proc
   cache. Removes the Task Scheduler dependency entirely; the relay
   becomes the single thing that needs to run.

4. **Linux secret-tool fallback** (`secret-tool lookup service
   "Claude Code-credentials"`) when porting `claude-usage` to Linux.

## Gotchas

- OAuth path needs **claude-code/<ver>** UA; anything else 429s while
  Claude Code is running. `_claude_code_version()` reads it from
  `claude --version`.
- statusLine `rate_limits` field was added in Claude Code v1.2.80
  and only appears for Pro/Max subscribers after at least one API
  response in the session — that's why the statusLine path was
  unreliable as a primary source.
- `~/.claude/.credentials.json` does **not** contain
  `organizationUuid`. That's a Claude Desktop / web concept. The
  sessionKey path calls `/api/organizations` to discover it.
- Codex `/usage` `pct` is displayed as **remaining** (mode:
  remaining), Claude is **used** (mode: used). The preview HTML
  handles both via the `mode` field. Don't unify them — they reflect
  what the respective official UIs show.
