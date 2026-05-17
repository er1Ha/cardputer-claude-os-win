"""Smoke-test the cardputer-mcp bridge end-to-end without a real MCP client.

Imports the Bridge from server.py, connects to a Cardputer running the
cardputer_mcp.py device app, fires a notify, then an ask. Prints the
results to stdout so a human can verify the flow against what they
see on the device's LCD.

Run from inside the venv:

    cd mcp
    .venv/bin/python smoke_test.py

The first run triggers a BLE scan (5 s) and a macOS Bluetooth-permission
prompt. Subsequent runs use the cached address in
~/.cardputer-mcp/paired.json and are much faster.
"""

import asyncio
import sys

# Make server.py's Bridge class importable when run from the mcp/ dir.
sys.path.insert(0, ".")
from server import Bridge  # noqa: E402


async def main() -> int:
    bridge = Bridge()

    # ---- notify ----------------------------------------------------
    print("→ notify: 'Hello from Claude' (info)")
    result = await bridge.send(
        "notify",
        {
            "title": "Hello from Claude",
            "body": "smoke test from mcp/smoke_test.py",
            "urgency": "info",
        },
        rpc_timeout_s=10,
    )
    print(f"  ← {result}")
    if not result.get("ok"):
        print("  FAIL: notify did not ack ok")
        return 1
    print("  OK — Cardputer should have flashed a banner and chirped.\n")

    # Give the linger window a beat to be visible before we draw the
    # ask question over it. Not required for correctness — just nicer
    # to look at on the device.
    await asyncio.sleep(2)

    # ---- ask -------------------------------------------------------
    print("→ ask: pick a fruit (15s timeout)")
    print("  >>> press 1, 2, or 3 on the Cardputer, or ESC to cancel <<<")
    result = await bridge.send(
        "ask",
        {
            "question": "pick a fruit",
            "choices": ["apple", "banana", "cherry"],
            "timeout_s": 15,
        },
        rpc_timeout_s=25,
    )
    print(f"  ← {result}")

    if result.get("ok") and "choice" in result:
        print(f"  OK — user picked: {result['choice']}\n")
    elif result.get("timed_out"):
        print("  TIMEOUT — no keypress in 15 s (also a valid outcome)\n")
    elif result.get("cancelled"):
        print("  CANCELLED — user pressed ESC\n")
    else:
        print(f"  FAIL: unexpected ack shape: {result}\n")
        return 1

    await asyncio.sleep(1)

    # ---- confirm ---------------------------------------------------
    print("→ confirm: 'DROP customers' (30s timeout)")
    print("  >>> HOLD Y on the Cardputer for 3 seconds to confirm <<<")
    print("  >>> or press N/ESC to cancel <<<")
    result = await bridge.send(
        "confirm",
        {"title": "DROP customers", "danger": True, "timeout_s": 30},
        rpc_timeout_s=40,
    )
    print(f"  ← {result}")

    if result.get("ok") and result.get("confirmed"):
        hold_ms = result.get("hold_ms", 0)
        print(f"  OK — user confirmed (held {hold_ms} ms)\n")
    elif result.get("cancelled"):
        print("  CANCELLED — user backed out (also a valid outcome)\n")
    elif result.get("timed_out"):
        print("  TIMEOUT — no hold completed in 30 s\n")
    else:
        print(f"  FAIL: unexpected ack shape: {result}\n")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
