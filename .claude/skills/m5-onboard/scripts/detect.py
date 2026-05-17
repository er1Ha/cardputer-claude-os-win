"""Find the M5Stack's serial port and identify what's on it.

We filter to known USB-UART bridges first so we don't try to esptool
a Bluetooth-serial port or a debug probe. Then we probe with esptool
to confirm it's really an ESP32 and grab chip ID + flash size.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

# Put the vendored pyserial / esptool on sys.path before any
# third-party import so we use the pinned, pre-installed copy from
# scripts/vendor/ instead of requiring the user to pip-install
# anything. No-op if the vendor directory is missing (falls back to
# system packages + pip-install prompt in onboard.py).
import vendor_path
vendor_path.ensure_on_syspath()

try:
    from serial.tools import list_ports
except ImportError:
    sys.stderr.write("pyserial not installed. Try: pip install pyserial\n")
    raise


# Vendor IDs for the USB-UART bridges M5 actually ships. Anything else
# on the port list is almost certainly not the device we want.
USB_UART_VIDS = {
    0x1A86: "WCH (CH9102/CH340)",
    0x10C4: "Silabs (CP210x)",
    0x0403: "FTDI",
    0x303A: "Espressif (native USB-JTAG on S3/C3)",
}


def candidate_ports() -> list:
    ports = []
    for p in list_ports.comports():
        if p.vid in USB_UART_VIDS:
            ports.append(p)
    return ports


def find_esptool() -> str:
    # PATH first — honors any explicit user install or venv.
    for name in ("esptool.py", "esptool", "esptool.exe"):
        path = shutil.which(name)
        if path:
            return path
    # Fall back to the standard `pip install --user` locations across
    # platforms. pip puts user-installed scripts in different places
    # depending on the OS, and on Windows they're commonly not on PATH
    # until you tick the "add Python to PATH" installer option.
    import glob
    import os
    candidates: list[str] = []
    # macOS framework build
    candidates += glob.glob(os.path.expanduser("~/Library/Python/*/bin/esptool"))
    candidates += glob.glob(os.path.expanduser("~/Library/Python/*/bin/esptool.py"))
    # Linux / macOS posix user-install
    candidates += [
        os.path.expanduser("~/.local/bin/esptool"),
        os.path.expanduser("~/.local/bin/esptool.py"),
    ]
    # Windows user-install: %APPDATA%\Python\Python3XX\Scripts\
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates += glob.glob(
            os.path.join(appdata, "Python", "Python*", "Scripts", "esptool.exe")
        )
        candidates += glob.glob(
            os.path.join(appdata, "Python", "Python*", "Scripts", "esptool.py")
        )
    # Windows python.org all-users install: C:\PythonXX\Scripts\ is
    # sometimes where esptool.exe lands for system-wide installs.
    # Glob rather than a hardcoded version list so we pick up whatever
    # Python is actually installed (3.9, 3.14, anything) without the
    # list bitrotting.
    if os.name == "nt":
        candidates += glob.glob(r"C:\Python*\Scripts\esptool.exe")
        candidates += glob.glob(r"C:\Python*\Scripts\esptool.py")
        # Also check Microsoft Store Python (lives under WindowsApps).
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            candidates += glob.glob(os.path.join(
                localappdata,
                "Packages", "PythonSoftwareFoundation.Python.*",
                "LocalCache", "local-packages", "Python*", "Scripts",
                "esptool.exe",
            ))
    for c in candidates:
        # os.access(..., X_OK) is unreliable on Windows; treat existence
        # of an .exe/.py as sufficient there.
        if os.path.isfile(c) and (os.name == "nt" or os.access(c, os.X_OK)):
            return c
    hint = (
        "esptool not found on PATH or in known user-install locations.\n"
        "Install with:\n"
        "  pip install --user esptool\n"
        "Expected locations per platform:\n"
        "  macOS: ~/Library/Python/3.X/bin/esptool\n"
        "  Linux: ~/.local/bin/esptool\n"
        "  Windows: %APPDATA%\\Python\\Python3XX\\Scripts\\esptool.exe"
    )
    raise RuntimeError(hint)


def probe(port: str) -> dict:
    """Run `esptool chip_id` and parse the output.

    Returns a dict with chip, mac, flash_size, and crystal fields.
    Raises CalledProcessError if the port doesn't respond.
    """
    esptool = find_esptool()
    out = subprocess.check_output(
        [esptool, "--port", port, "--baud", "115200", "flash_id"],
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
    )
    info = {"raw": out, "port": port}
    for line in out.splitlines():
        s = line.strip()
        # esptool <5 says "Chip is X", esptool >=5 says "Chip type: X".
        # Same story for a handful of other fields. Accept both.
        if s.startswith("Chip is "):
            info["chip"] = s.removeprefix("Chip is ").strip()
        elif s.startswith("Chip type:"):
            info["chip"] = s.split(":", 1)[1].strip()
        elif s.startswith("MAC:"):
            info["mac"] = s.split(":", 1)[1].strip()
        elif s.startswith("Detected flash size:"):
            info["flash_size"] = s.split(":", 1)[1].strip()
        elif s.startswith("Flash size:"):
            info["flash_size"] = s.split(":", 1)[1].strip()
        elif s.startswith("Crystal is "):
            info["crystal"] = s.removeprefix("Crystal is ").strip()
        elif s.startswith("Crystal frequency:"):
            info["crystal"] = s.split(":", 1)[1].strip()
    return info


def pick_port(explicit: str | None) -> str:
    if explicit:
        return explicit
    cands = candidate_ports()
    if not cands:
        raise SystemExit(
            "No USB-UART bridge found. Is the device plugged in and powered?\n"
            "Tip: a green LED should be visible on the M5Stack."
        )
    if len(cands) == 1:
        return cands[0].device
    # Multiple candidates — print them and ask. Don't guess.
    sys.stderr.write("Multiple USB-UART devices found:\n")
    for i, p in enumerate(cands):
        vid_name = USB_UART_VIDS.get(p.vid, f"vid={p.vid:#06x}")
        sys.stderr.write(f"  [{i}] {p.device}  ({vid_name}, {p.description})\n")
    sys.stderr.write("Re-run with --port <device> to pick one.\n")
    raise SystemExit(2)


def main() -> int:
    ap = argparse.ArgumentParser(description="Detect an M5Stack on USB.")
    ap.add_argument("--port", help="Explicit serial port (skips autodetect).")
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON for machine consumption.",
    )
    args = ap.parse_args()

    port = pick_port(args.port)
    info = probe(port)

    if args.json:
        # Strip the noisy raw field for JSON output.
        info.pop("raw", None)
        print(json.dumps(info, indent=2))
    else:
        print(f"Port:       {info['port']}")
        print(f"Chip:       {info.get('chip', '?')}")
        print(f"MAC:        {info.get('mac', '?')}")
        print(f"Flash size: {info.get('flash_size', '?')}")
        print(f"Crystal:    {info.get('crystal', '?')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
