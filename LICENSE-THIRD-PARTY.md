# Third-party licenses

This project's own source code (everything outside `onboard/scripts/vendor/`) is licensed under the Apache License, Version 2.0 — see [`LICENSE`](LICENSE).

The repository ships exactly one third-party Python package, vendored under `onboard/scripts/vendor/`. It retains its upstream license; the full license text is preserved alongside the source in the corresponding `*.dist-info/` directory.

## Inventory

| Package | Version | License | Source |
|---|---|---|---|
| pyserial | 3.5 | BSD-3-Clause | https://github.com/pyserial/pyserial |

BSD-3-Clause is permissive and Apache-2.0-compatible. There is no GPL code in this repository.

## What's *not* vendored

`esptool` (GPLv2+) is required at runtime by the flash stage but is **not** bundled with this repository. The skill's preflight installs it via `pip install esptool` on first run if it isn't already in the user's environment. Declared as a runtime dependency in [`requirements.txt`](requirements.txt). For a reproducible setup, run:

```
python3 -m pip install --user -r requirements.txt
```

This separation keeps the repository cleanly Apache-2.0 — no GPL aggregation — while preserving the "clone and run" experience for end users (the auto-install on first run is silent unless something is missing).

If you want to add esptool back as a vendored dependency for offline use, follow the refresh procedure in `onboard/scripts/vendor/__init__.py` (and accept the GPL implications for your distribution).

## Why pyserial is vendored

`pyserial` provides the serial-port abstractions used everywhere in the skill. Vendoring a pinned copy means port enumeration and REPL I/O work on a fresh clone with no pip step at all, which keeps the cold-start path to a single command. Permissive license, single package, no transitive deps — small surface area, large UX win.

## Refresh procedure

See `onboard/scripts/vendor/__init__.py` for the exact pip command and version pin used to regenerate the pyserial vendor tree.
