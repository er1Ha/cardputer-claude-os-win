#!/usr/bin/env python3
"""Post Codex subscription usage from local Codex logs to the relay.

Reads ~/.codex/sessions/**/rollout-*.jsonl, finds the newest rate_limits
payload, and POSTs a compact usage payload to /usage.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONFIG_PATH = Path("~/.config/claude-pager/config.json").expanduser()
CODEX_SESSIONS = Path("~/.codex/sessions").expanduser()
TIMEOUT = 20


def die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"invalid JSON in {CONFIG_PATH}: {e}")
    return cfg if isinstance(cfg, dict) else {}


def parse_event_ts(value: Any, fallback: float) -> datetime:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.fromtimestamp(fallback, tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def latest_codex_rate_limits() -> dict[str, Any] | None:
    if not CODEX_SESSIONS.exists():
        return None
    newest: tuple[datetime, dict[str, Any]] | None = None
    for path in CODEX_SESSIONS.rglob("rollout-*.jsonl"):
        try:
            mtime = path.stat().st_mtime
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if '"rate_limits"' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = obj.get("payload") or {}
            rate_limits = payload.get("rate_limits")
            if isinstance(rate_limits, dict):
                ts = parse_event_ts(obj.get("timestamp"), mtime)
                if newest is None or ts > newest[0]:
                    newest = (ts, rate_limits)
    return newest[1] if newest else None


def resets_in_seconds(side: dict[str, Any]) -> int:
    resets_at = side.get("resets_at")
    if not isinstance(resets_at, (int, float)):
        return 0
    delta = int(resets_at - datetime.now(timezone.utc).timestamp())
    return max(0, delta)


def usage_slot(side: dict[str, Any], window_minutes: int) -> dict[str, Any]:
    return {
        "used_percent": float(side.get("used_percent", 0) or 0),
        "window_minutes": window_minutes,
        "resets_in_seconds": resets_in_seconds(side),
        "source": "codex_rate_limits",
    }


def build_payload() -> dict[str, Any]:
    rate_limits = latest_codex_rate_limits() or {}
    primary = rate_limits.get("primary") if isinstance(rate_limits.get("primary"), dict) else {}
    secondary = rate_limits.get("secondary") if isinstance(rate_limits.get("secondary"), dict) else {}
    return {
        "codex": {
            "primary": usage_slot(primary, 300),
            "secondary": usage_slot(secondary, 10080),
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
    args = parser.parse_args()

    payload = build_payload()
    result = post_usage(args.relay, args.secret, payload)
    codex = result.get("usage", {}).get("codex", {})
    h5 = codex.get("h5", {}).get("pct", payload["codex"]["primary"]["used_percent"])
    d7 = codex.get("d7", {}).get("pct", payload["codex"]["secondary"]["used_percent"])
    print(f"posted codex usage: 5h={h5}% 7d={d7}%")


if __name__ == "__main__":
    main()
