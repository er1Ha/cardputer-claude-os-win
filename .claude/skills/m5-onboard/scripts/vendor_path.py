"""Put the bundled vendor/ directory onto sys.path.

Every script in this directory that imports ``serial``, ``esptool``,
or any other third-party package should call
:func:`ensure_on_syspath` as its very first action — before any
third-party import. That way the vendored copy at
``scripts/vendor/`` is picked up rather than whatever the user has
system-wide (or not).

Rationale: shipping the dependencies with the skill removes the pip-
install step, makes the install work offline, and pins known-good
versions so an upstream release doesn't surprise us. See
``vendor/__init__.py`` for the refresh procedure and version pins.

If ``vendor/`` isn't present (e.g. someone downloaded a zip without
it, or pruned it), this helper silently no-ops — the scripts then
fall back to importing from the user's environment and the
``onboard.py`` preflight will prompt for pip-install as a last
resort.
"""

import os
import sys


_THIS_DIR = os.path.abspath(os.path.dirname(__file__))
_VENDOR_DIR = os.path.join(_THIS_DIR, "vendor")


def ensure_on_syspath() -> None:
    """Prepend the vendor directory to ``sys.path`` (idempotent).

    No-op if ``vendor/`` doesn't exist so the scripts degrade to the
    pre-vendor behavior (user-installed esptool/pyserial) instead of
    hard-failing.
    """
    if not os.path.isdir(_VENDOR_DIR):
        return
    # Prepend so the vendored copies win over any system-installed
    # versions with the same name. Check for duplicates so repeated
    # calls from different entrypoints don't bloat sys.path.
    if _VENDOR_DIR not in sys.path:
        sys.path.insert(0, _VENDOR_DIR)


def is_available() -> bool:
    """True if the vendor directory is present."""
    return os.path.isdir(_VENDOR_DIR)


def subprocess_env(base_env=None) -> dict:
    """Return an env dict that makes the vendor dir visible to
    subprocesses (esptool invoked via ``python -m esptool`` etc.).

    Subprocesses don't inherit their parent's ``sys.path``, but they
    DO inherit ``PYTHONPATH`` from the environment. We prepend the
    vendor dir to whatever ``PYTHONPATH`` is already set to, so the
    user's own PYTHONPATH still works.

    ``base_env=None`` starts from a copy of ``os.environ``; pass a
    dict if you want to build from a cleaned environment.
    """
    env = dict(os.environ if base_env is None else base_env)
    if os.path.isdir(_VENDOR_DIR):
        existing = env.get("PYTHONPATH", "")
        if existing:
            env["PYTHONPATH"] = _VENDOR_DIR + os.pathsep + existing
        else:
            env["PYTHONPATH"] = _VENDOR_DIR
    return env


def vendor_dir() -> str:
    """Return the absolute path to the vendor directory, whether or
    not it exists. Useful for diagnostics / pip commands."""
    return _VENDOR_DIR
