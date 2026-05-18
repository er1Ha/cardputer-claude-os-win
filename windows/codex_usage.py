#!/usr/bin/env python3
"""Post Codex subscription usage from local Codex logs to the relay.

Reads ~/.codex/sessions/**/rollout-*.jsonl, finds the newest rate_limits
payload, and POSTs a compact usage payload to /usage.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONFIG_PATH = Path("~/.config/claude-pager/config.json").expanduser()
CODEX_SESSIONS = Path("~/.codex/sessions").expanduser()
CODEX_LOG_DB = Path("~/.codex/logs_2.sqlite").expanduser()
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


def parse_event_ts(value: Any, fallback: float) -> datetime:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.fromtimestamp(fallback, tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _codex_command() -> str | None:
    for name in ("codex.cmd", "codex.exe", "codex"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _app_server_request(timeout_seconds: int = 12) -> dict[str, Any] | None:
    exe = _codex_command()
    if not exe:
        return None
    try:
        proc = subprocess.Popen(
            [exe, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None

    try:
        requests = [
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "cardputer-usage", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            },
            {"id": 2, "method": "account/rateLimits/read", "params": None},
        ]
        assert proc.stdin is not None
        assert proc.stdout is not None
        for request in requests:
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == 2 and isinstance(msg.get("result"), dict):
                return msg["result"]
    finally:
        try:
            proc.kill()
        except OSError:
            pass
    return None


def _normalize_app_server_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {
        "limit_id": snapshot.get("limitId"),
        "limit_name": snapshot.get("limitName"),
        "credits": snapshot.get("credits"),
        "plan_type": snapshot.get("planType"),
        "rate_limit_reached_type": snapshot.get("rateLimitReachedType"),
        "source": "codex_app_server",
    }
    for src_key, dst_key in (("primary", "primary"), ("secondary", "secondary")):
        side = snapshot.get(src_key)
        if not isinstance(side, dict):
            continue
        normalized[dst_key] = {
            "used_percent": side.get("usedPercent", 0),
            "window_minutes": side.get("windowDurationMins"),
            "resets_at": side.get("resetsAt"),
            "source": "codex_app_server",
        }
    return normalized


def latest_codex_rate_limits_from_app_server() -> dict[str, Any] | None:
    result = _app_server_request()
    if not result:
        return None
    by_limit = result.get("rateLimitsByLimitId")
    snapshot = None
    if isinstance(by_limit, dict):
        snapshot = by_limit.get("codex")
    if not isinstance(snapshot, dict):
        snapshot = result.get("rateLimits")
    if not isinstance(snapshot, dict):
        return None
    return _normalize_app_server_snapshot(snapshot)


def _extract_json_object(text: str, marker: str) -> dict[str, Any] | None:
    idx = text.find(marker)
    if idx < 0:
        return None
    start = text.find("{", idx)
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for pos in range(start, len(text)):
        ch = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : pos + 1])
                    except json.JSONDecodeError:
                        return None
    return None


def _normalize_rate_limits(raw: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(raw)
    for key in ("primary", "secondary"):
        side = normalized.get(key)
        if not isinstance(side, dict):
            continue
        if "resets_at" not in side and "reset_at" in side:
            side["resets_at"] = side["reset_at"]
        if "resets_at" not in side and "reset_after_seconds" in side:
            side["resets_at"] = datetime.now(timezone.utc).timestamp() + float(side["reset_after_seconds"])
    return normalized


def latest_codex_rate_limits_from_sqlite() -> dict[str, Any] | None:
    if not CODEX_LOG_DB.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{CODEX_LOG_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = conn.execute(
            """
            select feedback_log_body
            from logs
            where feedback_log_body like '%"rate_limits"%'
            order by id desc
            limit 500
            """
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    for (body,) in rows:
        rate_limits = _extract_json_object(body or "", '"rate_limits"')
        if isinstance(rate_limits, dict) and isinstance(rate_limits.get("primary"), dict):
            return _normalize_rate_limits(rate_limits)
    return None


def latest_codex_rate_limits_from_rollouts() -> dict[str, Any] | None:
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
    return _normalize_rate_limits(newest[1]) if newest else None


def latest_codex_rate_limits() -> dict[str, Any] | None:
    return (
        latest_codex_rate_limits_from_app_server()
        or latest_codex_rate_limits_from_sqlite()
        or latest_codex_rate_limits_from_rollouts()
    )


def resets_in_seconds(side: dict[str, Any]) -> int:
    resets_at = side.get("resets_at")
    if not isinstance(resets_at, (int, float)):
        return 0
    delta = int(resets_at - datetime.now(timezone.utc).timestamp())
    return max(0, delta)


def _percent_override(cfg: dict[str, Any], used_key: str, remaining_key: str) -> tuple[float, str] | None:
    if cfg.get(used_key) is not None:
        return float(cfg[used_key]), "used"
    if cfg.get(remaining_key) is not None:
        remaining = float(cfg[remaining_key])
        if cfg.get("codex_display_remaining"):
            return remaining, "remaining"
        return 100.0 - remaining, "used"
    return None


def usage_slot(
    side: dict[str, Any],
    window_minutes: int,
    *,
    override: tuple[float, str] | None = None,
) -> dict[str, Any]:
    mode = "used"
    if override is not None:
        used_percent, mode = override
    else:
        used_percent = float(side.get("used_percent", 0) or 0)
    return {
        "used_percent": max(0.0, min(100.0, used_percent)),
        "window_minutes": window_minutes,
        "resets_in_seconds": resets_in_seconds(side),
        "source": "codex_official_override" if override is not None else side.get("source", "codex_rate_limits"),
        "mode": mode,
    }


def build_payload(cfg: dict[str, Any]) -> dict[str, Any]:
    rate_limits = latest_codex_rate_limits() or {}
    primary = rate_limits.get("primary") if isinstance(rate_limits.get("primary"), dict) else {}
    secondary = rate_limits.get("secondary") if isinstance(rate_limits.get("secondary"), dict) else {}
    use_manual = bool(cfg.get("use_manual_usage_overrides"))
    h5_override = _percent_override(cfg, "codex_5h_used_percent", "codex_5h_remaining_percent") if use_manual else None
    d7_override = _percent_override(cfg, "codex_7d_used_percent", "codex_7d_remaining_percent") if use_manual else None
    return {
        "codex": {
            "primary": usage_slot(primary, 300, override=h5_override),
            "secondary": usage_slot(secondary, 10080, override=d7_override),
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

    payload = build_payload(cfg)
    result = post_usage(args.relay, args.secret, payload)
    codex = result.get("usage", {}).get("codex", {})
    h5 = codex.get("h5", {}).get("pct", payload["codex"]["primary"]["used_percent"])
    d7 = codex.get("d7", {}).get("pct", payload["codex"]["secondary"]["used_percent"])
    print(f"posted codex usage: 5h={h5}% 7d={d7}%")


if __name__ == "__main__":
    main()
