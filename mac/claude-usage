#!/usr/bin/env python3
"""Estimate Claude usage from local Claude Code JSONL logs and POST it.

Scans ~/.claude/projects/**/*.jsonl, sums message.usage token fields in
the last 5 hours and 7 days, compares them with configurable caps, and
POSTs the result to /usage.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CONFIG_PATH = Path("~/.config/claude-pager/config.json").expanduser()
CLAUDE_PROJECTS = Path("~/.claude/projects").expanduser()
DEFAULT_CLAUDE_5H_TOKEN_CAP = 2_000_000
DEFAULT_CLAUDE_7D_TOKEN_CAP = 205_000
TIMEOUT = 20


def die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        die(f"invalid JSON in {CONFIG_PATH}: {e}")
    return cfg if isinstance(cfg, dict) else {}


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def usage_tokens(usage: dict[str, Any]) -> int:
    return (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("output_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
    )


def iter_usage_records() -> list[tuple[datetime, int]]:
    if not CLAUDE_PROJECTS.exists():
        return []
    records: list[tuple[datetime, int]] = []
    cutoff_mtime = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()
    for path in CLAUDE_PROJECTS.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff_mtime:
                continue
        except OSError:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = obj.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(usage, dict):
                continue
            ts = parse_ts(obj.get("timestamp"))
            if ts is None:
                continue
            tokens = usage_tokens(usage)
            if tokens > 0:
                records.append((ts, tokens))
    return records


def reset_seconds(earliest: datetime | None, window: timedelta, now: datetime) -> int:
    if earliest is None:
        return 0
    return max(0, int(((earliest + window) - now).total_seconds()))


def pct(used: int, cap: int) -> float:
    return round((used / max(1, cap)) * 100, 2)


def _override_percent(value: Any) -> float | None:
    if value is None:
        return None
    return max(0.0, min(100.0, float(value)))


def build_payload(
    cap_5h: int,
    cap_7d: int,
    *,
    override_5h: float | None = None,
    override_7d: float | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    window_5h = timedelta(hours=5)
    window_7d = timedelta(days=7)
    cutoff_5h = now - window_5h
    cutoff_7d = now - window_7d
    total_5h = 0
    total_7d = 0
    earliest_5h: datetime | None = None
    earliest_7d: datetime | None = None
    for ts, tokens in iter_usage_records():
        if ts >= cutoff_7d:
            total_7d += tokens
            earliest_7d = ts if earliest_7d is None or ts < earliest_7d else earliest_7d
        if ts >= cutoff_5h:
            total_5h += tokens
            earliest_5h = ts if earliest_5h is None or ts < earliest_5h else earliest_5h
    return {
        "claude": {
            "primary": {
                "used_percent": override_5h if override_5h is not None else pct(total_5h, cap_5h),
                "used": total_5h,
                "cap": cap_5h,
                "window_minutes": 300,
                "resets_in_seconds": reset_seconds(earliest_5h, window_5h, now),
                "source": "claude_official_override" if override_5h is not None else "claude_jsonl_estimate",
            },
            "secondary": {
                "used_percent": override_7d if override_7d is not None else pct(total_7d, cap_7d),
                "used": total_7d,
                "cap": cap_7d,
                "window_minutes": 10080,
                "resets_in_seconds": reset_seconds(earliest_7d, window_7d, now),
                "source": "claude_official_override" if override_7d is not None else "claude_jsonl_estimate",
            },
        }
    }


def post_usage(relay: str, secret: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = relay.rstrip("/") + "/usage"
    body = json.dumps(payload).encode("utf-8")
    headers = {"content-type": "application/json"}
    if secret:
        headers["x-device-secret"] = secret
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        die(f"POST {url} failed: HTTP {e.code}: {detail}", code=1)
    except urllib.error.URLError as e:
        die(f"POST {url} failed: {e.reason}", code=1)
    return json.loads(text) if text else {}


def main() -> None:
    cfg = load_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relay", default=cfg.get("worker_base") or cfg.get("relay_url") or "http://127.0.0.1:8787")
    parser.add_argument("--secret", default=cfg.get("device_secret") or os.environ.get("DEVICE_SECRET", ""))
    parser.add_argument("--claude-5h-token-cap", type=int, default=int(cfg.get("claude_5h_token_cap", DEFAULT_CLAUDE_5H_TOKEN_CAP)))
    parser.add_argument("--claude-7d-token-cap", type=int, default=int(cfg.get("claude_7d_token_cap", DEFAULT_CLAUDE_7D_TOKEN_CAP)))
    args = parser.parse_args()

    payload = build_payload(
        args.claude_5h_token_cap,
        args.claude_7d_token_cap,
        override_5h=_override_percent(cfg.get("claude_5h_used_percent")),
        override_7d=_override_percent(cfg.get("claude_7d_used_percent")),
    )
    result = post_usage(args.relay, args.secret, payload)
    claude = result.get("usage", {}).get("claude", {})
    h5 = claude.get("h5", {}).get("pct", payload["claude"]["primary"]["used_percent"])
    d7 = claude.get("d7", {}).get("pct", payload["claude"]["secondary"]["used_percent"])
    print(f"posted claude usage: 5h={h5}% 7d={d7}%")


if __name__ == "__main__":
    main()
