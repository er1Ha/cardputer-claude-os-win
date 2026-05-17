"""cardputer-mcp — pocket pager for AI agents, exposed over MCP.

Iteration 2: real BLE transport.

This process speaks stdio MCP to its client (Claude Code, Cursor, etc.)
and bridges tool calls to a Cardputer running the `cardputer_mcp.py`
device app over Bluetooth Low Energy.

Architecture:

  MCP client  ──stdio──▶  this process  ──BLE/bleak──▶  Cardputer
                                          (a5cd0001-…)

Tool calls become BLE writes; the device's acknowledgments resolve
in-flight asyncio Futures keyed by a per-call `id`. Disconnect events
fail all in-flight RPCs cleanly so the client gets a real error
rather than a hung tool.

Why FastMCP rather than the low-level Server API: this server's tool
surface is small (five tools at full build-out), each with a clean
typed signature. FastMCP's decorator style keeps the call-site code
close to the description text, which is what we'll iterate on most
often as we tune Claude's tool-selection behavior.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Literal, Optional

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from mcp.server.fastmcp import FastMCP


# ---- protocol constants --------------------------------------------
#
# Keep these in sync with buddy/references/mcp_protocol.md and the
# device-side cardputer_mcp.py. If you change a UUID here, change it
# in all three places — there's no central manifest because grepping
# `a5cd` is faster than maintaining a config file.

SERVICE_UUID = "a5cd0001-c0de-4abe-9c1a-4d5e6f7a8b90"
RX_UUID = "a5cd0002-c0de-4abe-9c1a-4d5e6f7a8b90"  # host → device
TX_UUID = "a5cd0003-c0de-4abe-9c1a-4d5e6f7a8b90"  # device → host

# Device advertises as CardputerMCP_<6 hex>; we filter on the prefix.
NAME_PREFIX = "CardputerMCP_"

SCAN_TIMEOUT_S = 5.0
HELLO_TIMEOUT_S = 5.0
DEFAULT_RPC_TIMEOUT_S = 30.0

# When connection fails, suppress retries for this long so we don't
# stall every tool call with a fresh 5-second scan when the device
# is simply not in range. The MCP client will see a fast "unavailable"
# result instead.
FAIL_BACKOFF_S = 30.0

# Where we remember the device's BLE address after first successful
# connect. macOS hands out a per-host UUID rather than the real MAC —
# fine, it's stable across reboots of the laptop.
PAIR_CACHE_DIR = Path.home() / ".cardputer-mcp"
PAIR_CACHE_FILE = PAIR_CACHE_DIR / "paired.json"


def _log(line: str) -> None:
    """Write to stderr, which is what Claude Code surfaces in its MCP
    log pane. Never write to stdout — that's the MCP protocol stream
    and any non-protocol bytes there corrupt the transport."""
    print(f"[cardputer-mcp] {line}", file=sys.stderr, flush=True)


def _load_cached_address() -> Optional[str]:
    try:
        with open(PAIR_CACHE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    addr = data.get("address")
    return addr if isinstance(addr, str) else None


def _save_cached_address(addr: str, name: str) -> None:
    try:
        PAIR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        PAIR_CACHE_FILE.write_text(
            json.dumps(
                {"address": addr, "name": name, "paired_at": int(time.time())}
            )
        )
    except OSError as e:
        _log(f"cache save failed: {e}")


# ---- bridge --------------------------------------------------------


class Bridge:
    """Manages one BLE connection to a Cardputer and correlates RPCs.

    The lifecycle is lazy: `ensure_connected()` is a no-op when the
    link is already up, otherwise it scans (or uses the cached
    address) and waits for the device's `hello` event before
    returning. Every tool call is one or more 20-byte RX writes
    matched to a TX ack by a generated `id` string.

    State machine, simplified:

        disconnected  ──ensure_connected──▶  scanning ──▶ connecting
                                                                │
                                                                ▼
        ◀──disconnected_callback──  connected  ◀── hello-received
    """

    def __init__(self) -> None:
        self.client: Optional[BleakClient] = None
        self.hello: Optional[dict] = None

        self._rx_buf = bytearray()
        self._pending: dict[str, asyncio.Future] = {}
        self._connect_lock = asyncio.Lock()
        self._hello_event = asyncio.Event()

        # Suppress reconnect storms when the device is plainly absent —
        # without this, every tool call eats 5 s of scan time before
        # returning "unavailable", which makes Claude wait forever
        # when the user just hasn't powered the device on.
        self._last_fail_at: Optional[float] = None

    # --- connection lifecycle ---------------------------------------

    async def ensure_connected(self) -> None:
        if self.client and self.client.is_connected and self.hello is not None:
            return

        if (
            self._last_fail_at is not None
            and (time.monotonic() - self._last_fail_at) < FAIL_BACKOFF_S
        ):
            raise ConnectionError(
                f"device not found in last {int(FAIL_BACKOFF_S)} s "
                "(power-on Cardputer, launch the MCP app, then retry)"
            )

        async with self._connect_lock:
            # Re-check under the lock — another caller may have raced
            # us through scan/connect while we were waiting.
            if self.client and self.client.is_connected and self.hello is not None:
                return
            try:
                await self._connect()
                self._last_fail_at = None
            except Exception:
                self._last_fail_at = time.monotonic()
                raise

    async def _connect(self) -> None:
        addr = _load_cached_address()
        if addr:
            try:
                _log(f"connecting to cached address {addr}")
                await self._open_client(addr)
                return
            except (BleakError, asyncio.TimeoutError, ConnectionError) as e:
                _log(f"cached address failed ({e}); falling back to scan")

        addr, name = await self._scan()
        if addr is None:
            raise ConnectionError("no Cardputer-MCP device found in BLE scan")

        _log(f"connecting to discovered {name} ({addr})")
        await self._open_client(addr)
        _save_cached_address(addr, name or "")

    async def _scan(self) -> tuple[Optional[str], Optional[str]]:
        _log(f"scanning for {NAME_PREFIX}* ({SCAN_TIMEOUT_S} s)")
        try:
            # `return_adv=True` makes discover() return a dict
            # {addr: (device, AdvertisementData)} so we can read RSSI
            # and pick the strongest signal when multiple devices are
            # in range.
            discovered = await BleakScanner.discover(
                timeout=SCAN_TIMEOUT_S,
                return_adv=True,
            )
        except BleakError as e:
            _log(f"scan failed: {e}")
            return None, None

        candidates: list[tuple[int, str, str]] = []
        for addr, (device, adv) in discovered.items():
            name = device.name or (adv.local_name if adv else "") or ""
            # Two routes to discovery: name prefix (active scan) or
            # service UUID (passive scan). The device tries to put both
            # in its advertising payload but the radio sometimes
            # rejects rich payloads — see the cascade fallback in
            # `_advertise` on the device side.
            adv_uuids = [str(u).lower() for u in (adv.service_uuids or [])] if adv else []
            if name.startswith(NAME_PREFIX) or SERVICE_UUID in adv_uuids:
                rssi = adv.rssi if (adv and adv.rssi is not None) else -127
                candidates.append((rssi, addr, name or "Cardputer"))

        if not candidates:
            return None, None

        # Strongest RSSI wins. If two devices have the same RSSI, the
        # tuple comparison falls through to address, which is arbitrary
        # but stable — fine for a tiebreaker we don't expect to hit.
        candidates.sort(reverse=True)
        _, addr, name = candidates[0]
        return addr, name

    async def _open_client(self, addr: str) -> None:
        # Tear down any prior client. We've seen bleak hold a "live"
        # client reference that returns is_connected=False but still
        # refuses a new connect() until explicitly disconnected.
        if self.client is not None:
            with suppress(Exception):
                await self.client.disconnect()

        self._rx_buf = bytearray()
        # Fail any RPCs that were somehow still pending from a prior
        # connection — they'd never resolve against a fresh peer.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("reconnecting"))
        self._pending.clear()
        self.hello = None
        self._hello_event.clear()

        self.client = BleakClient(
            addr, disconnected_callback=self._on_disconnect_sync
        )
        await self.client.connect()
        await self.client.start_notify(TX_UUID, self._on_tx)

        # The device sends a `hello` event a moment after the central
        # subscribes to TX. If it doesn't arrive within HELLO_TIMEOUT_S
        # we're probably talking to something that looks like our
        # service but isn't (or an old firmware that doesn't speak the
        # current protocol). Abort the connection so the next call
        # retries cleanly.
        try:
            await asyncio.wait_for(
                self._hello_event.wait(), timeout=HELLO_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            with suppress(Exception):
                await self.client.disconnect()
            raise ConnectionError(
                "device connected but didn't send hello within "
                f"{HELLO_TIMEOUT_S} s (wrong firmware?)"
            )

        caps = (self.hello or {}).get("caps") or []
        _log(f"connected; caps={caps}; mtu={(self.hello or {}).get('mtu')}")

    def _on_disconnect_sync(self, _client: BleakClient) -> None:
        # bleak calls this synchronously from its own thread/loop.
        # Resolve in-flight futures with an error so the tools return
        # promptly rather than hanging on their wait_for(). Future
        # callbacks scheduled via set_exception run on the loop that
        # owns the future, so this is safe across threads.
        _log("BLE disconnected")
        self.hello = None
        self._hello_event.clear()
        for mid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(
                    ConnectionError("device disconnected mid-call")
                )
        self._pending.clear()

    # --- inbound stream ---------------------------------------------

    def _on_tx(self, _char, data: bytearray) -> None:
        """Called by bleak whenever the device pushes bytes on TX.

        TX is chunked at 20 bytes by the device to stay under the
        default ATT MTU; we accumulate until we see a `\\n` and then
        parse one JSON object per line.
        """
        self._rx_buf.extend(data)
        while b"\n" in self._rx_buf:
            line, _, rest = self._rx_buf.partition(b"\n")
            self._rx_buf = bytearray(rest)
            try:
                msg = json.loads(line.decode())
            except (ValueError, UnicodeError) as e:
                _log(f"bad TX line: {e!r} raw={bytes(line)!r}")
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        if "event" in msg:
            ev = msg["event"]
            if ev == "hello":
                self.hello = msg
                self._hello_event.set()
            elif ev == "heartbeat":
                # Heartbeats are advisory in iter 2. Iter 3+ will use
                # them for battery display and DND-state propagation.
                pass
            else:
                _log(f"unknown event: {ev}")
            return

        if "ack" in msg:
            mid = msg.get("id")
            if not isinstance(mid, str):
                # Hello/heartbeat won't have ids, but a malformed ack
                # without one is a protocol error from the device.
                _log(f"ack without id: {msg!r}")
                return
            if msg.get("pending"):
                # Delivery confirmation — the device has received the
                # request but the resolution will arrive later (after
                # user input or timeout). Don't resolve the future yet.
                return
            fut = self._pending.pop(mid, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return

        _log(f"unknown TX shape: {msg!r}")

    # --- outbound RPC ------------------------------------------------

    async def send(
        self,
        cmd: str,
        payload: dict,
        rpc_timeout_s: float = DEFAULT_RPC_TIMEOUT_S,
    ) -> dict:
        """Send one command, await its ack. Returns the ack dict.

        On no-connection / write-fail / timeout, returns a synthetic
        ack with `ok: false` and an `err` so the tool layer can map
        cleanly to a user-visible string without bubbling exceptions.
        """
        try:
            await self.ensure_connected()
        except ConnectionError as e:
            return {"ack": cmd, "ok": False, "err": f"unavailable: {e}"}
        assert self.client is not None

        # Capability gate: tools the device doesn't advertise in
        # `hello.caps` short-circuit here without ever hitting the
        # radio. Older firmware staying compatible with newer tools
        # is the whole reason we negotiate caps.
        caps = (self.hello or {}).get("caps") or []
        if caps and cmd not in caps and cmd not in ("ping", "cancel"):
            return {
                "ack": cmd,
                "ok": False,
                "err": f"device firmware does not advertise '{cmd}'",
            }

        mid = uuid.uuid4().hex[:8]
        msg = {"cmd": cmd, "id": mid, "agent": "mcp-client", **payload}
        data = (json.dumps(msg) + "\n").encode()

        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut

        try:
            mtu = (self.hello or {}).get("mtu") or 20
            for i in range(0, len(data), mtu):
                await self.client.write_gatt_char(
                    RX_UUID, data[i : i + mtu], response=False
                )
        except (BleakError, OSError) as e:
            self._pending.pop(mid, None)
            return {"ack": cmd, "ok": False, "err": f"ble write failed: {e}"}

        try:
            return await asyncio.wait_for(fut, timeout=rpc_timeout_s)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            # Best-effort: tell the device to abandon the request so
            # the LCD doesn't sit on a stale question.
            with suppress(Exception):
                cancel = (
                    json.dumps({"cmd": "cancel", "id": uuid.uuid4().hex[:8], "target_id": mid})
                    + "\n"
                ).encode()
                for i in range(0, len(cancel), mtu):
                    await self.client.write_gatt_char(
                        RX_UUID, cancel[i : i + mtu], response=False
                    )
            return {"ack": cmd, "ok": False, "err": "rpc timeout"}
        except ConnectionError as e:
            return {"ack": cmd, "ok": False, "err": str(e)}


# ---- MCP surface ---------------------------------------------------

bridge = Bridge()
mcp = FastMCP("cardputer")


@mcp.tool()
async def notify(
    title: str,
    body: str = "",
    urgency: Literal["info", "warn", "crit"] = "info",
) -> str:
    """Display a non-blocking notification on the user's Cardputer.

    The Cardputer is a credit-card-sized handheld device the user
    carries with them. Use this tool when you want the user to glance
    at something — a status update, a result, a heads-up — without
    interrupting their main screen.

    Returns once the notification is shown on the device. The
    Cardputer LCD is 240×135 pixels, so keep `title` to ~20 characters
    and `body` to ~3 short lines. `urgency` controls the alert sound:
    'info' is a soft chirp, 'warn' is a louder double-beep, 'crit'
    is an urgent triple-beep. Prefer 'info' for most uses; reserve
    'crit' for things the user needs to react to within seconds.

    Do not call this in rapid succession — agents that spam
    notifications get muted by the device's per-agent rate limit
    (roughly 1 per 60 s) in a later iteration. Returns 'shown',
    'unavailable: <reason>', or 'failed: <reason>'.
    """
    title = title[:64]
    body = body[:240]
    # Notify is non-blocking on the device, so the RPC should resolve
    # within milliseconds. 10 s is generous slack for radio + device
    # render — if it exceeds that something is wrong.
    result = await bridge.send(
        "notify",
        {"title": title, "body": body, "urgency": urgency},
        rpc_timeout_s=10,
    )
    if result.get("ok"):
        return "shown"
    err = result.get("err", "unknown")
    if err.startswith("unavailable"):
        return err
    return f"failed: {err}"


@mcp.tool()
async def ask(
    question: str,
    choices: list[str],
    timeout_s: int = 60,
) -> str:
    """Ask the user a multiple-choice question on their Cardputer.

    BLOCKING — returns once the user presses a number key (1–4) on
    the device's QWERTY keyboard, or after `timeout_s` seconds have
    elapsed. Use when you need a quick decision from the user and
    don't want to interrupt their main screen, especially if they
    might be away from their laptop.

    The Cardputer LCD is 240×135 pixels, so keep `question` short
    (~60 chars wraps to 2 lines) and provide 2–4 short choices (each
    ≤ ~32 chars). The user picks by pressing the digit that matches
    their choice; ESC on the device cancels. Returns one of:

      - the exact choice string the user selected
      - 'timeout' if the user didn't respond in `timeout_s` seconds
      - 'cancelled' if the user pressed ESC on the device, or if
         a follow-up cancel was requested
      - 'unavailable: <reason>' if the device is not connected

    Prefer this over blocking your assistant message with a question
    when the user might not be at their laptop. Do NOT use this for
    destructive operations — call the `confirm` tool (iter 3+)
    instead, which requires a physical hold-to-confirm gesture.
    """
    if len(choices) < 2:
        return "error: need at least 2 choices"
    if len(choices) > 4:
        return "error: at most 4 choices (LCD is small)"
    if timeout_s < 1 or timeout_s > 600:
        return "error: timeout_s must be between 1 and 600"

    # RPC timeout is the device's own input timeout + 10 s slack for
    # radio jitter and the device's grace window. Without slack the
    # host can race the device and return "rpc timeout" when the user
    # genuinely was about to answer.
    rpc_timeout = timeout_s + 10
    result = await bridge.send(
        "ask",
        {
            "question": question[:120],
            "choices": [str(c)[:32] for c in choices],
            "timeout_s": timeout_s,
        },
        rpc_timeout_s=rpc_timeout,
    )

    if result.get("ok") and "choice" in result:
        return str(result["choice"])
    if result.get("timed_out"):
        return "timeout"
    if result.get("cancelled"):
        return "cancelled"
    if result.get("dnd"):
        return "dnd"
    err = result.get("err", "unknown")
    if err.startswith("unavailable"):
        return err
    return f"failed: {err}"


@mcp.tool()
async def confirm(
    title: str,
    timeout_s: int = 30,
) -> str:
    """Demand physical confirmation from the user before executing a
    destructive operation.

    This tool is for IRREVERSIBLE actions only — production deploys,
    force pushes, DROP TABLE / DELETE without WHERE, unstaged-file
    deletions, financial transactions, paid API calls with large
    side effects, etc. The user must physically HOLD the Y key on
    the Cardputer's QWERTY for 3 continuous seconds. A tap does
    nothing; only a sustained physical gesture counts.

    The point is that no amount of tool-output content or prompt
    injection can synthesize a physical key-hold. If you're about to
    do something the user couldn't un-do in a minute, use this
    instead of trusting an `ask` or your own assistant-message
    confirmation.

    Returns one of:
      - 'confirmed' — user physically held Y for ≥3 s
      - 'cancelled' — user pressed N or ESC on the device
      - 'timeout' — user did not respond within `timeout_s` seconds
      - 'unavailable: <reason>' — device not connected

    `title` should fit roughly 18 characters on the device's 240×135
    LCD ("FORCE PUSH origin/main" or "DROP customers"). Keep it
    declarative. The user reading this on a tiny screen must
    instantly recognize the operation.

    Do NOT use this for routine yes/no decisions — that's what
    `ask` is for. Do NOT call this rapidly; every invocation demands
    a deliberate 3-second physical gesture, which is exhausting if
    abused. Reserve this for the handful of actions per session
    where wrong = bad.
    """
    title = title[:64]
    if timeout_s < 5 or timeout_s > 120:
        return "error: timeout_s must be between 5 and 120"

    # RPC timeout is the device's own deadline + slack for radio
    # jitter and the device's hold-detection grace window. Without
    # slack the host can race the device and report rpc-timeout
    # while the user is mid-hold.
    rpc_timeout = timeout_s + 10
    result = await bridge.send(
        "confirm",
        {"title": title, "danger": True, "timeout_s": timeout_s},
        rpc_timeout_s=rpc_timeout,
    )

    if result.get("ok") and result.get("confirmed"):
        # We surface the recorded hold duration to encourage tools
        # that want to log it — most callers will just check the
        # 'confirmed' prefix and move on.
        hold_ms = result.get("hold_ms", 0)
        return f"confirmed (held {hold_ms} ms)"
    if result.get("cancelled"):
        return "cancelled"
    if result.get("timed_out"):
        return "timeout"
    err = result.get("err", "unknown")
    if err.startswith("unavailable"):
        return err
    return f"failed: {err}"


# ---- entrypoint -----------------------------------------------------


def main() -> None:
    _log(f"starting (pid={os.getpid()})")
    # FastMCP.run() defaults to stdio, which is what `claude mcp add`
    # expects. If we ever want HTTP/SSE for the cloud-bridge path,
    # that's a different entry point — leave this one stdio-only.
    mcp.run()


if __name__ == "__main__":
    main()
