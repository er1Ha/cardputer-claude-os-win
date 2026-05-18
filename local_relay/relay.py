#!/usr/bin/env python3
"""Cardputer local relay — Windows.

Reads ``~/.claude`` and ``~/.codex`` for subscription usage,
exposes chat endpoints that shell out to the local ``claude`` /
``codex`` CLIs (so chat consumes your Pro/Max subscription quota,
not pay-as-you-go API credit).

Claude usage: sum of JSONL token records in ~/.claude/projects.
Codex usage:  reads the latest `rate_limits` event from
              ~/.codex/sessions/**/rollout-*.jsonl (the official
              Codex CLI logs it after every turn, with exact
              reset timestamps).

Stdlib only. Python 3.10+.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = Path(os.path.expanduser("~"))
CLAUDE_DIR = HOME / ".claude"
CLAUDE_PROJECTS = CLAUDE_DIR / "projects"
CODEX_DIR = HOME / ".codex"
CODEX_SESSIONS = CODEX_DIR / "sessions"

DEFAULT_CLAUDE_5H_CAP = 2_000_000
DEFAULT_CLAUDE_7D_CAP = 20_000_000

SECRET: str | None = None
CONFIG: dict = {}
USAGE_OVERRIDES: dict[str, dict] = {}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------- Claude: JSONL token aggregator ---------------------------------

def iter_jsonl(root: Path, days_back: int = 7):
    if not root.exists():
        return
    cutoff_mtime = (now_utc() - timedelta(days=days_back)).timestamp()
    for p in root.rglob("*.jsonl"):
        try:
            if p.stat().st_mtime < cutoff_mtime:
                continue
        except OSError:
            continue
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue


def tokens_from_entry(obj: dict):
    ts = obj.get("timestamp") or obj.get("created_at") or obj.get("time")
    usage = None
    if isinstance(obj.get("message"), dict):
        usage = obj["message"].get("usage")
    if usage is None:
        usage = obj.get("usage")
    if not usage or not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    tokens = (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("output_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
    )
    return dt, tokens


def claude_window():
    h5_cutoff = now_utc() - timedelta(hours=5)
    d7_cutoff = now_utc() - timedelta(days=7)
    h5_used = 0
    d7_used = 0
    earliest_in_5h: datetime | None = None
    for obj in iter_jsonl(CLAUDE_PROJECTS, days_back=7):
        rec = tokens_from_entry(obj)
        if not rec:
            continue
        dt, tok = rec
        if dt >= d7_cutoff:
            d7_used += tok
        if dt >= h5_cutoff:
            h5_used += tok
            if earliest_in_5h is None or dt < earliest_in_5h:
                earliest_in_5h = dt
    if earliest_in_5h:
        reset_at = earliest_in_5h + timedelta(hours=5)
        reset_in_sec = max(0, int((reset_at - now_utc()).total_seconds()))
    else:
        reset_in_sec = 0
    h5_cap = CONFIG["claude_5h_cap"]
    d7_cap = CONFIG["claude_7d_cap"]
    return {
        "h5": {
            "pct":   min(100, int(100 * h5_used / max(1, h5_cap))),
            "used":  h5_used,
            "cap":   h5_cap,
            "reset": fmt_reset_in(reset_in_sec),
        },
        "d7": {
            "pct":   min(100, int(100 * d7_used / max(1, d7_cap))),
            "used":  d7_used,
            "cap":   d7_cap,
            "reset": fmt_d7_reset(now_utc() + timedelta(days=7)),
        },
    }


# ---------- Codex: scan rollout-*.jsonl for the latest rate_limits ---------

def latest_codex_rate_limits() -> dict | None:
    """Find the most recent rollout JSONL, scan it from the bottom for
    a `rate_limits` payload, return that dict, or None."""
    if not CODEX_SESSIONS.exists():
        return None
    rollouts = sorted(
        CODEX_SESSIONS.rglob("rollout-*.jsonl"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    for p in rollouts[:3]:
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for ln in reversed(lines):
            ln = ln.strip()
            if not ln or '"rate_limits"' not in ln:
                continue
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError:
                continue
            payload = obj.get("payload") or {}
            rl = payload.get("rate_limits")
            if isinstance(rl, dict):
                return rl
    return None


def codex_window():
    rl = latest_codex_rate_limits()
    if not rl:
        zero = {"pct": 0, "used": None, "cap": None, "reset": "now", "source": "none"}
        return {"h5": zero, "d7": dict(zero)}

    def slot(side: dict, default_reset_str: str):
        used = float(side.get("used_percent", 0) or 0)
        resets_at = side.get("resets_at")
        if isinstance(resets_at, (int, float)) and resets_at > 0:
            dt = datetime.fromtimestamp(int(resets_at), tz=timezone.utc)
            reset_in_sec = int((dt - now_utc()).total_seconds())
            if reset_in_sec > 24 * 3600:
                reset_str = fmt_d7_reset(dt)
            else:
                reset_str = fmt_reset_in(reset_in_sec)
        else:
            reset_str = default_reset_str
        return {
            "pct":   min(100, int(round(used))),
            "used":  None,
            "cap":   None,
            "reset": reset_str,
            "source": "rollout",
        }

    primary   = rl.get("primary")   if isinstance(rl.get("primary"),   dict) else {}
    secondary = rl.get("secondary") if isinstance(rl.get("secondary"), dict) else {}
    return {
        "h5": slot(primary,   "in ~5 hr"),
        "d7": slot(secondary, fmt_d7_reset(now_utc() + timedelta(days=7))),
    }


# ---------- Format helpers --------------------------------------------------

def fmt_reset_in(sec: int) -> str:
    if sec <= 0:
        return "now"
    h = sec // 3600
    m = (sec % 3600) // 60
    return f"in {h} hr {m} min" if h else f"in {m} min"


def fmt_d7_reset(dt_utc: datetime) -> str:
    local = dt_utc.astimezone()
    fmt = "%a %#I:%M %p" if os.name == "nt" else "%a %-I:%M %p"
    return local.strftime(fmt).upper()


def _posted_slot(slot: dict, window_minutes: int) -> dict:
    used = float(slot.get("used_percent", slot.get("pct", 0)) or 0)
    resets_in = int(slot.get("resets_in_seconds", 0) or 0)
    reset = slot.get("reset") or fmt_reset_in(resets_in)
    return {
        "pct": min(100, int(round(used))),
        "used": slot.get("used"),
        "cap": slot.get("cap"),
        "reset": reset,
        "window_minutes": int(slot.get("window_minutes", window_minutes) or window_minutes),
        "source": slot.get("source", "posted"),
    }


def normalize_posted_usage(side: dict) -> dict | None:
    if not isinstance(side, dict):
        return None
    primary = side.get("primary") or side.get("h5")
    secondary = side.get("secondary") or side.get("d7")
    if not isinstance(primary, dict) and not isinstance(secondary, dict):
        return None
    existing = {}
    if isinstance(primary, dict):
        existing["h5"] = _posted_slot(primary, 300)
    if isinstance(secondary, dict):
        existing["d7"] = _posted_slot(secondary, 10080)
    return existing


def build_usage():
    usage = {"claude": claude_window(), "codex": codex_window()}
    for name, override in USAGE_OVERRIDES.items():
        merged = dict(usage.get(name, {}))
        merged.update(override)
        usage[name] = merged
    return usage


# ---------- CLI runner ------------------------------------------------------

def resolve_cli(name: str) -> str | None:
    for cand in (name, f"{name}.cmd", f"{name}.exe", f"{name}.bat"):
        p = shutil.which(cand)
        if p:
            return p
    return None


def run_cli(name: str, args: list[str], timeout: int = 180) -> str:
    exe = resolve_cli(name)
    if not exe:
        raise RuntimeError(f"`{name}` CLI not found on PATH. Install it first.")
    try:
        proc = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{name} timed out after {timeout}s")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:500]
        raise RuntimeError(f"{name} exited {proc.returncode}: {err}")
    return (proc.stdout or "").strip()


def claude_ask(prompt: str) -> str:
    return run_cli("claude", ["-p", prompt])


def codex_ask(prompt: str) -> str:
    return run_cli("codex", ["exec", "--skip-git-repo-check", prompt])


# ---------- HTTP server -----------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def _cors(self):
        # Permissive CORS so the file:// preview page (and any browser
        # client on the LAN) can hit /usage without a preflight failure.
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
        self.send_header("access-control-allow-headers", "content-type, x-device-secret")
        self.send_header("access-control-max-age", "600")

    def _send(self, code: int, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("content-length", "0")
        self.end_headers()

    def _auth(self) -> bool:
        if not SECRET:
            return True
        return self.headers.get("x-device-secret") == SECRET

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            return self._send(200, {"ok": True})
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/usage":
            try:
                return self._send(200, build_usage())
            except Exception as e:
                return self._send(500, {"error": str(e)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        global USAGE_OVERRIDES
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid json"})
        if self.path == "/usage":
            updated = []
            for name in ("claude", "codex"):
                normalized = normalize_posted_usage(data.get(name))
                if normalized:
                    USAGE_OVERRIDES[name] = normalized
                    updated.append(name)
            if not updated:
                return self._send(400, {"error": "expected claude or codex usage payload"})
            return self._send(200, {"ok": True, "updated": updated, "usage": build_usage()})

        prompt = (data.get("prompt") or data.get("text") or "").strip()
        if not prompt:
            return self._send(400, {"error": "empty prompt"})
        try:
            if self.path in ("/ask", "/ask-text", "/claude"):
                resp = claude_ask(prompt)
            elif self.path == "/codex":
                resp = codex_ask(prompt)
            elif self.path == "/reset":
                return self._send(200, {"ok": True, "cleared": True})
            else:
                return self._send(404, {"error": "not found"})
            return self._send(200, {"transcript": prompt, "response": resp})
        except RuntimeError as e:
            return self._send(502, {"error": str(e)})


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main():
    global SECRET, CONFIG
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--secret", default=os.environ.get("DEVICE_SECRET", ""))
    p.add_argument("--claude-5h-cap", type=int, default=DEFAULT_CLAUDE_5H_CAP)
    p.add_argument("--claude-7d-cap", type=int, default=DEFAULT_CLAUDE_7D_CAP)
    args = p.parse_args()

    SECRET = args.secret or None
    CONFIG = {
        "claude_5h_cap": args.claude_5h_cap,
        "claude_7d_cap": args.claude_7d_cap,
    }

    ip = lan_ip()
    print("=" * 60)
    print("  Cardputer local relay")
    print("=" * 60)
    print(f"  listening on http://{args.host}:{args.port}")
    print(f"  LAN url:     http://{ip}:{args.port}   <- put this on the device")
    print(f"  auth:        {'ON (x-device-secret required)' if SECRET else 'OFF (no secret)'}")
    print(f"  claude dir:  {CLAUDE_PROJECTS} ({'OK' if CLAUDE_PROJECTS.exists() else 'MISSING'})")
    print(f"  codex  dir:  {CODEX_DIR} ({'OK' if CODEX_DIR.exists() else 'MISSING'})")
    print(f"  codex sess:  {CODEX_SESSIONS} ({'OK' if CODEX_SESSIONS.exists() else 'MISSING — codex usage will be 0'})")
    print(f"  claude CLI:  {resolve_cli('claude') or 'NOT FOUND on PATH'}")
    print(f"  codex  CLI:  {resolve_cli('codex')  or 'NOT FOUND on PATH'}")
    print("=" * 60)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n=> shutting down")
        httpd.server_close()


if __name__ == "__main__":
    main()
