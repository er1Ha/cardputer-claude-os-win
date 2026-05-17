---
name: uiflow2_nvs
description: The UIFlow 2.0 NVS namespace — what install_apps writes and the failure modes you'll see if you get it wrong.
---

# UIFlow 2.0 NVS reference

UIFlow 2.0 stores its runtime config in the ESP-IDF NVS namespace
`"uiflow"`. The onboarder writes one key here — `boot_option` —
to control whether UIFlow's stock launcher or the bundle's
`main.py` takes over after boot. This page documents that key and
the type-tag gotcha that has bitten every NVS write we do.

## The keys we touch

Reads happen via `nvs.get_u8()` for integer keys (and `get_str()`
for strings, which UIFlow uses internally for other config we
don't write). Authoritative source:
https://raw.githubusercontent.com/m5stack/uiflow-micropython/master/m5stack/modules/startup/__init__.py

| Key           | Type | Example value | Notes |
|---------------|------|---------------|-------|
| `boot_option` | u8   | `2`           | 0 = factory test, 1 = UIFlow launcher, 2 = run `/flash/main.py`. `install_apps.py` sets this to `2` when the bundle ships a root `main.py` so our launcher takes the boot flow instead of UIFlow's pairing screen. |

## Failure modes

### 1. Wrong type tag (set_blob where set_str / set_u8 is required)

**Symptom:** Device boots, prints a backtrace ending in
`OSError: (-4354, 'ESP_ERR_NVS_NOT_FOUND')`, reboots, loops forever.

**Cause:** ESP-IDF NVS stores entries under different type tags
per setter. Calling `get_str("k")` against a key written with
`set_blob` returns the "not found" error even though the key
shows up in the namespace listing. UIFlow's startup is strict
about types.

**Fix:** From the REPL:
```python
import esp32
nvs = esp32.NVS("uiflow")
nvs.erase_key("<key>")
nvs.set_str("<key>", "<value>")   # or set_u8 for integer keys
nvs.commit()
```

### 2. Monkey-patching `esp32.NVS` fails

**Symptom:** `AttributeError: 'module' object has no attribute 'NVS'`
when trying to replace the NVS class for debugging.

**Cause:** UIFlow's `esp32` module is a frozen module — module
attributes can't be reassigned at runtime.

**Fix:** Don't instrument; read the startup source directly from
the public GitHub repo to understand what it's doing.

## Writing NVS correctly

Paste this block via REPL paste mode (Ctrl-E / Ctrl-D — the REPL
mishandles indented blocks sent line-by-line):

```python
import esp32
nvs = esp32.NVS("uiflow")
try: nvs.erase_key("boot_option")
except Exception: pass
nvs.set_u8("boot_option", 2)
nvs.commit()
print("NVS-OK")
```

Then hard-reset via DTR/RTS on UART-bridge boards, or via
`mpy_repl.repl_reset()` on native-USB ESP32-S3 boards (the
DTR/RTS pulse is a no-op there). UIFlow boots more reliably off
a real reset than a soft reboot.
