"""Quota dashboard server.

Parses local Claude Code and Codex CLI logs, rolls them up into 5-hour
and 7-day token-usage windows, and exposes both a JSON endpoint (for
the M5 to poll over Wi-Fi) and a browser dashboard.

stdlib only — no pip install needed. Tested on CPython 3.10+.
"""

import argparse
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Iterator

CONFIG_PATH = Path(__file__).parent / "config.json"
EXAMPLE_PATH = Path(__file__).parent / "config.example.json"

_FIVE_HOURS_S = 5 * 3600
_SEVEN_DAYS_S = 7 * 86400


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(
            "config.json not found. Copy {} to {} and edit it.".format(
                EXAMPLE_PATH.name, CONFIG_PATH.name
            )
        )
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for key in ("claude_log_dir", "codex_log_dir"):
        cfg[key] = str(Path(os.path.expanduser(cfg[key])))
    return cfg


# ---- parsers ---------------------------------------------------------
#
# Both CLIs write append-only JSONL session files. We keep a per-file
# offset and only re-read new bytes on each refresh — over a day of
# usage these files can grow to tens of MB, and re-reading them all on
# every 10-second tick wastes a lot of disk I/O.


# (path → {"offset": int, "samples": [(ts, tokens), ...]})
_FILE_CACHE: dict[str, dict] = {}
_CACHE_LOCK = threading.Lock()


def _parse_iso(ts: str) -> float | None:
    """Best-effort ISO-8601 → epoch seconds. None on anything we can't read."""
    if not isinstance(ts, str):
        return None
    s = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _sum_usage_fields(u: dict) -> int:
    """Sum the fields that count toward Anthropic / OpenAI rate limits.

    Notably excludes `cache_read_input_tokens` — those are cache hits,
    billed at ~0.1x and not what trips the 5h / weekly subscription
    cap. Including them inflates the count by an order of magnitude
    during active coding sessions where the same context is replayed
    over and over.
    """
    total = 0
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    ):
        v = u.get(key)
        if isinstance(v, (int, float)):
            total += int(v)
    return total


def _extract_usage(event: dict) -> int:
    """Pull total tokens (input + output + cache) out of an event.

    Handles three shapes seen in the wild:
      Claude Code:  event["message"]["usage"][...]
      Codex CLI:    event["token_usage"][...] or event["usage"][...]
    Events with no usage block return 0.
    """
    for key in ("usage", "token_usage"):
        u = event.get(key)
        if isinstance(u, dict):
            return _sum_usage_fields(u)
    msg = event.get("message")
    if isinstance(msg, dict):
        u = msg.get("usage")
        if isinstance(u, dict):
            return _sum_usage_fields(u)
    return 0


def _extract_ts(event: dict) -> float | None:
    for key in ("timestamp", "ts", "time", "created_at"):
        t = _parse_iso(event.get(key, ""))
        if t is not None:
            return t
    return None


def _read_new_samples(path: Path) -> None:
    """Tail one file: read from the last known offset, append to cache."""
    key = str(path)
    try:
        size = path.stat().st_size
    except OSError:
        _FILE_CACHE.pop(key, None)
        return

    entry = _FILE_CACHE.get(key)
    if entry is None:
        entry = {"offset": 0, "samples": []}
        _FILE_CACHE[key] = entry

    # File truncated or rotated — reset and re-read from scratch.
    if size < entry["offset"]:
        entry["offset"] = 0
        entry["samples"] = []

    if size == entry["offset"]:
        return

    try:
        with path.open("rb") as f:
            f.seek(entry["offset"])
            chunk = f.read()
            entry["offset"] = f.tell()
    except OSError:
        return

    for raw in chunk.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        ts = _extract_ts(ev)
        if ts is None:
            continue
        tokens = _extract_usage(ev)
        if tokens > 0:
            entry["samples"].append((ts, tokens))


