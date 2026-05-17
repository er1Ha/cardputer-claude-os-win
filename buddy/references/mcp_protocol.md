# Cardputer MCP BLE protocol — reference

The wire format the `cardputer_mcp.py` device app implements, and the
`mcp/server.py` host-side bridge speaks. Kept separate from the
Claude Buddy protocol (see `protocol.md`) because the two apps are
distinct surfaces with different threat models, different evolution
rates, and different MCP-client expectations.

## Status

Iteration 1: documented, not yet implemented. The device-side scaffold
in `cardputer_mcp.py` does not register the GATT service yet; the
host-side `mcp/server.py` has the transport stubbed to stderr. This
file pins the design so iter 2 can wire both sides against a fixed
contract.

## Transport

Nordic-UART-shaped GATT service, line-delimited UTF-8 JSON with `\n`
terminators. Same wire shape as the Buddy protocol — chosen on
purpose so MicroPython knowledge transfers — but a fresh UUID block
so the two apps don't collide on a future build that hosts both.

| Role               | Characteristic UUID                    | Flags               |
| ------------------ | -------------------------------------- | ------------------- |
| Service            | `a5cd0001-c0de-4abe-9c1a-4d5e6f7a8b90` | —                   |
| RX (host → device) | `a5cd0002-c0de-4abe-9c1a-4d5e6f7a8b90` | `WRITE`, `WRITE_NR` |
| TX (device → host) | `a5cd0003-c0de-4abe-9c1a-4d5e6f7a8b90` | `READ`, `NOTIFY`    |

Advertising name: `CardputerMCP_<last 6 hex digits of BT MAC>`.

The `a5cd` UUID prefix is arbitrary but distinctive — it makes the
service easy to spot in a BLE scan. If you change it, update the
host-side constants in `mcp/server.py` (iter 2) and grep this repo
for `a5cd` to catch every place it's referenced.

## Authentication

Same constraints as Buddy: UIFlow 2.0's MicroPython build strips the
pairing API, so the link is unauthenticated on today's firmware.
We rely on a per-device confirmation gesture (described under the
`confirm` tool) for any destructive operation, and we never accept
file pushes over MCP — there's simply no `file` / `chunk` command in
this protocol. See `protocol.md` § Authentication for the broader
discussion that applies here too.

## Inbound (host → device)

All commands carry an `id` string the host generated; the device
echoes that `id` on every ack so the host can match replies to
in-flight requests.

| cmd       | shape                                                                                           | behavior                                                                                                                       |
| --------- | ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ---------------------- | ------------------------------------------------------------ |
| `notify`  | `{"cmd":"notify","id":"...","title":"...","body":"...","urgency":"info"                         | "warn"                                                                                                                         | "crit","agent":"..."}` | Display the notification, beep per urgency, ack immediately. |
| `ask`     | `{"cmd":"ask","id":"...","question":"...","choices":["a","b",...],"timeout_s":N,"agent":"..."}` | Show question + choices, wait for keypress or timeout. Send a `pending` ack on receipt and a resolution ack later (see below). |
| `confirm` | `{"cmd":"confirm","id":"...","title":"...","danger":true,"timeout_s":N,"agent":"..."}`          | Show a danger banner, require hold-Y-for-3 s. Send `pending` ack on receipt + resolution ack later.                            |
| `show`    | `{"cmd":"show","id":"...","text":"...","channel":"agent-tag"}`                                  | Update one line of the ambient status area. Ack immediately. _(iter 4)_                                                        |
| `cancel`  | `{"cmd":"cancel","id":"...","target_id":"..."}`                                                 | Cancel a pending `ask` / `confirm`. Ack with cancellation state.                                                               |
| `ping`    | `{"cmd":"ping","id":"..."}`                                                                     | Round-trip liveness check.                                                                                                     |

The `agent` field carries a short tag (`"claude-code"`, `"cursor"`,
`"managed-agent:sesn_..."` etc.) that the device uses to:

- attribute notifications in the history view
- apply the per-agent rate limit (~1 notify per 60 s; `crit`
  notifications and blocking tools bypass the limit)
- route blocking-tool focus when multiple agents are paired

## Outbound (device → host)

