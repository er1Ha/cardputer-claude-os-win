# Handoff — quota-display next steps

PR: https://github.com/er1Ha/cardputer-claude-os-win/pull/1
Branch: `claude/codex_Creditlimit`
Latest commit at handoff: `e366358`

The **host side is finished and verified live**. The browser dashboard
at `http://localhost:8765/` shows Claude and Codex usage that matches
Claude Code's HUD and Codex CLI's `/status` exactly. What remains is
turning that working server into a daily-usable setup and onto the M5
device.

## What works today

- `python quota-display/host/server.py` serves:
  - `/`            — 240x135 LCD-style pixel-art dashboard
  - `/api/quota`   — full JSON for the dashboard
  - `/api/heartbeat` — flattened JSON shaped for `buddy_ui_cp.update_heartbeat()`
- Claude data source: tries `~/.claude/usage-status.json` (statusLine
  capture, zero API), then `api.anthropic.com/api/oauth/usage` with
  the OAuth token from `~/.claude/.credentials.json`, then the
  claude-hud cache, then token-sum estimate.
- Codex data source: `codex app-server` JSON-RPC
  (`account/rateLimits/read`), with the rollout-file scanner as
  fallback.
- Self-tests (`python quota-display/host/test_server.py`) cover
  build_snapshot end-to-end with both sources mocked.

## Verified numbers (2026-05-20, user 0615008@gmail.com)

| | server | Codex /status | match |
|---|---|---|---|
| Codex 5h | 49% | 51% left (=49% used) | ok |
| Codex 7d | 8% | 92% left (=8% used) | ok |
| Claude 5h | 95% | OAuth live | ok |
| Claude 7d | 10% | OAuth live | ok |

## Open work

### 1. Browser UI sanity check
User hasn't visually confirmed the dashboard renders correctly in
their browser. Numbers are right server-side; need to confirm the
pixel-font CSS, logo SVGs, and tab nav all show as intended. Steps:
- `python quota-display/host/server.py`
- open `http://localhost:8765/`
- expect tabbed CLAUDE/CODEX pixel screens, arrow keys swap.
Google Fonts requests for `Press Start 2P` and `Silkscreen` need to
be reachable; if the user is behind a corporate proxy that blocks
fonts.googleapis.com, package those fonts locally under
`quota-display/web/`.

### 2. Auto-start on Windows (priority: high)
Write `quota-display/windows/install_quota_service.ps1` modeled on
`windows/install_usage_tasks.ps1` from
`origin/claude/check-file-updates-6lPO6`:
- Register a Task Scheduler entry "CardputerQuotaServer".
- Triggers: at logon, repeat indefinitely.
- Action: `pythonw.exe quota-display/host/server.py`.
- Hidden, no console window. CREATE_NO_WINDOW equivalent.
- `-Uninstall` flag for removal.
- Mention firewall: the M5 needs to reach port 8765 from the LAN, so
  the script should add an inbound rule (or document `New-NetFirewallRule
  -DisplayName "Cardputer Quota" -Direction Inbound -LocalPort 8765
  -Protocol TCP -Action Allow`).

### 3. M5 device side (priority: high)
Existing scaffolding at `quota-display/device/`:
- `main.py`, `quota_app.py`, `quota_chat.py`, `buddy_ui_cp.py` (vendored), `config.example.py`.
- `quota_app.py` already connects to Wi-Fi and polls
  `SERVER_URL` (default `http://192.168.1.50:8765/api/heartbeat`) every
  30s, feeding the response into `BuddyUI.update_heartbeat()`.

Remaining tasks:
- Push the files to a Cardputer-Adv (use `buddy/scripts/push.py` in
  the parent repo).
- Device key handling is implemented: Tab/Space toggles panels, comma/A/C
  selects Claude, slash/D/X selects Codex, and Y opens optional AI text chat.
- If Y-key chat is needed on hardware, fill `CHAT_WORKER_BASE` and
  `CHAT_DEVICE_SECRET` in the gitignored device `config.py` first.
- Verify the device LCD rendering matches the browser preview at the
  pixel level. They share `buddy_ui_cp.py` so they should, but
  M5.Lcd's text metrics on DejaVu9 differ slightly from CSS text;
  spot-check 7D row clipping.

### 4. statusLine capture install (priority: medium)
`python quota-display/host/install_capture.py` already exists. It
copies `claude_statusline_capture.py` to
`~/.config/claude-pager/` and patches `~/.claude/settings.json`. User
hasn't run it yet because OAuth works for now. Once OAuth gets 429'd
again, run install_capture then launch `claude` once to populate
`~/.claude/usage-status.json`. Document this in the README's
troubleshooting section.

### 5. VPS / WAN access (priority: low)
If the user later wants to view the dashboard from outside the LAN:
- Add a `?token=...` / `x-device-secret` auth check on `/api/*`.
- Keep config in `host/config.json` (e.g. `"device_secret": "..."`).
- Browser UI already has placeholders for a relay URL+secret input in
  the live preview file on the other branch — mirror that.

### 6. Battery in the dashboard header (priority: low)
The pixel UI shows a battery icon in the LCD header but it's a
decorative SVG. Either fill it with the M5's real battery via a
`/api/battery` endpoint (device → host periodic POST) or remove it
for honesty.

## Repo layout cheatsheet

```
quota-display/
├── README.md
├── HANDOFF.md                  ← this file
├── host/
│   ├── server.py               main entry: --once for snapshot, no flag for HTTP serve
│   ├── config.example.json     copy to config.json
│   ├── test_server.py          self-test
│   ├── claude_statusline_capture.py
│   └── install_capture.py
├── device/
│   ├── main.py                 MicroPython boot
│   ├── quota_app.py            Wi-Fi + poll loop
│   ├── quota_chat.py           optional Y-key AI text chat
│   ├── buddy_ui_cp.py          vendored 240x135 renderer
│   └── config.example.py       Wi-Fi creds + server URL + chat config
└── web/
    └── index.html              dashboard served at GET /
```

## Things not to re-litigate

- `cache_read_input_tokens` is deliberately excluded from the
  token-sum fallback. Don't add it back even though the other branch
  includes it; cache reads bill at ~0.1x and don't trip rate limits.
- The Claude OAuth User-Agent must look like `claude-code/X.Y.Z`. The
  endpoint 429s generic UAs. `_claude_code_version()` probes
  `claude --version`; falls back to "2.1.0" if claude isn't on PATH.
- `codex app-server` JSON-RPC requires the `experimentalApi` capability
  flag in initialize; without it `account/rateLimits/read` returns
  "method not found".
- `find_codex_rate_limits` caps the scan at the newest 5 rollout files
  by mtime, then reads each file backwards looking for the latest
  `token_count` event with a `rate_limits` block. Don't widen the
  scan window — it'll be slow on long histories and the latest data
  is always in the very newest files anyway.
