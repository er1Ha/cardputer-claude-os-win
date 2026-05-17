"""Erase and write a UIFlow firmware image to an M5Stack.

Two speed/protocol profiles are used depending on USB topology:

**UART-bridge boards** (CH9102, CP210x — Basic, Fire, Core2, Tough):
  460800 baud with esptool's RAM stub. Higher rates fail on CH9102
  during erase_flash — observed as lost sync partway through. 460800
  takes ~60 s for a full 16 MB image. That's fine.

**Native-USB boards** (ESP32-S3/C3 — CoreS3, Cardputer, Cardputer-Adv):
  115200 baud with --no-stub. Counter-intuitively FASTER than the stub
  path on native USB, because the chip's USB-Serial/JTAG is a CDC-ACM
  device — the "baud" is cosmetic, transfers happen at USB speed. The
  stub path changes baud rates mid-session to speed up, but that baud
  renegotiation occasionally kills the CDC link mid-flash (observed as
  `Lost connection, retrying... Staying in bootloader.` around 2–5% in).
  --no-stub keeps one baud the whole time and is rock-solid, completing
  an 8 MB flash in ~180 s.

Callers pass `native=True` for ESP32-S3/C3 flows; the caller knows
which kind of port it's talking to.

The flash layout for UIFlow 2.0 on ESP32 is:
  0x1000   bootloader
  0x8000   partition table
  0x10000  application
A full-flash image starts at 0x0 and already contains all three
sections, so we just write the whole blob to offset 0.
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys

import vendor_path
vendor_path.ensure_on_syspath()

from detect import find_esptool

# UART-bridge default. Overridden for native-USB via `native=True`.
FLASH_BAUD = 460800
# Native USB uses the ROM-bootloader initial rate and skips the stub.
# See the module docstring for why.
NATIVE_BAUD = 115200


def _esptool_cmd():
    """Build the base command to invoke esptool.

    Prefer ``python -m esptool`` via the current interpreter — that
    works whether esptool was pip-installed in the user/system
    site-packages (the standard install path; esptool is GPLv2+ and
    is intentionally not vendored) or, hypothetically, sitting in
    sys.path some other way. Subprocesses inherit user-site by
    default so the import resolves.

    Only fall back to hunting the binary on ``$PATH`` / common
    user-install dirs when the importable module isn't found —
    rare, but covers a pip-install that landed in Scripts/ without
    a corresponding site-packages entry.
    """
    if importlib.util.find_spec("esptool") is not None:
        return [sys.executable, "-m", "esptool"]
    return [find_esptool()]


def _run_esptool(args, **kwargs):
    """``subprocess.check_call`` wrapper that uses the vendored env
    when available."""
    env = vendor_path.subprocess_env() if vendor_path.is_available() else None
    return subprocess.check_call(_esptool_cmd() + args, env=env, **kwargs)


def erase(port: str, before: str = "default_reset") -> None:
    _run_esptool(
        ["--port", port, "--baud", str(FLASH_BAUD),
         "--before", before, "erase_flash"],
        timeout=180,
    )


def native_reset(port: str) -> bool:
    """Trigger a watchdog reset on a native-USB ESP32-S3.

    Used as a recovery path when :func:`write`'s inline
    ``--after watchdog-reset`` step fails due to post-flash serial
    desync. This is a second, standalone esptool invocation that
    only does ``--before no_reset --after watchdog-reset flash_id``:
    no ``--before default_reset`` / ``--before usb_reset`` (those
    reset the chip before anything else and would re-enter download
    mode), no RAM stub (same reasons as :func:`write`), minimal
    chip round-trip. ``flash_id`` is a cheap read — we don't care
    about its result, we just need a subcommand so esptool runs
    the ``--after`` step afterward.

    Returns True if the reset round-trip succeeded, False otherwise.
    A False return is the caller's cue to prompt the user to press
    RESET on the device manually.
    """
    cmd = _esptool_cmd() + [
        "--port", port,
        "--baud", str(NATIVE_BAUD),
        "--before", "no_reset",
        "--after", "watchdog_reset",
        "--no-stub",
        "flash_id",
    ]
    env = vendor_path.subprocess_env() if vendor_path.is_available() else None
    try:
        subprocess.run(cmd, timeout=30, check=True,
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL,
                       env=env)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def write(port: str, image: str, offset: str = "0x0",
          before: str = "default_reset",
          after: str = "hard_reset",
          native: bool = False) -> None:
    """Write ``image`` to ``port`` starting at ``offset``.

    Native-USB quirk: esptool's post-flash reset step (``--after
    watchdog-reset``) drives the chip over the same serial link the
    flash just used. If the USB-CDC link hiccupped during the flash —
    which happens occasionally on the Cardputer-Adv during long
    erase_flash operations and the internal retries recover it —
    the link is still in a slightly desynced state when the reset
    command runs, and esptool aborts with StopIteration /
    "A fatal error occurred: The chip stopped responding". The
    flash write at that point is already complete and hash-verified;
    we just can't reset via esptool. Caller can reset via
    ``mpy_repl.repl_reset`` or the device's RESET button.

    This function tolerates that specific failure pattern: we capture
    stdout, look for the post-write success markers, and if they're
    present we treat a final non-zero exit as a soft warning instead
    of propagating. Any earlier failure (e.g. write itself) still
    raises.
    """
    baud = NATIVE_BAUD if native else FLASH_BAUD
    cmd = _esptool_cmd() + [
        "--port", port,
        "--baud", str(baud),
        "--before", before,
        "--after", after,
    ]
    if native:
        # Skip the RAM stub — see module docstring. --no-stub is a
        # global option on esptool and must come before the subcommand.
        cmd.append("--no-stub")
    cmd += ["write_flash", offset, image]

    env = vendor_path.subprocess_env() if vendor_path.is_available() else None
    # Tee esptool's output through to our caller while capturing it,
    # so we can inspect for success markers even on non-zero exit.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
        env=env,
    )
    captured = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stderr.write(line)
        sys.stderr.flush()
        captured.append(line)
    rc = proc.wait(timeout=600)
    out = "".join(captured)

    if rc == 0:
        return

    # The write itself is the big-ticket step. "Hash of data verified"
    # (or the older "Hash of data matches") confirms the bytes on
    # flash match the image. Once we see that, any subsequent failure
    # is either a cosmetic print or a reset step — not something that
    # invalidates the flash. Accept exit codes 1 and 2 after hash
    # verification since esptool uses both for different reset-path
    # failures.
    hash_verified = (
        "Hash of data verified" in out
        or "Hash of data matches" in out
    )
    reset_step_died = (
        "The chip stopped responding" in out
        or "StopIteration" in out
        or "watchdog_reset" in out.lower()
        or "after reset" in out.lower()
    )
    if hash_verified and reset_step_died:
        sys.stderr.write(
            "flash.write: flash content verified OK; "
            "esptool's post-flash reset failed "
            "(known native-USB flake). Continuing — caller should "
            "reset via mpy_repl.repl_reset or manually.\n"
        )
        return

    # Genuine failure — propagate with the same error type the old
    # check_call path raised so callers don't need to learn a new one.
    raise subprocess.CalledProcessError(rc, cmd, output=out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Flash UIFlow to an M5Stack.")
    ap.add_argument("--port", required=True)
    ap.add_argument("--image", required=True, help="Path to the .bin firmware file.")
    ap.add_argument(
        "--offset",
        default="0x0",
        help="Flash offset (default 0x0 for full-flash images).",
    )
    ap.add_argument(
        "--skip-erase",
        action="store_true",
        help="Skip erase_flash (don't use unless you know why).",
    )
    args = ap.parse_args()

    if not args.skip_erase:
        sys.stderr.write("Erasing flash...\n")
        erase(args.port)
    sys.stderr.write(f"Writing {args.image} to {args.offset}...\n")
    write(args.port, args.image, args.offset)
    sys.stderr.write("Flash complete.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
