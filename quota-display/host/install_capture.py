"""Install the Claude statusLine capture hook.

Copies ``claude_statusline_capture.py`` to ``~/.config/claude-pager/``
and points ``~/.claude/settings.json`` at it. The capture hook runs
every time Claude Code renders its status bar; it saves the live
``rate_limits`` block to ``~/.claude/usage-status.json`` so this
server can serve it without ever hitting the Anthropic API.

Idempotent. Existing settings are merged, not clobbered, and a
``settings.json.bak`` is written before the update.

Run after copying the project to a fresh machine:

    python install_capture.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAPTURE_SRC = HERE / "claude_statusline_capture.py"

TARGET_DIR = Path(os.path.expanduser("~/.config/claude-pager"))
TARGET_PATH = TARGET_DIR / "claude_statusline_capture.py"
SETTINGS_PATH = Path(os.path.expanduser("~/.claude/settings.json"))


def install_script() -> None:
    if not CAPTURE_SRC.exists():
        sys.exit("source script missing: {}".format(CAPTURE_SRC))
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CAPTURE_SRC, TARGET_PATH)
    print("wrote {}".format(TARGET_PATH))


def patch_settings() -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    settings: dict = {}
    if SETTINGS_PATH.exists():
        try:
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            sys.exit("settings.json is not valid JSON: {}".format(exc))
        # Back up before mutating; helpful if something goes sideways.
        backup = SETTINGS_PATH.with_suffix(SETTINGS_PATH.suffix + ".bak")
        backup.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        print("backed up to {}".format(backup))

    if sys.platform == "win32":
        command = 'py "{}"'.format(TARGET_PATH).replace("\\", "\\\\")
    else:
        command = "python3 {}".format(TARGET_PATH)

    existing = settings.get("statusLine") or {}
    if existing.get("command") == command:
        print("settings already point at capture script — nothing to change")
        return

    if existing.get("command"):
        # Hand off to the previously-configured command so the user
        # keeps whatever HUD they had. The capture script reads this
        # delegate path on each invocation.
        delegate_path = TARGET_DIR / "claude_statusline_delegate.txt"
        delegate_path.write_text(existing["command"], encoding="utf-8")
        print("preserved old statusLine command in {}".format(delegate_path))

    settings["statusLine"] = {"type": "command", "command": command}
    SETTINGS_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("updated {}".format(SETTINGS_PATH))


def main() -> None:
    install_script()
    patch_settings()
    print(
        "\nDone. Launch Claude Code once (`claude`) so the status bar "
        "renders and the first ~/.claude/usage-status.json gets written. "
        "Then run `python host/server.py --once` to confirm the source "
        "is `claude_statusline`."
    )


if __name__ == "__main__":
    main()
