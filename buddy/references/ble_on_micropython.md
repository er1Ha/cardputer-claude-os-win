# BLE on MicroPython (ESP32) — notes that bit us

## IRQ constants are version-dependent

The numeric event IDs (`_IRQ_CENTRAL_CONNECT = 1`, etc.) have shifted
between MicroPython builds. UIFlow 2.0 / MicroPython 1.22+ matches the
values in `buddy_ble.py`. If porting to an older build, verify against:

```
from micropython import const
# print every event ID the stack emits during a full connect/pair cycle
def debug(ev, d): print("IRQ", ev, d)
bluetooth.BLE().irq(debug)
```

## `gatts_set_buffer` is load-bearing for folder push

The default RX buffer on MicroPython is ~20 bytes — exactly one MTU of
ATT payload. If you don't widen it, a fast burst of `chunk` writes
overflows and drops bytes silently. `gatts_set_buffer(rx_h, 512, True)`
with `append=True` fixes it. This took an embarrassing hour to find.

## `bytes(UUID(...))` returns little-endian

For building advertising payloads by hand, `bytes(bluetooth.UUID(...))`
already gives LE order — do NOT reverse it. The BLE core spec stores
128-bit UUIDs little-endian on the air, so this Just Works.

## LE Secure Connections + DisplayOnly

To get a 6-digit passkey displayed on-device (instead of a Just Works
silent bond or a numeric comparison flow), you need:

```python
ble.config(
    bond=True,
    mitm=True,
    le_secure=True,
    io=bluetooth.IO_CAPABILITY_DISPLAY_ONLY,
)
```

Without `le_secure=True` the stack may fall back to legacy pairing on
older phones/hosts. Without `mitm=True` you get Just Works.

## No clean "erase all bonds"

MicroPython doesn't expose NimBLE's keystore. Toggling `ble.active()`
false/true on ESP32 clears the runtime-side cache but bonds persisted
to NVS can linger. The belt-and-suspenders approach used here:

1. `ble.active(False)` / `ble.active(True)`
2. Erase our own state keys
3. On reconnect, if the host's bond doesn't match, the pairing flow
   restarts — which is the user-visible behavior we want anyway.

## Notification MTU = 20 bytes default

Stay under 20-byte notifications unless both sides negotiate higher.
The desktop does request a larger MTU but until that request arrives
the device has to assume 20. We chunk at 20 everywhere, which is
wasteful at higher MTUs but always correct.
