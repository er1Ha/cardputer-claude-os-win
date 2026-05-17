"""Vendored dependencies for the m5-onboard skill.

This directory contains a pre-installed copy of ``pyserial`` only.
Shipping it with the repo means port enumeration and REPL I/O work
on a fresh clone with no pip step at all, which keeps the cold-
start path to a single command.

esptool is **not** vendored here. It is GPLv2+; vendoring it would
mix license terms inside what we want to keep as a clean Apache-2.0
repository. The skill declares esptool as a pip dependency in
``requirements.txt`` and auto-installs it on first run if it isn't
already in the user's environment.

### How it gets used

``scripts/vendor_path.py`` is the public helper — each script that
needs pyserial calls :func:`vendor_path.ensure_on_syspath` at the
very top, which prepends this directory to ``sys.path``. Any
subsequent ``import serial`` then resolves to the vendored copy.

For subprocess calls to esptool we use
``[sys.executable, "-m", "esptool", ...]``. The subprocess inherits
the parent process's user-site, so a pip-installed esptool is
importable from inside it. ``vendor_path.subprocess_env()`` also
adds this directory to ``PYTHONPATH`` so esptool can find pyserial
from the vendored tree.

### Refresh

To rebuild the vendor tree against a new upstream pyserial:

    cd <repo-root>
    rm -rf onboard/scripts/vendor/serial onboard/scripts/vendor/pyserial-*.dist-info
    python3 -m pip install --target onboard/scripts/vendor \\
        'pyserial==3.5'
    cd onboard/scripts/vendor
    rm -rf __pycache__

    # Restore this __init__.py (pip install --target clobbers it)
    git checkout __init__.py

If you ever want to revive offline-friendly esptool vendoring, add
``'esptool==4.11.0'`` to the same install, then strip the C-extension
packages pip pulls in (cryptography, cffi, _yaml, bitarray, tibs)
and the espefuse / espsecure / esp_rfc2217_server / bin trees that
ship alongside esptool's own source. Be aware that this
re-introduces GPL code into the repo and you'll need to update
LICENSE-THIRD-PARTY.md and NOTICE accordingly.

### Version pins

pyserial==3.5 — pinned because:
  - 3.5 is the current stable; the changelog is sparse and
    backward compat has held for years
"""
