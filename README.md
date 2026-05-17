# Cardputer Claude OS

A DIY "OS" bundle for the [M5Stack Cardputer](https://shop.m5stack.com/) —
flash UIFlow firmware, install a launcher, and ship a tiny suite of apps
that turn the Cardputer into a hand-held Claude device:

- **Claude Buddy** — pair the Cardputer with Claude Code over BLE; watch
  agent runs, token spend, and queue depth from your pocket.
- **Push to Claude** — hold SPACE to record a voice question, release to
  send. Whisper transcribes, Claude Haiku 4.5 replies on the LCD, and a
  per-device 24-hour memory keeps context across turns. Type-mode also
  available for noisy rooms.
- **Claude Pager** — type a task on the QWERTY, fire it off as a
  long-running [Managed Agents] session in the cloud, and watch live
  status (`bash: pytest …`, `wrote auth_test.py`, `idle ✓`) on the LCD.
  Inbox lists active sessions; Detail screen lets you reply, interrupt,
  or approve pending tool calls from your pocket. Pairs with the
  **Central Console** browser UI on your Mac and the `claude-pull`
  artifact sync.
- **Cardputer MCP** — turn the device into a pocket pager for any
  Model-Context-Protocol-speaking agent (Claude Code, Cursor,
  Claude Desktop, Managed Agents, etc.). The agent can buzz you
  with a colored banner + speaker chirp (`notify`), ask a
  multiple-choice question you answer on the QWERTY (`ask`), or
  demand a physical gesture for destructive operations (`confirm`).
  Local stdio MCP server + BLE-GATT peripheral — no cloud, no
  Wi-Fi required.
- **Hello / Snake** — minimal example app + a snake game so the bundle
  isn't all serious business.

[Managed Agents]: https://platform.claude.com/docs/en/managed-agents/overview

> **Forked from** [`moremas/build-with-claude`](https://github.com/moremas/build-with-claude).
> This fork adds the `worker/` directory (a Cloudflare Worker that
> handles voice STT + Claude chat with conversation memory) and the
> `Push to Claude` device app that talks to it. Everything else
> originates from upstream — credit and thanks to the original authors.

## What's new in this fork

| Addition                         | Where                                                                                     | What it does                                                                                                                                                                                                 |
| -------------------------------- | ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Cardputer MCP (host bridge)**  | [`mcp/`](mcp/)                                                                            | Stdio Model Context Protocol server (`bleak`-based) that any Claude or MCP client can register. Three tools: `notify`, `ask`, `confirm`. Talks BLE to the device app — no cloud, no Wi-Fi required.          |
| **Cardputer MCP (device app)**   | [`buddy/device/apps/cardputer_mcp.py`](buddy/device/apps/cardputer_mcp.py)                | BLE GATT peripheral on a fresh service UUID block (`a5cd0001-…`), distinct from Buddy's NUS. Renders notifications, ask-question modals, and a hold-Y confirmation gesture; sends acks via TX notifications. |
| **Cloudflare Worker relay**      | [`worker/`](worker/)                                                                      | Auth-gated edge endpoint. Whisper for STT, Claude Haiku 4.5 for the reply, Workers KV for per-device conversation memory (last 8 messages, 24 h TTL).                                                        |
| **Voice + chat app**             | [`buddy/device/apps/push_to_claude.py`](buddy/device/apps/push_to_claude.py)              | On-device client. Streams WAV to the Worker as it records (flat RAM footprint), text-fallback mode, scrollable replies, `/reset` shortcut.                                                                   |
| **Pager device app**             | [`buddy/device/apps/pager.py`](buddy/device/apps/pager.py)                                | Three-screen UI (Compose / Inbox / Detail) for firing and triaging Managed Agents sessions from the QWERTY. Long-polls the Worker for live event ticker.                                                     |
| **SessionRouter Durable Object** | [`worker/src/router.do.js`](worker/src/router.do.js)                                      | One DO per Anthropic session. Lazily polls the Managed Agents `events.list` endpoint, mirrors events into DO storage, and serves both the Pager (poll) and Console (SSE).                                    |
| **Central Console (browser)**    | [`worker/src/console.html`](worker/src/console.html)                                      | Single-file dark-theme HTML console served from the Worker. Live event stream, syntax-highlighted bash, inline diffs for `str_replace`, file pills, interrupt + reply. Token-gated, no build step.           |
| **Cross-platform artifact sync** | [`mac/claude-pull`](mac/claude-pull) + [`mac/com.claude.pager.pull.plist`](mac/com.claude.pager.pull.plist) (macOS launchd) + [`windows/`](windows/) (Windows Task Scheduler) | Stdlib Python script run every 60 s. Pulls each session's `/workspace/out/` files into `~/ClaudeRuns/<title>-<id>/` and posts a banner notification (macOS `osascript` / Windows toast via PowerShell) when a session completes.                     |
| **Externalized device config**   | [`buddy/device/apps/config.example.py`](buddy/device/apps/config.example.py)              | Worker URL + device secret loaded from a gitignored `config.py` so secrets never enter the repo.                                                                                                             |

See [`worker/README.md`](worker/README.md) for the full Cloudflare deploy
guide.

## Buy a Cardputer

The bundle targets the **M5Stack Cardputer-Adv** (the version with PDM
mic + speaker, required for Push to Claude). Get one direct from
[shop.m5stack.com](https://shop.m5stack.com/) — search "Cardputer".
The original Cardputer (non-Adv) works for everything except the voice
app.

## Quick start — flash a Cardputer

1. Clone this repo locally — anywhere is fine:
   ```bash
   git clone https://github.com/dakshaymehta/cardputer-claude-os.git
   ```
   The skill auto-detects the buddy bundle relative to its own install location, so the clone path doesn't matter. `~/Downloads/m5stack/` and `~/Desktop/m5stack/` are also checked as conventional fallbacks.
2. Plug the Cardputer into your laptop via USB-C
3. Open Claude Code and start a new chat
4. Point Claude Code to the repo folder
5. Type `m5-onboard go`

That's it — Claude will automatically flash the firmware and push the apps onto the device.

### When Claude prompts you to put the device into download mode

Halfway through, Claude will pause and ask you to do this on the **back** of the device:

1. Hold down the **G0** button on the Cardputer
2. While still holding G0, press the **Reset** button
3. Release Reset first, then release G0
4. The screen goes dark — device is in download mode

Claude takes over from there.

### What happens next

- **Firmware writes to the device** (~180 seconds)
- **Apps push to the device** (~100 seconds)
- **Device reboots** straight into the launcher — pick an app and go

Done. Power the device on/off with the side switch.

---

## Quick start — Cardputer MCP (let any agent reach the device)

Turn the Cardputer into a pocket pager that any MCP-speaking client
— Claude Code, Claude Desktop, Cursor, Codex, Managed Agents (via
the future HTTPS bridge), or anything that supports the Model
Context Protocol — can reach. Three tools land on first connect:

- `cardputer.notify(title, body, urgency)` — flash a banner on the
  device and chirp the speaker. Urgency colors the header
  (info=dark, warn=yellow, crit=red) and varies the beep pattern.
  Returns once the banner is shown; auto-clears after 5 s.
- `cardputer.ask(question, choices, timeout_s)` — show a numbered
  multiple-choice question; the user presses 1–4 on the QWERTY;
  the chosen string returns to the agent. Blocks the agent until
  the user answers, ESCs, or `timeout_s` elapses.
- `cardputer.confirm(title, timeout_s)` — display a red danger
  banner and demand a physical gesture before resolving as
  `confirmed`. The whole point is that a prompt injection cannot
  synthesize a sustained physical keypress. Reserve this for
  irreversible operations (deploys, force pushes, DROP TABLE,
  charges, etc.). See _Known limitations_ in
  [`mcp/README.md`](mcp/README.md) for the current gesture
  caveat — the screen says "HOLD Y" but on this build you tap Y
  rapidly to advance the timer.

The whole stack is local — stdio MCP between your client and the
host-side `bleak` bridge, then BLE-GATT to the device. No cloud
trip, no Wi-Fi required. Pairing cache lives at
`~/.cardputer-mcp/paired.json` so reconnects skip the BLE scan.

### Setup

1. **Push the device app** (no firmware re-flash needed if you've
   already onboarded the device):

   ```bash
   python3 .claude/skills/m5-onboard/scripts/install_apps.py \
       --port <PORT> --src buddy
   ```

2. **Set up the host bridge:**

   ```bash
   cd mcp
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Register the MCP server with Claude Code:**

   ```bash
   claude mcp add cardputer \
       "$(pwd)/.venv/bin/python" \
       "$(pwd)/server.py"
   ```

   (Cursor, Codex, Claude Desktop, etc. each have their own
   MCP-server registration UI; point them at the same
   `.venv/bin/python server.py` pair.)

4. **On the device:** boot the launcher, pick **cardputer_mcp**
   from the menu. The screen will read `waiting for bridge` and
   display the device's `CardputerMCP_XXXXXX` BLE name. First
   tool call from the agent triggers a scan + connect; the
   screen flips to a green `READY`.

5. **Try it.** In a fresh Claude Code session:

   > Buzz the Cardputer with title "tests passing" and body
   > "127 ok in 4.2s".

   You should see the banner flash on the device, hear a chirp,
   and Claude gets back `"shown"`. The whole round trip is
   sub-second after the first connect.

### macOS Bluetooth permission

The first BLE scan from the bridge triggers a macOS permission
prompt. Approve it once; `bleak` caches the grant. If Claude Code
runs inside a sandboxed terminal multiplexer that's not seeing
the prompt, grant Bluetooth to the terminal app itself under
**System Settings → Privacy & Security → Bluetooth**.

### Verifying without an MCP client

[`mcp/smoke_test.py`](mcp/smoke_test.py) exercises the bridge
directly (imports `Bridge` and calls the tools as Python), with
no MCP registration needed. Useful for confirming BLE
connectivity, debugging gesture handling, and validating new
firmware before plugging it into a real session:

```bash
cd mcp && .venv/bin/python smoke_test.py
```

It runs a `notify`, then an `ask` (you press 1–3 on the device),
then a `confirm` (you tap Y rapidly per the caveat above).

See [`mcp/README.md`](mcp/README.md) for the full architecture
notes, wire-protocol pointer, known limitations, and roadmap;
the BLE wire format lives in
[`buddy/references/mcp_protocol.md`](buddy/references/mcp_protocol.md).

---

## Quick start — Push to Claude (voice + chat)

The voice app needs a Cloudflare Worker you control. Roughly 10 minutes
of one-time setup; after that every voice/text turn is a single tap.

1. Deploy the Worker — follow [`worker/README.md`](worker/README.md). You'll end up with a Worker URL and a `DEVICE_SECRET` you generated.
2. Point the device at it:
   ```bash
   cp buddy/device/apps/config.example.py buddy/device/apps/config.py
   ```
   Edit `config.py`, paste in your `WORKER_BASE` and `DEVICE_SECRET`.
3. Push the apps to the Cardputer (no firmware re-flash needed):
   ```bash
   python3 .claude/skills/m5-onboard/scripts/install_apps.py --port <PORT> --src buddy
   ```
4. Boot the device → **Push to Claude** → tap SPACE.

`config.py` is gitignored — your secret stays on your machine.

---

## Quick start — Claude Pager + Central Console (cloud agents)

The Pager turns the Cardputer into a remote control + status display
for [Anthropic Managed Agents] sessions. Type a task on the QWERTY,
fire it, and watch the device tick through `bash`, `write`, `idle ✓`
in real time. A self-contained HTML console on your Mac mirrors the
same sessions with a full terminal-style event log; a launchd job
syncs artifacts the agent saves into `~/ClaudeRuns/`.

[Anthropic Managed Agents]: https://platform.claude.com/docs/en/managed-agents/overview

The Pager rides on the same Worker as Push to Claude — finish that
quick start first. Then:

1. **Provision an extra KV namespace** for the session index:

   ```bash
   cd worker
   npx wrangler kv namespace create INDEX
   ```

   Paste the returned id into `worker/wrangler.toml`, replacing
   `REPLACE_WITH_YOUR_INDEX_KV_ID`.

2. **Add the Durable Object migration** (already in `wrangler.toml`)
   and redeploy:

   ```bash
   npx wrangler deploy
   ```

   First deploy registers the `SessionRouter` DO via the v1 migration
   block; subsequent deploys are normal.

3. **Open the Central Console** at
   `https://<your-worker>.workers.dev/console`. On first load it asks
   for your `DEVICE_SECRET` (same value as the Cardputer); the secret
   is stored in browser localStorage and never sent anywhere except
   your Worker. Hit `+ New`, type a task, watch it run.

4. **Push the Pager app** to the Cardputer. It ships as pre-compiled
   bytecode (`.mpy`) because the source-form is too large to parse
   inside the launcher's residual heap:

   ```bash
   pip3 install --user --break-system-packages mpy-cross
   python3 buddy/scripts/push_pager_mpy.py --port <PORT>
   ```

   Reboot the device and pick **Pager** from the launcher menu.

5. **(Optional) Background artifact sync.** Agents save user-facing
   artifacts into `/workspace/out/` inside their container. The
   `claude-pull` script mirrors them to `~/ClaudeRuns/<title>-<id>/`
   on a 60-second schedule and pings you with a desktop notification
   when a session completes.

   **macOS (launchd):**
   ```bash
   ./mac/install_launchd.sh        # writes a stub config and exits
   $EDITOR ~/.config/claude-pager/config.json   # paste worker_base + device_secret
   ./mac/install_launchd.sh        # second run actually loads launchd
   ```
   Logs land at `/tmp/claude-pull.{out,err}.log`. Run manually with
   `./mac/claude-pull -v`.

   **Windows (Task Scheduler):**
   ```powershell
   # In PowerShell, from the repo root:
   powershell -ExecutionPolicy Bypass -File windows\install_task.ps1
   # First run writes %USERPROFILE%\.config\claude-pager\config.json and exits
   notepad $env:USERPROFILE\.config\claude-pager\config.json
   # Fill in worker_base + device_secret, save, then re-run:
   powershell -ExecutionPolicy Bypass -File windows\install_task.ps1
   ```
   The task runs `windows\run_silent.vbs` every 60 seconds with
   `pythonw.exe` (no console flash). Notifications appear as
   Windows toast banners. To check status: `Get-ScheduledTask -TaskName ClaudePagerPull`.
   Run manually for debugging: `python windows\claude_pull.py -v`.

   `claude_pull.py` and `mac/claude-pull` are the same script — the
   Windows copy just has a `.py` extension so `pythonw.exe` can find it.

### Using the Pager

Three screens, switched with the arrow cluster:

```
COMPOSE   ← →   INBOX   →   DETAIL
                            (Enter on a row)
```

- **Compose** — type a task and Enter to launch. `→` jumps to Inbox
  without sending.
- **Inbox** — live list of recent sessions with status pip + last-tool
  subline. Refreshed every 4 s. Up/Down to scroll, Enter to drill into
  Detail, `D` to delete, `N` to jump back to Compose.
- **Detail** — live ticker for one session. Long-polls the Worker so
  deltas show within ~1 s of the agent acting.
  - `R` reply (sends a follow-up message)
  - `I` interrupt (sends `user.interrupt`)
  - `Y/N` approve/deny pending tool confirmation (when present)
  - `Esc` back to Inbox

Notifications fire across **every** screen — the Pager polls the
Worker every 15 s in the background. When an agent transitions:

| Trigger                   | Beep               | Banner                        |
| ------------------------- | ------------------ | ----------------------------- |
| `running` → `idle`        | A5 → E6 chirp      | green **DONE: <title>**       |
| pending tool confirmation | triple D6 urgent   | yellow **NEEDS YOU: <title>** |
| → `terminated`            | A4 → A3 descending | red **ERROR: <title>**        |

State is persisted to `/flash/.pager_notif.json` so the same DONE
doesn't re-fire after a reboot.

### Using the Central Console

Browser tab at `<your-worker>/console`. Token-gated, dark theme,
monospace. Left rail = sessions, main pane = event stream with:

- syntax-highlighted bash blocks
- inline diffs for `str_replace` tool calls
- collapsible tool-result blocks
- pending-confirmation `y`/`n` buttons in the composer
- file pills along the bottom — click to download

Press `n` (when no input is focused) to fire a new task. Use `⌘/Ctrl-Enter`
in the composer or the spawn modal to send.

### Cost guard

Each Managed Agents session keeps a cloud container hot for its
lifetime — typically a few cents to a couple of dollars per task.
The Worker enforces a per-device daily spawn cap (`PAGER_DAILY_SPAWN_CAP`
in `wrangler.toml`, default 30). Bump or lower it to taste.

---

## Using Claude Buddy (BLE)

1. Power on the Cardputer
2. Pick **Claude Buddy** from the launcher menu
3. In Claude Desktop: **Help → Troubleshooting → Enable Developer Tools** (one-time, persists)
4. Then **Developer menu → Hardware Buddy → Connect**

## WiFi auto-connect

The launcher tries to bring up WiFi on every boot and shows the result
on screen — `Connected · IP: 192.168.x.x` on success, `WiFi: offline`
on failure (the launcher always continues either way). Out of the box
the credentials in [`buddy/device/wifi_event.py`](buddy/device/wifi_event.py)
are blank, so you'll see `WiFi: offline`. Edit that file to fill in
your own SSID + password, or remove the `_connect_wifi_with_splash()`
call near the top of `main()` to skip the auto-connect entirely.

## Adding your own app

1. Drop a `.py` file into `buddy/device/apps/`
2. Push just the apps without re-flashing:
   ```bash
   python3 .claude/skills/m5-onboard/scripts/install_apps.py --port <PORT> --src buddy
   ```
3. The launcher auto-discovers the new app on next boot

Crib from `buddy/device/apps/hello_cardputer.py` — it's the smallest example of the conventions (keyboard polling, font, exit behaviour).

## Getting back to stock UIFlow

The buddy bundle takes over the boot flow via `/flash/main.py`. Remove
that file and UIFlow's stock launcher boots normally on the next reset.
From the device REPL:

```python
import os
os.remove('/flash/main.py')
import machine; machine.reset()
```

To also drop the apps under `/flash/apps/`, walk that directory the
same way and remove what you don't want.

If you want a fresh UIFlow firmware on top, re-run `m5-onboard go`
_without_ `--apps`: the skill flashes UIFlow and stops, leaving the
filesystem alone.

---

## Prerequisites

You need **Python 3.10+**, **git**, and **Claude Code** on your laptop. `pyserial` ships vendored inside `.claude/skills/m5-onboard/scripts/vendor/`. `esptool` is GPL-licensed and is **not** vendored — the skill auto-installs it via pip on first run if it isn't already in your environment, so the user-facing experience is still a single command. To pre-install explicitly: `python3 -m pip install --user -r requirements.txt`.

For the Push-to-Claude Worker you also need **Node.js 18+** and a
Cloudflare account; full instructions in [`worker/README.md`](worker/README.md).

Bootstrap if needed:

- **macOS** — `python3` usually pre-installed; if not, `brew install python`
- **Linux (Debian/Ubuntu)** — `sudo apt-get install -y python3 python3-pip git`
- **Windows** — `winget install -e --id Python.Python.3.13` and `winget install -e --id Git.Git`. If `winget` isn't available (older Win10), grab installers from [python.org](https://www.python.org/) (tick **Add Python to PATH** in the installer) and [git-scm.com](https://git-scm.com/).

**Windows shell choice:** all PowerShell snippets in this README work in both the built-in PowerShell 5.1 and PowerShell 7. The legacy `cmd.exe` works too, but the install scripts use PowerShell. Open **PowerShell** from the Start menu, not "Command Prompt".

**Windows + older boards only:** the CH9102 USB-UART driver is needed for Basic / Fire / Core2 / StickC. Download from [WCH](https://www.wch.cn/downloads/CH343SER_EXE.html). Cardputer-Adv and CoreS3 use the in-box composite-USB driver and need nothing extra.

**Want `--apps buddy` to point at a different bundle?** The default resolves to the `buddy/device/` directory next to the skill in this repo, with `~/Downloads/m5stack/` and `~/Desktop/m5stack/` checked as fallbacks. To override (e.g. you maintain a fork or have a customized bundle elsewhere), set `M5_BUDDY_DIR`:

```bash
export M5_BUDDY_DIR=/path/to/buddy/device
```

## Troubleshooting

- **Download-mode prompt keeps retrying** — you're releasing G0 too early. Release Reset first, keep holding G0 for about a second, then release.
- **"No USB-UART bridge found" (older boards)** — install the CH9102 driver on Windows; on macOS/Linux, unplug and replug.
- **Claude Buddy never connects over BLE** — make sure the buddy launcher (not UIFlow's) owns `/flash/main.py`. The skill handles this automatically on install.
- **Push to Claude shows "Not configured"** — copy `config.example.py` to `config.py` and fill in `WORKER_BASE` + `DEVICE_SECRET`, then re-push the apps.
- **Push to Claude returns "unauthorized"** — the `DEVICE_SECRET` in `config.py` doesn't match the one set on the Worker. Re-run `wrangler secret put DEVICE_SECRET` and update `config.py` to match.
- **Something else feels broken** — run `python3 .claude/skills/m5-onboard/scripts/smoke_test.py --port <PORT>` for an I2C + LCD + speaker + button check.

## What's in this repo

- **`.claude/skills/m5-onboard/`** — the Claude Code skill. Detect port, flash UIFlow, install apps. See [`.claude/skills/m5-onboard/SKILL.md`](.claude/skills/m5-onboard/SKILL.md) for the full playbook and every gotcha baked into the scripts.
- **`buddy/`** — the MicroPython app bundle that gets installed. See [`buddy/README.md`](buddy/README.md) for device-side layout and iteration tooling.
- **`worker/`** — the Cloudflare Worker that powers Push to Claude (voice + chat memory). See [`worker/README.md`](worker/README.md) for deploy instructions.
- **`mac/`** — macOS-side background sync. `claude-pull` (Python) + `install_launchd.sh` (bootstraps a launchd agent) + plist template.
- **`windows/`** — Windows-side background sync. `claude_pull.py` (same script as `mac/claude-pull`, just renamed for `pythonw.exe`) + `install_task.ps1` (registers a Task Scheduler job) + `run_silent.vbs` (windowless launcher).
- **`mcp/`** — host-side MCP bridge that lets any MCP client talk to the Cardputer over BLE.

The pieces are decoupled by design: the `m5-onboard` skill can install any bundle via `--apps <path>`, `buddy` is just what ships here, and the worker is optional (only the Push-to-Claude app uses it). The `mac/` and `windows/` directories are independent of everything else — they only matter if you use the Pager app and want artifact sync.

## Contributing

PRs welcome — especially new launcher apps, new boards, and improvements
to the onboarding flow. Open an issue first if you're planning anything
non-trivial. The code is small and intentionally tries to stay readable
end-to-end.

## License

This project's own code is licensed under **Apache 2.0** — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

`pyserial` (BSD-3-Clause, Apache-compatible) is the only third-party package bundled in `.claude/skills/m5-onboard/scripts/vendor/`. `esptool` (GPLv2+) is intentionally not vendored; it's declared as a pip dependency in [`requirements.txt`](requirements.txt) so the repository itself stays cleanly Apache-2.0. See [`LICENSE-THIRD-PARTY.md`](LICENSE-THIRD-PARTY.md) for details.

Forked from [`moremas/build-with-claude`](https://github.com/moremas/build-with-claude); upstream Apache-2.0 license preserved in `LICENSE` and `NOTICE`.