def _walk_jsonl(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    yield from root.rglob("*.jsonl")


def scan_logs(root: Path) -> list[tuple[float, int]]:
    """Walk a log directory, tail any growing files, return all known samples."""
    samples: list[tuple[float, int]] = []
    seen: set[str] = set()
    for path in _walk_jsonl(root):
        _read_new_samples(path)
        seen.add(str(path))
        samples.extend(_FILE_CACHE[str(path)]["samples"])
    # Drop cache entries for files that have disappeared (rotated logs).
    for key in list(_FILE_CACHE.keys()):
        if key.startswith(str(root)) and key not in seen:
            _FILE_CACHE.pop(key, None)
    return samples


# ---- Codex live rate limits via `codex app-server` -------------------
#
# `codex app-server` is the local JSON-RPC server Codex Desktop talks
# to. Sending `account/rateLimits/read` returns the same numbers
# /status renders — sourced directly from the OpenAI service, not from
# whatever the latest rollout file happens to hold. Rollout files only
# update after a turn completes, so during an active session they lag
# the real percentage by a measurable amount.
#
# Spawning `codex.exe` costs ~1s, so cache for 30s. Falls through to
# the rollout scanner if codex isn't on PATH or the subprocess hangs.


def _codex_executable() -> str | None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        desktop_exe = Path(local_app_data) / "OpenAI" / "Codex" / "bin" / "codex.exe"
        if desktop_exe.exists():
            return str(desktop_exe)
    import shutil
    for name in ("codex.cmd", "codex.exe", "codex"):
        path = shutil.which(name)
        if path:
            return path
    return None


_APP_SERVER_CACHE: dict = {"ts": 0.0, "data": None}
_APP_SERVER_TTL_S = 30.0


def _normalize_app_server_snapshot(snapshot: dict) -> dict:
    """Convert the camelCase JSON-RPC shape to the snake_case shape the
    rest of the server uses for Codex windows."""
    out: dict = {}
    for src_key in ("primary", "secondary"):
        side = snapshot.get(src_key)
        if not isinstance(side, dict):
            continue
        used = side.get("usedPercent", 0) or 0
        out[src_key] = {
            "used_percent": float(used),
            "window_minutes": side.get("windowDurationMins"),
            "resets_at": side.get("resetsAt"),
        }
    plan = snapshot.get("planType")
    if isinstance(plan, str):
        out["plan_type"] = plan
    return out


def fetch_codex_app_server_rate_limits() -> dict | None:
    """Drive `codex app-server` via stdin JSON-RPC. Cached for 30s.

    Returns the same `{primary, secondary, plan_type}` shape the
    rollout-file scanner returns, so callers downstream are agnostic
    about which path we used.
    """
    now = time.time()
    if (
        _APP_SERVER_CACHE["data"] is not None
        and now - _APP_SERVER_CACHE["ts"] < _APP_SERVER_TTL_S
    ):
        return _APP_SERVER_CACHE["data"]

    exe = _codex_executable()
    if not exe:
        return None

    # CREATE_NO_WINDOW keeps codex.exe from flashing a console on
    # Windows; on POSIX the flag is 0.
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            [exe, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
    except OSError as exc:
        sys.stderr.write("codex app-server spawn failed: {}\n".format(exc))
        return None

    try:
        requests = [
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "cardputer-quota", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            },
            {"id": 2, "method": "account/rateLimits/read", "params": None},
        ]
        assert proc.stdin is not None and proc.stdout is not None
        for r in requests:
            proc.stdin.write(json.dumps(r) + "\n")
        proc.stdin.flush()

        deadline = time.monotonic() + 12.0
        snapshot = None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == 2 and isinstance(msg.get("result"), dict):
                snapshot = msg["result"]
                break
    finally:
        try:
            proc.kill()
        except OSError:
            pass

    if not isinstance(snapshot, dict):
        return None

    by_limit = snapshot.get("rateLimitsByLimitId")
    inner = None
    if isinstance(by_limit, dict):
        inner = by_limit.get("codex")
    if not isinstance(inner, dict):
        inner = snapshot.get("rateLimits")
    if not isinstance(inner, dict):
        return None

    normalized = _normalize_app_server_snapshot(inner)
    if not normalized:
        return None
    _APP_SERVER_CACHE["ts"] = now
    _APP_SERVER_CACHE["data"] = normalized
    return normalized


# ---- Codex official rate_limits --------------------------------------
#
# Codex CLI writes the server-reported subscription rate limits straight
# into each rollout file as `event_msg` events with payload.type =
# "token_count". The shape is:
#
#     {"payload": {
#         "type": "token_count",
#         "info": {"total_token_usage": {...}, "last_token_usage": {...},
#                  "model_context_window": N},
#         "rate_limits": {
#             "primary":   {"used_percent": 1.0,  "window_minutes": 300,
#                           "resets_at": <epoch_seconds>},
#             "secondary": {"used_percent": 38.0, "window_minutes": 10080,
#                           "resets_at": <epoch_seconds>},
#             "plan_type": "plus", ...
#         },
#     }}
#
# This is the authoritative source — no need to sum tokens or guess a
# cap. We scan the most recent rollout files newest-first and take the
# freshest rate_limits block we find.


def _scan_file_for_rate_limits(path: Path) -> tuple[dict | None, float | None]:
    """Return the last rate_limits block in `path` plus its event timestamp.

    Reads the file once into memory and walks lines in reverse so we
    can stop at the first match — rollout files are append-only and
    the most recent turn is at the end.
    """
    try:
        with path.open("rb") as f:
            data = f.read()
    except OSError:
        return None, None
    for raw in reversed(data.splitlines()):
        if b"token_count" not in raw or b"rate_limits" not in raw:
            continue
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        payload = ev.get("payload") or {}
        if payload.get("type") != "token_count":
            continue
        # rate_limits sits directly under `payload`, not under
        # `payload.info` (info holds total/last token counts and the
        # model_context_window — a separate concern).
        rl = payload.get("rate_limits")
        if isinstance(rl, dict):
            return rl, _parse_iso(ev.get("timestamp", ""))
    return None, None


def find_codex_rate_limits(root: Path) -> dict | None:
    """Newest rate_limits snapshot across the last few rollout files.

    Caps the scan at 5 files so a long session history doesn't slow
    down each refresh tick; the rate_limits block we want will always
    be in a very recent file (it's emitted on every Codex turn).
    """
    if not root.exists():
        return None
    files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    best: dict | None = None
    best_ts: float = -1.0
    for path in files[:5]:
        rl, ts = _scan_file_for_rate_limits(path)
        if rl is None:
            continue
        score = ts if ts is not None else path.stat().st_mtime
        if score > best_ts:
            best_ts = score
            best = rl
    return best


# ---- Claude statusLine capture ---------------------------------------
#
# claude_statusline_capture.py is wired in via settings.json's
# `statusLine.command`. Claude Code passes one JSON object on stdin to
# that command on every status-line render; the capture script saves
# the official `rate_limits` block to ~/.claude/usage-status.json with
# shape:
#
#   {"captured_at": "<iso>",
#    "primary":   {"used_percent": N, "window_minutes": 300,
#                  "resets_in_seconds": N},
#    "secondary": {"used_percent": N, "window_minutes": 10080,
#                  "resets_in_seconds": N}}
#
# Zero API calls — Claude Code itself gives us the freshest possible
# numbers as a side effect of rendering its status bar.


def find_claude_statusline_usage(claude_home: Path) -> dict | None:
    path = claude_home / "usage-status.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _claude_statusline_window(side, captured_at_s: float | None, now: float) -> dict:
    if not isinstance(side, dict):
        return {"tokens": 0, "cap": 100, "pct": 0, "reset_s": 0, "source": "missing"}
    used = side.get("used_percent")
    pct = int(round(used)) if isinstance(used, (int, float)) else 0
    pct = max(0, min(100, pct))
    raw_reset = side.get("resets_in_seconds")
    if not isinstance(raw_reset, (int, float)):
        return {"tokens": pct, "cap": 100, "pct": pct, "reset_s": 0, "source": "claude_statusline"}
    # The captured number is delta-from-captured-at, so subtract the
    # elapsed seconds since the file was written.
    elapsed = (now - captured_at_s) if captured_at_s else 0.0
    reset_s = int(max(0, raw_reset - elapsed))
    if reset_s == 0 and raw_reset > 0:
        return {"tokens": 0, "cap": 100, "pct": 0, "reset_s": 0, "source": "rolled_over"}
    return {"tokens": pct, "cap": 100, "pct": pct, "reset_s": reset_s, "source": "claude_statusline"}


# ---- Claude OAuth official usage -------------------------------------
#
# api.anthropic.com/api/oauth/usage returns the same numbers the
# official HUD plugins display — five_hour and seven_day blocks with
# `utilization` (percent) and `resets_at` (ISO). This is the freshest
# possible source because we ask Anthropic directly each refresh tick;
# the claude-hud cache file only updates when Claude Code renders its
# status line, so it can sit stale for days if you haven't launched
# Claude Code recently.
#
# Auth: bearer token from ~/.claude/.credentials.json
# (claudeAiOauth.accessToken), the same token Claude Code itself uses.
# The User-Agent must look like `claude-code/<version>` — the endpoint
# 429s other UAs.


def _claude_oauth_token(claude_home: Path) -> str | None:
    env = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env:
        return env
    path = claude_home / ".credentials.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            creds = json.load(f)
    except (OSError, ValueError):
        return None
    inner = creds.get("claudeAiOauth")
    if isinstance(inner, dict):
        tok = inner.get("accessToken")
        if isinstance(tok, str) and tok:
            return tok
    return None


def _claude_code_version() -> str:
    """Probe `claude --version` so the User-Agent looks like a real CLI."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=3,
            creationflags=creationflags,
        ).stdout.strip()
        match = re.match(r"^(\d+\.\d+\.\d+)", out)
        if match:
            return match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return "2.1.0"


_OAUTH_USAGE_CACHE: dict = {"ts": 0.0, "data": None}
_OAUTH_USAGE_TTL_S = 30.0


def fetch_claude_oauth_usage(claude_home: Path) -> dict | None:
    """GET api.anthropic.com/api/oauth/usage, cached for OAUTH_TTL seconds.

    Returns the parsed JSON ({"five_hour": {...}, "seven_day": {...}, ...})
    or None if there's no token, the request failed, or the body wasn't
    JSON. Caller handles `None` by falling back to the claude-hud cache
    or token-summing path.
    """
    now = time.time()
    if (
        _OAUTH_USAGE_CACHE["data"] is not None
        and now - _OAUTH_USAGE_CACHE["ts"] < _OAUTH_USAGE_TTL_S
    ):
        return _OAUTH_USAGE_CACHE["data"]
    token = _claude_oauth_token(claude_home)
    if not token:
        return None
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "accept": "application/json",
            "authorization": "Bearer " + token,
            "anthropic-beta": "oauth-2025-04-20",
            "user-agent": "claude-code/" + _claude_code_version(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        # 429 is the common case here — Anthropic rate-limits the
        # endpoint pretty aggressively. Don't drop our last-good data
        # just because we got rate-limited for a tick; keep returning
        # the previous reading until either the cooldown lifts or a
        # newer source (statusLine capture, claude-hud) appears.
        body = b""
        try:
            body = exc.read()
        except Exception:
            pass
        sys.stderr.write(
            "claude oauth usage fetch failed: HTTP {}: {}\n".format(
                exc.code, body[:200].decode("utf-8", errors="replace")
            )
        )
        if exc.code == 429 and _OAUTH_USAGE_CACHE["data"] is not None:
            # Hold the previous response for 5 minutes before retrying.
            _OAUTH_USAGE_CACHE["ts"] = now - _OAUTH_USAGE_TTL_S + 300
            return _OAUTH_USAGE_CACHE["data"]
        return None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        sys.stderr.write("claude oauth usage fetch failed: {}\n".format(exc))
        return None
    _OAUTH_USAGE_CACHE["ts"] = now
    _OAUTH_USAGE_CACHE["data"] = data
    return data


def _claude_oauth_window(side, now: float) -> dict:
    if not isinstance(side, dict):
        return {"tokens": 0, "cap": 100, "pct": 0, "reset_s": 0, "source": "missing"}
    util = side.get("utilization")
    pct = int(round(util)) if isinstance(util, (int, float)) else 0
    pct = max(0, min(100, pct))
    resets_at_iso = side.get("resets_at")
    resets_at = _parse_iso(resets_at_iso) if isinstance(resets_at_iso, str) else None
    if resets_at is not None and resets_at < now:
        return {"tokens": 0, "cap": 100, "pct": 0, "reset_s": 0, "source": "rolled_over"}
    reset_s = max(0, int(resets_at - now)) if resets_at is not None else 0
    return {"tokens": pct, "cap": 100, "pct": pct, "reset_s": reset_s, "source": "claude_oauth"}


# ---- Claude HUD official usage ---------------------------------------
#
# The community `claude-hud` plugin (jarrodwatts/claude-hud) caches the
# server's reported subscription usage at
#   ~/.claude/plugins/claude-hud/.usage-cache.json
# with shape:
#   {"data": {"planName": "Pro",
#             "fiveHour": <percent>, "sevenDay": <percent>,
#             "fiveHourResetAt": "<iso>", "sevenDayResetAt": "<iso>"},
#    "timestamp": <ms_epoch>,
#    "lastGoodData": {...}}
# The cache is refreshed whenever Claude Code renders its status line.
# If `data` is missing (transient fetch failure), `lastGoodData` is the
# previous known-good snapshot.


def find_claude_hud_usage(claude_home: Path) -> dict | None:
    path = claude_home / "plugins" / "claude-hud" / ".usage-cache.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, ValueError):
        return None
    data = blob.get("data") or blob.get("lastGoodData")
    if not isinstance(data, dict):
        return None
    return data


def _claude_hud_window(percent, resets_at_iso, now: float) -> dict:
    if not isinstance(percent, (int, float)):
        return {"tokens": 0, "cap": 100, "pct": 0, "reset_s": 0, "source": "missing"}
    pct = max(0, min(100, int(round(percent))))
    resets_at = _parse_iso(resets_at_iso) if isinstance(resets_at_iso, str) else None
    if resets_at is not None and resets_at < now:
        # Window rolled over since the cache was written; until claude-hud
        # refreshes the file, assume 0%.
        return {"tokens": 0, "cap": 100, "pct": 0, "reset_s": 0, "source": "rolled_over"}
    reset_s = 0
    if resets_at is not None:
        reset_s = max(0, int(resets_at - now))
    return {"tokens": pct, "cap": 100, "pct": pct, "reset_s": reset_s, "source": "claude_hud"}


def _codex_window(window: dict | None, now: float) -> dict:
    """Convert one Codex rate_limits sub-block into the snapshot shape.

    If the reported reset moment has already passed, the window has
    rolled over since the snapshot was written — assume 0% used until
    a fresher snapshot lands.
    """
    if not isinstance(window, dict):
        return {"tokens": 0, "cap": 100, "pct": 0, "reset_s": 0, "source": "missing"}
    used = window.get("used_percent")
    pct = int(used) if isinstance(used, (int, float)) else 0
    pct = max(0, min(100, pct))
    resets_at = window.get("resets_at")
    if isinstance(resets_at, (int, float)) and resets_at < now:
        return {"tokens": 0, "cap": 100, "pct": 0, "reset_s": 0, "source": "rolled_over"}
    reset_s = 0
    if isinstance(resets_at, (int, float)):
        reset_s = max(0, int(resets_at - now))
    return {"tokens": pct, "cap": 100, "pct": pct, "reset_s": reset_s, "source": "rate_limits"}


# ---- rollups ---------------------------------------------------------


def _sum_window(samples: list[tuple[float, int]], window_s: int, now: float) -> tuple[int, float | None]:
    cutoff = now - window_s
    total = 0
    oldest: float | None = None
    for ts, tok in samples:
        if ts < cutoff:
            continue
        total += tok
        if oldest is None or ts < oldest:
            oldest = ts
    return total, oldest


def _pct(value: int, cap: int) -> int:
    if cap <= 0:
        return 0
    return max(0, min(100, int(value * 100 / cap)))


def _reset_seconds(oldest_ts: float | None, window_s: int, now: float) -> int:
    if oldest_ts is None:
        return 0
    return max(0, int(oldest_ts + window_s - now))


def _window(samples, window_s, cap, now):
    tok, oldest = _sum_window(samples, window_s, now)
    return {
        "tokens": tok,
        "cap": cap,
        "pct": _pct(tok, cap),
        "reset_s": _reset_seconds(oldest, window_s, now),
    }


def build_snapshot(cfg: dict) -> dict:
    now = time.time()
    codex_root = Path(cfg["codex_log_dir"])
    claude_log_root = Path(cfg["claude_log_dir"])
    # ~/.claude/projects/... → ~/.claude (where claude-hud caches usage).
    claude_home = claude_log_root.parent

    # Codex preference order:
    #   1. `codex app-server` JSON-RPC — live OpenAI numbers, same as
    #      /status. Costs a subprocess spawn (~1s), so cached for 30s.
    #   2. Latest rate_limits block from rollout files — accurate at
    #      the moment of the last completed turn, but lags during an
    #      active session.
    #   3. Token-sum estimate.
    codex_rl = fetch_codex_app_server_rate_limits()
    codex_source_tag = "codex_app_server"
    if codex_rl is None:
        codex_rl = find_codex_rate_limits(codex_root)
        codex_source_tag = "rate_limits"
    if codex_rl is not None:
        codex_view = {
            "5h": _codex_window(codex_rl.get("primary"), now),
            "7d": _codex_window(codex_rl.get("secondary"), now),
            "plan": codex_rl.get("plan_type") or "",
        }
        for win in ("5h", "7d"):
            if codex_view[win].get("source") == "rate_limits":
                codex_view[win]["source"] = codex_source_tag
    else:
        with _CACHE_LOCK:
            codex_samples = scan_logs(codex_root)
        codex_view = {
            "5h": _window(codex_samples, _FIVE_HOURS_S, cfg["codex_5h_cap"], now),
            "7d": _window(codex_samples, _SEVEN_DAYS_S, cfg["codex_7d_cap"], now),
            "plan": "",
        }
        codex_view["5h"]["source"] = "estimated"
        codex_view["7d"]["source"] = "estimated"

    # Four-tier preference for Claude, in freshness/reliability order:
    #   1. ~/.claude/usage-status.json — written by the statusLine
    #      capture hook every time Claude Code renders its status bar.
    #      Zero API calls, refreshed naturally while you work.
    #   2. api.anthropic.com/api/oauth/usage — direct call. Same numbers
    #      the HUD plugins themselves use, but Anthropic rate-limits
    #      this endpoint, so we cache aggressively.
    #   3. ~/.claude/plugins/claude-hud/.usage-cache.json — same shape
    #      as the statusLine file but refreshed less reliably.
    #   4. Sum tokens from ~/.claude/projects/*.jsonl as a last resort.
    statusline = find_claude_statusline_usage(claude_home)
    hud_for_plan = find_claude_hud_usage(claude_home) or {}
    plan_fallback = hud_for_plan.get("planName") or ""
    captured_at_s = None
    if isinstance(statusline, dict):
        captured_at_s = _parse_iso(statusline.get("captured_at", ""))

    if statusline is not None and (statusline.get("primary") or statusline.get("secondary")):
        claude_view = {
            "5h": _claude_statusline_window(statusline.get("primary"), captured_at_s, now),
            "7d": _claude_statusline_window(statusline.get("secondary"), captured_at_s, now),
            "plan": plan_fallback,
        }
    elif (oauth := fetch_claude_oauth_usage(claude_home)) is not None:
        claude_view = {
            "5h": _claude_oauth_window(oauth.get("five_hour"), now),
            "7d": _claude_oauth_window(oauth.get("seven_day"), now),
            "plan": plan_fallback,
        }
    elif (hud := find_claude_hud_usage(claude_home)) is not None:
        claude_view = {
            "5h": _claude_hud_window(hud.get("fiveHour"), hud.get("fiveHourResetAt"), now),
            "7d": _claude_hud_window(hud.get("sevenDay"), hud.get("sevenDayResetAt"), now),
            "plan": hud.get("planName") or "",
        }
    else:
        with _CACHE_LOCK:
            claude_samples = scan_logs(claude_log_root)
        claude_view = {
            "5h": _window(claude_samples, _FIVE_HOURS_S, cfg["claude_5h_cap"], now),
            "7d": _window(claude_samples, _SEVEN_DAYS_S, cfg["claude_7d_cap"], now),
            "plan": "",
        }
        claude_view["5h"]["source"] = "estimated"
        claude_view["7d"]["source"] = "estimated"

    return {
        "generated_at": int(now),
        "claude": claude_view,
        "codex": codex_view,
    }


def heartbeat_payload(snap: dict) -> dict:
    """Flatten a snapshot into the field shape the M5 UI expects.

    Mirrors `buddy_ui_cp._usage_pct` / `_usage_reset` lookup keys so the
    existing renderer consumes it without changes.
    """
    c = snap["claude"]
    x = snap["codex"]
    return {
        "usage_view": "claude",
        "claude_5h_pct": c["5h"]["pct"],
        "claude_5h_reset_s": c["5h"]["reset_s"],
        "claude_7d_pct": c["7d"]["pct"],
        "claude_7d_reset_s": c["7d"]["reset_s"],
        "codex_5h_pct": x["5h"]["pct"],
        "codex_5h_reset_s": x["5h"]["reset_s"],
        "codex_7d_pct": x["7d"]["pct"],
        "codex_7d_reset_s": x["7d"]["reset_s"],
    }


# ---- HTTP server -----------------------------------------------------


_LIVE: dict = {"snap": None, "hb": None, "ts": 0.0}
_LIVE_LOCK = threading.Lock()


def _log_summary(snap: dict) -> None:
    c5 = snap["claude"]["5h"]
    c7 = snap["claude"]["7d"]
    x5 = snap["codex"]["5h"]
    x7 = snap["codex"]["7d"]
    sys.stderr.write(
        "refresh  claude 5h={:>3}% ({}/{:,})  7d={:>3}%   "
        "codex 5h={:>3}% ({}/{:,})  7d={:>3}%\n".format(
            c5["pct"], c5["tokens"], c5["cap"], c7["pct"],
            x5["pct"], x5["tokens"], x5["cap"], x7["pct"],
        )
    )


def refresher(cfg: dict, stop: threading.Event):
    while not stop.is_set():
        try:
            snap = build_snapshot(cfg)
            hb = heartbeat_payload(snap)
            with _LIVE_LOCK:
                _LIVE["snap"] = snap
                _LIVE["hb"] = hb
                _LIVE["ts"] = time.time()
            _log_summary(snap)
        except Exception as exc:
            print("refresh error:", exc, file=sys.stderr)
        stop.wait(cfg.get("refresh_seconds", 10))


_WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


def _load_dashboard() -> bytes:
    """Read the dashboard HTML fresh on each request so the UI can be
    edited and reloaded without restarting the server."""
    path = _WEB_ROOT / "index.html"
    try:
        return path.read_bytes()
    except OSError as exc:
        msg = "dashboard html missing at {}: {}".format(path, exc)
        return msg.encode("utf-8")




class Handler(http.server.BaseHTTPRequestHandler):
    # Quiet down the default per-request access log; the refresher's
    # one-line summary tells you the server is alive.
    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, status: int = 200):
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index", "/index.html"):
            self._send(200, _load_dashboard(), "text/html; charset=utf-8")
            return
        if path == "/api/quota":
            with _LIVE_LOCK:
                snap = _LIVE["snap"]
            if snap is None:
                self._json({"error": "warming up"}, 503)
                return
            self._json(snap)
            return
        if path == "/api/heartbeat":
            with _LIVE_LOCK:
                hb = _LIVE["hb"]
            if hb is None:
                self._json({"error": "warming up"}, 503)
                return
            self._json(hb)
            return
        if path == "/api/healthz":
            self._json({"ok": True, "ts": _LIVE["ts"]})
            return
        self._send(404, b"not found", "text/plain")


class ReusableTCPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def cmd_once(cfg: dict) -> None:
    """One-shot: print a snapshot and exit. Useful for sanity-checking
    parsing before leaving the server running."""
    snap = build_snapshot(cfg)
    print(json.dumps(snap, indent=2))


def cmd_serve(cfg: dict) -> None:
    stop = threading.Event()
    t = threading.Thread(target=refresher, args=(cfg, stop), daemon=True)
    t.start()

    addr = (cfg["host"], int(cfg["port"]))
    sys.stderr.write("quota-display listening on http://{}:{}/\n".format(*addr))
    sys.stderr.write("  dashboard:  http://{}:{}/\n".format(*addr))
    sys.stderr.write("  M5 polls:   http://{}:{}/api/heartbeat\n".format(*addr))
    with ReusableTCPServer(addr, Handler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            sys.stderr.write("\nshutting down\n")
        finally:
            stop.set()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
    args = ap.parse_args()
    cfg = load_config()
    if args.once:
        cmd_once(cfg)
    else:
        cmd_serve(cfg)


if __name__ == "__main__":
    main()
