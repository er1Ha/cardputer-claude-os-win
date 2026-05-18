#!/usr/bin/env python3
"""codex-usage — push Codex CLI rate-limit state to the Worker.

Scans ``~/.codex/sessions/`` for the most recent ``token_count`` event,
extracts its ``rate_limits`` block (primary = 5h window, secondary =
7d window in Codex CLI's default ChatGPT-Plus config), and POSTs the
result to ``{worker_base}/usage``. The Cardputer can then read it
back via ``GET /usage`` (or have it injected into BLE heartbeats by a
host-side Buddy app) to render real %s instead of fallbacks.

Designed to be run periodically (launchd / Task Scheduler / cron),
same cadence as ``claude-pull`` — once a minute is plenty since
heartbeat windows are 5h / 7d.

Config: ``~/.config/claude-pager/config.json`` (shared with claude-pull)
  {
    "worker_base": "https://....workers.dev",
    "device_secret": "...",
    "codex_sessions_dir": "~/.codex/sessions"   (optional)
  }

Stdlib-only — no pip dependencies. Python 3.9+.
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

CONFIG_PATH = Path("~/.config/claude-pager/config.json").expanduser()
DEFAULT_SESSIONS_DIR = Path("~/.codex/sessions").expanduser()
TIMEOUT = 15


# ---- config ---------------------------------------------------------

def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        die(
            f"missing {CONFIG_PATH}\n\n"
            "Create it with:\n"
            f"  mkdir -p {CONFIG_PATH.parent}\n"
            f"  cat > {CONFIG_PATH} <<EOF\n"
            "  {\n"
            '    "worker_base": "https://YOUR.workers.dev",\n'
            '    "device_secret": "YOUR_DEVICE_SECRET"\n'
            "  }\n"
            "  EOF\n"
        )
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as e:
        die(f"invalid JSON in {CONFIG_PATH}: {e}")
    for required in ("worker_base", "device_secret"):
        if not cfg.get(required):
            die(f"{CONFIG_PATH}: missing '{required}'")
    cfg["worker_base"] = cfg["worker_base"].rstrip("/")
    cfg["codex_sessions_dir"] = Path(
        cfg.get("codex_sessions_dir") or DEFAULT_SESSIONS_DIR
    ).expanduser()
    return cfg


def die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


# ---- session scan ---------------------------------------------------

def latest_rate_limits(sessions_dir: Path) -> dict[str, Any] | None:
    """Find the newest ``token_count`` event with a ``rate_limits`` block.

    Codex CLI writes one JSONL per session under
    ``sessions/YYYY/MM/DD/rollout-*.jsonl``. We sort by mtime, then walk
    each file from the end and stop on the first matching event.
    """
    if not sessions_dir.is_dir():
        return None
    files = sorted(
        sessions_dir.rglob("rollout-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for fp in files[:8]:  # newest 8 is plenty; old ones don't have fresh limits
        hit = _scan_file(fp)
        if hit:
            return hit
    return None


def _scan_file(fp: Path) -> dict[str, Any] | None:
    try:
        # Read whole file — Codex sessions are bounded (typically <few MB).
        # If a file is huge, take the tail.
        raw = fp.read_bytes()
    except OSError:
        return None
    if len(raw) > 4 * 1024 * 1024:
        raw = raw[-4 * 1024 * 1024 :]
    # Walk lines from the end.
    for line in reversed(raw.splitlines()):
        if b"rate_limits" not in line:
            continue
        try:
            ev = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        # Codex CLI shape (current): event_msg → payload → token_count
        # with payload.rate_limits.{primary,secondary}. Be lenient and
        # accept either nesting in case the schema drifts.
        rl = _extract_rate_limits(ev)
        if rl:
            return rl
    return None


def _extract_rate_limits(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    if "rate_limits" in obj and isinstance(obj["rate_limits"], dict):
        rl = obj["rate_limits"]
        if "primary" in rl or "secondary" in rl:
            return {
                "primary": _bucket(rl.get("primary")),
                "secondary": _bucket(rl.get("secondary")),
            }
    for v in obj.values():
        hit = _extract_rate_limits(v)
        if hit:
            return hit
    return None


def _bucket(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for k in ("used_percent", "window_minutes", "resets_in_seconds", "resets_at"):
        v = raw.get(k)
        if isinstance(v, (int, float)):
            out[k] = v
    return out or None


# ---- HTTP -----------------------------------------------------------

def post_usage(cfg: dict, payload: dict) -> dict | None:
    url = cfg["worker_base"] + "/usage"
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "x-device-secret": cfg["device_secret"],
        "content-type": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    ctx = ssl.create_default_context()
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        die(f"POST /usage → {e.code}: {body[:300]}", code=1)
    except urllib.error.URLError as e:
        die(f"POST /usage transport error: {e.reason}", code=1)
    txt = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(txt) if txt else None
    except json.JSONDecodeError:
        return None


# ---- main -----------------------------------------------------------

def main() -> int:
    cfg = load_config()
    hit = latest_rate_limits(cfg["codex_sessions_dir"])
    if not hit:
        print(
            f"no rate_limits found under {cfg['codex_sessions_dir']}",
            file=sys.stderr,
        )
        return 1
    buckets = {k: v for k, v in hit.items() if v}
    if not buckets:
        print("rate_limits found but all buckets empty", file=sys.stderr)
        return 1
    body = {"codex": buckets}
    resp = post_usage(cfg, body)
    print(json.dumps(resp or body, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
