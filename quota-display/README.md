# Cardputer Quota Display

A standalone LAN-only quota dashboard for Claude Code and Codex CLI.

```
[Computer running Claude Code / Codex CLI]
   └─ host/server.py
        ├─ parses ~/.claude/ and ~/.codex/ session logs
        ├─ computes rolling 5h + 7d token usage
        ├─ GET /          → browser dashboard
        └─ GET /api/quota → JSON for the M5
              ▲
              │ HTTP over LAN Wi-Fi
[M5 Cardputer]
   └─ device/quota_app.py polls every 30s
        renders 5H + 7D progress bars
```

Internet not required. No Cloudflare, no Anthropic API key on the M5.

## Layout

```
quota-display/
├── host/
│   ├── server.py           single-file server (stdlib only)
│   ├── config.example.json caps + paths + port
│   └── config.json         your local copy (gitignored)
├── device/
│   ├── main.py             MicroPython boot
│   ├── quota_app.py        Wi-Fi poll + screen render
│   ├── buddy_ui_cp.py      vendored UI (240x135 Cardputer panel)
│   ├── config.example.py   Wi-Fi creds + server URL
│   └── config.py           your local copy (gitignored)
└── web/
    └── (dashboard HTML is embedded in server.py)
```

## Setup

### 1. Host (the computer running Claude Code)

```bash
cp host/config.example.json host/config.json
# edit caps if you know them; otherwise leave defaults
python host/server.py
```

Server listens on `0.0.0.0:8765` by default. Browser to
`http://<your-lan-ip>:8765/` to verify.

Give the computer a static DHCP reservation on your router so its IP
doesn't drift.

### 2. M5 Cardputer

Copy `device/` to the device flash (use `buddy/scripts/push.py` from
the parent repo, or `mpremote`):

```bash
cp device/config.example.py device/config.py
# edit WIFI_SSID, WIFI_PASS, SERVER_URL
mpremote cp -r device/ :/
mpremote reset
```

The device reboots into `quota_app.py`, connects to Wi-Fi, and starts
polling the host server every 30 s.

## Sanity-check the parser

Before leaving the server running, point it at your real logs once:

```bash
python host/server.py --once
```

It dumps a snapshot to stdout. If `tokens` is 0 for both `claude` and
`codex`, the log format on your machine doesn't match the parser's
known shapes — grab a sample line and we'll extend the extractor.

There's also an end-to-end self-test that synthesizes fake JSONL files
in a temp directory and verifies the math:

```bash
python host/test_server.py
```

## Data source notes

The 5h / 7d numbers come from parsing local CLI logs:

- **Claude Code**:  `~/.claude/projects/*/*.jsonl` — token usage in
  each event's `message.usage.{input,output,cache_creation}_tokens`.
- **Codex CLI**:    `~/.codex/sessions/**/*.jsonl` — token usage in
  each event's `token_usage` or `usage` block.

`cache_read_input_tokens` is deliberately **not** summed. Cache hits
are billed at ~0.1x and don't trip the subscription rate limit;
counting them inflates totals ~10x during active coding sessions
where the same context is replayed across tool calls.

The parsers are defensive: any line missing a recognized usage field
is skipped. If the upstream log format changes, only the two parsers
in `host/server.py` need updating.

## About the token caps

Neither Anthropic nor OpenAI exposes a "remaining quota" API for
subscriptions, so the caps in `config.json` are local guesses.
The example defaults (200k / 1.4M / 50k / 350k) are rough estimates —
**tune them once you've seen a few days of your own real usage**.
If you hit the real rate limit before the dashboard reads 100%, raise
the cap; if the dashboard reads 100% but the CLI keeps working, lower
it. Restart the server to pick up the new value.
