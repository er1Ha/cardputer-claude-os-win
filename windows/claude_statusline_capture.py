#!/usr/bin/env python3
"""Capture Claude Code statusLine rate_limits without replacing the HUD.

Claude Code sends one JSON object on stdin to the configured statusLine
command. This wrapper saves the official rate_limits fields to
~/.claude/usage-status.json, then delegates to the user's previous
statusLine command if one was installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CLAUDE_DIR = Path("~/.claude").expanduser()
STATUS_PATH = CLAUDE_DIR / "usage-status.json"
RAW_STATUS_PATH = CLAUDE_DIR / "usage-statusline-input.json"
DELEGATE_PATH = Path("~/.config/claude-pager/claude_statusline_delegate.txt").expanduser()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_reset_seconds(side: dict[str, Any]) -> int:
    for key in ("resets_in_seconds", "reset_after_seconds"):
        if isinstance(side.get(key), (int, float)):
            return max(0, int(side[key]))
    for key in ("resets_at", "reset_at"):
        if isinstance(side.get(key), (int, float)):
            return max(0, int(float(side[key]) - now_utc().timestamp()))
        if isinstance(side.get(key), str):
            try:
                dt = datetime.fromisoformat(side[key].replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0, int((dt.astimezone(timezone.utc) - now_utc()).total_seconds()))
    return 0


def percent(side: dict[str, Any]) -> float:
    value = side.get("used_percent", side.get("used_percentage", side.get("usage_percent", 0)))
    return max(0.0, min(100.0, float(value or 0)))


def normalize_side(side: dict[str, Any], window_minutes: int) -> dict[str, Any]:
    return {
        "used_percent": percent(side),
        "window_minutes": int(side.get("window_minutes", window_minutes) or window_minutes),
        "resets_in_seconds": parse_reset_seconds(side),
    }


def normalize_rate_limits(data: dict[str, Any]) -> dict[str, Any] | None:
    rate_limits = data.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return None

    primary = rate_limits.get("primary") or rate_limits.get("h5") or rate_limits.get("five_hour")
    secondary = rate_limits.get("secondary") or rate_limits.get("d7") or rate_limits.get("weekly")
    if not isinstance(primary, dict) and not isinstance(secondary, dict):
        return None

    out: dict[str, Any] = {"captured_at": now_utc().isoformat()}
    if isinstance(primary, dict):
        out["primary"] = normalize_side(primary, 300)
    if isinstance(secondary, dict):
        out["secondary"] = normalize_side(secondary, 10080)
    return out


def save_status(raw: str) -> None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return

    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    RAW_STATUS_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    normalized = normalize_rate_limits(data)
    if normalized:
        STATUS_PATH.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")


def run_delegate(raw: str) -> int:
    if not DELEGATE_PATH.exists():
        return 0
    try:
        command = DELEGATE_PATH.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return 0
    if not command:
        return 0
    if "claude_statusline_capture.py" in command:
        return 0
    try:
        proc = subprocess.run(
            command,
            input=raw,
            text=True,
            shell=True,
            capture_output=True,
            timeout=5,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        return 0
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    return proc.returncode


def main() -> None:
    raw = sys.stdin.read()
    save_status(raw)
    raise SystemExit(run_delegate(raw))


if __name__ == "__main__":
    main()