| shape                                                                                | when                                                                                                                                                |
| ------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `{"event":"hello","version":"0.2.0","name":"...","caps":["notify","ask","confirm"]}` | Once, ~1.5 s after the BLE central subscribes to TX (delay is to let the central settle its CCCD write). `caps` lists tools this firmware supports. |
| `{"event":"heartbeat","bat":{...},"rssi":...}`                                       | Every ~10 s while connected, mirroring Buddy's cadence.                                                                                             |
| `{"ack":"<cmd>","id":"...","ok":true}`                                               | Generic success ack — for `notify`, `show`, `ping`, `cancel`.                                                                                       |
| `{"ack":"<cmd>","id":"...","ok":false,"err":"..."}`                                  | Generic failure ack. `err` is a short stable string.                                                                                                |
| `{"ack":"ask","id":"...","pending":true}`                                            | Immediate ack for `ask` / `confirm` indicating the device received the request and is awaiting user input.                                          |
| `{"ack":"ask","id":"...","ok":true,"choice":"..."}`                                  | Resolution: user picked a choice.                                                                                                                   |
| `{"ack":"ask","id":"...","ok":false,"timed_out":true}`                               | Resolution: `timeout_s` elapsed.                                                                                                                    |
| `{"ack":"ask","id":"...","ok":false,"dnd":true}`                                     | Resolution: device was in do-not-disturb mode.                                                                                                      |
| `{"ack":"ask","id":"...","ok":false,"cancelled":true}`                               | Resolution: host sent `cancel` for this id, or user pressed ESC.                                                                                    |
| `{"ack":"confirm","id":"...","ok":true,"confirmed":true,"hold_ms":N}`                | Resolution: user held Y for ≥ 3 s. `hold_ms` is the measured hold duration.                                                                         |
| `{"ack":"confirm","id":"...","ok":false,"cancelled":true}`                           | Resolution: user pressed N or ESC.                                                                                                                  |
| `{"ack":"confirm","id":"...","ok":false,"timed_out":true}`                           | Resolution: `timeout_s` elapsed before the user could complete the hold.                                                                            |

### hello event body

```
{
  "event": "hello",
  "version": "0.1.0",      # firmware version of cardputer_mcp.py
  "name":    "Pip",        # device-set display name (default: "Cardputer")
  "caps":    ["notify", "ask"],   # tool surface this build implements
  "model":   "cardputer-adv",     # or "cardputer" for non-Adv
  "mtu":     20            # current ATT MTU; host should chunk smaller writes accordingly
}
```

### heartbeat body

Mirrors the Buddy heartbeat status payload so device-side state code
can be shared between apps:

```
{
  "event":  "heartbeat",
  "bat":    {"pct": 0|25|50|75|100, "usb": bool},
  "rssi":   -57,           # signed dBm; useful for debugging fade-outs
  "uptime": 142,           # seconds since this app booted
  "dnd":    false          # do-not-disturb state
}
```

## Timing

- 10 s heartbeat interval (matches Buddy).
- 30 s silence → host treats device as gone; subsequent tool calls
  return `unavailable` until reconnection.
- `ask` / `confirm` timeouts are host-supplied (`timeout_s`). The
  device enforces them with a small grace window (~200 ms) so a
  user pressing the key right at the deadline doesn't lose their
  answer to a race.
- BLE IRQ handlers should return within ~5 ms (same constraint as
  Buddy — the stack will drop bytes under back-pressure).

## Chunking

Writes from the host are line-delimited JSON. With the default ATT
MTU of 20 bytes (UIFlow 2.0 negotiates higher with macOS but we
can't rely on it being settled before the first write), the host
chunks each line into ≤ 20 byte fragments and the device reassembles
on RX. The same applies to TX notifications going the other way.

Use `gatts_set_buffer(rx_h, 512, True)` on the device side, same as
Buddy — without `append=True` a fast burst of fragments overflows
the 20-byte default and bytes drop silently. (`ble_on_micropython.md`
documents this trap in more detail.)

## Versioning

The `hello` event carries a `version` string. The host should treat
firmware versions lexicographically: tools listed in `caps` are
safe to call; tools not listed return `unavailable` without ever
hitting the radio. This lets the host advertise the full MVP tool
surface (5 tools at full build-out) while a partially-implemented
device gracefully no-ops the parts it doesn't yet support.

When breaking the wire format, bump the major version and add a
back-compat shim on the host side that recognizes older `hello`
events and downgrades its sent commands. We don't expect to break
the format often — the JSON-line shape is forgiving enough that
most extensions can be additive.
