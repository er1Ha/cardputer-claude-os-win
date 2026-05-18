#!/usr/bin/env python3
"""Estimate Claude usage from local Claude Code JSONL logs and POST it.

Scans ~/.claude/projects/**/*.jsonl, sums message.usage token fields in
the last 5 hours and 7 days, compares them with configurable caps, and
POSTs the result to /usage.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import sqlite3
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CONFIG_PATH = Path("~/.config/claude-pager/config.json").expanduser()
CLAUDE_PROJECTS = Path("~/.claude/projects").expanduser()
CLAUDE_STATUS_PATH = Path("~/.claude/usage-status.json").expanduser()
CLAUDE_CREDENTIALS_PATH = Path("~/.claude/.credentials.json").expanduser()
CLAUDE_DESKTOP_DIR = Path(os.environ.get("APPDATA", "")) / "Claude"
CLAUDE_DESKTOP_LOCAL_STATE = CLAUDE_DESKTOP_DIR / "Local State"
CLAUDE_DESKTOP_COOKIES = CLAUDE_DESKTOP_DIR / "Network" / "Cookies"
DEFAULT_CLAUDE_5H_TOKEN_CAP = 2_000_000
DEFAULT_CLAUDE_7D_TOKEN_CAP = 205_000
TIMEOUT = 20


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


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


def _dpapi_decrypt(blob: bytes) -> bytes:
    inbuf = ctypes.create_string_buffer(blob, len(blob))
    inblob = _DataBlob(len(blob), ctypes.cast(inbuf, ctypes.POINTER(ctypes.c_byte)))
    outblob = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(inblob), None, None, None, None, 0, ctypes.byref(outblob)
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(outblob.pbData, outblob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(outblob.pbData)


def _desktop_master_key() -> bytes | None:
    if not CLAUDE_DESKTOP_LOCAL_STATE.exists():
        return None
    try:
        state = json.loads(CLAUDE_DESKTOP_LOCAL_STATE.read_text(encoding="utf-8-sig"))
        encrypted = base64.b64decode(state["os_crypt"]["encrypted_key"])
        if encrypted.startswith(b"DPAPI"):
            encrypted = encrypted[5:]
        return _dpapi_decrypt(encrypted)
    except Exception:
        return None


def _decrypt_chrome_value(encrypted_value: bytes, key: bytes) -> str:
    if encrypted_value.startswith((b"v10", b"v11")):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as e:
            raise RuntimeError("cryptography package is required for Claude Desktop cookie decryption") from e
        plain = AESGCM(key).decrypt(encrypted_value[3:15], encrypted_value[15:], None)
        # Chromium 130+ prefixes cookie plaintext with SHA256(host_key).
        if len(plain) > 32:
            try:
                return plain[32:].decode("utf-8")
            except UnicodeDecodeError:
                pass
        return plain.decode("utf-8")
    return _dpapi_decrypt(encrypted_value).decode("utf-8")


def _claude_desktop_cookies() -> str | None:
    if not CLAUDE_DESKTOP_COOKIES.exists():
        return None
    key = _desktop_master_key()
    if not key:
        return None
    wanted = {
        "__cf_bm",
        "__ssid",
        "anthropic-device-id",
        "cf_clearance",
        "lastActiveOrg",
        "sessionKey",
        "sessionKeyLC",
    }
    try:
        conn = sqlite3.connect(CLAUDE_DESKTOP_COOKIES.as_uri() + "?mode=ro", uri=True, timeout=2)
        rows = conn.execute(
            """
            select name, value, encrypted_value
            from cookies
            where host_key like '%claude.ai%'
            """
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    parts: list[str] = []
    for name, value, encrypted_value in rows:
        if name not in wanted:
            continue
        try:
            cookie_value = value or _decrypt_chrome_value(bytes(encrypted_value), key)
            cookie_value.encode("latin-1")
        except Exception:
            continue
        if cookie_value:
            parts.append(f"{name}={cookie_value}")
    return "; ".join(parts) if parts else None


def _organization_uuid() -> str | None:
    """Try the Claude Code OAuth credential file. Usually does NOT have
    organizationUuid — that's a Claude Desktop / web concept — so
    callers should fall back to ``_organization_uuid_from_api``."""
    if not CLAUDE_CREDENTIALS_PATH.exists():
        return None
    try:
        creds = json.loads(CLAUDE_CREDENTIALS_PATH.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None
    for key in ("organizationUuid", "organization_uuid", "orgUuid"):
        val = creds.get(key)
        if isinstance(val, str) and val:
            return val
    inner = creds.get("claudeAiOauth")
    if isinstance(inner, dict):
        for key in ("organizationUuid", "organization_uuid"):
            val = inner.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def _claude_request(path: str, cookies: str) -> Any:
    url = "https://claude.ai" + path
    headers = {
        "accept": "application/json,text/plain,*/*",
        "cookie": cookies,
        "origin": "https://claude.ai",
        "referer": "https://claude.ai/settings/usage",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Claude/1.7196.0 Chrome/142.0.7444.200 Electron/41.5.0 Safari/537.36"
        ),
        "x-requested-with": "XMLHttpRequest",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _organization_uuid_from_api(cookies: str) -> str | None:
    try:
        data = _claude_request("/api/organizations", cookies)
    except Exception:
        return None
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            for key in ("uuid", "id", "organizationUuid"):
                val = first.get(key)
                if isinstance(val, str) and val:
                    return val
    return None


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


def reset_seconds_from_iso(value: Any, now: datetime) -> int:
    dt = parse_ts(value)
    if dt is None:
        return 0
    return max(0, int((dt - now).total_seconds()))


def reset_label(seconds: int) -> str:
    if seconds <= 0:
        return "now"
    minutes = (seconds + 59) // 60
    hours, mins = divmod(minutes, 60)
    if hours:
        return f"in {hours} hr {mins} min"
    return f"in {mins} min"


def pct(used: int, cap: int) -> float:
    return round((used / max(1, cap)) * 100, 2)


def _override_percent(value: Any) -> float | None:
    if value is None:
        return None
    return max(0.0, min(100.0, float(value)))


def _status_slot(side: dict[str, Any], window_minutes: int) -> dict[str, Any]:
    resets_in = int(side.get("resets_in_seconds", 0) or 0)
    used = float(side.get("used_percent", side.get("used_percentage", 0)) or 0)
    return {
        "used_percent": max(0.0, min(100.0, used)),
        "used": side.get("used"),
        "cap": side.get("cap"),
        "window_minutes": int(side.get("window_minutes", window_minutes) or window_minutes),
        "resets_in_seconds": max(0, resets_in),
        "source": "claude_statusline_rate_limits",
    }


def payload_from_desktop_usage_api(
    *,
    session_key: str | None = None,
    organization_uuid: str | None = None,
    debug: bool = False,
) -> dict[str, Any] | None:
    """Hit https://claude.ai/api/organizations/{org}/usage as the web app does.

    Cookie source priority:
      1. Explicit ``session_key`` — wrapped as ``sessionKey=<value>``.
         Most reliable; copy from browser DevTools → Application →
         Cookies → claude.ai → sessionKey.
      2. Claude Desktop's encrypted cookie SQLite (DPAPI + AES-GCM via
         the ``cryptography`` package). Fragile.

    Org UUID source priority:
      1. Explicit ``organization_uuid``.
      2. ~/.claude/.credentials.json (usually not present there).
      3. GET /api/organizations using the cookies.
    """
    def _log(msg: str) -> None:
        if debug:
            print(f"[claude_usage] {msg}", file=sys.stderr)

    if session_key:
        cookies = f"sessionKey={session_key}"
        _log("using session_key from arg/config")
    else:
        cookies = _claude_desktop_cookies()
        if cookies:
            _log("using cookies extracted from Claude Desktop")
        else:
            _log(
                "no session_key provided and Claude Desktop cookies unreadable "
                "(install + login to Claude Desktop, or pass --session-key, or "
                'set "claude_session_key" in config.json)'
            )
            return None

    org = organization_uuid or _organization_uuid()
    if not org:
        org = _organization_uuid_from_api(cookies)
        if org:
            _log(f"resolved org uuid via /api/organizations: {org}")
        else:
            _log("could not resolve organization uuid (sessionKey may be invalid)")
            return None
    else:
        _log(f"using org uuid {org}")

    url = f"https://claude.ai/api/organizations/{org}/usage"
    try:
        data = _claude_request(f"/api/organizations/{org}/usage", cookies)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        _log(f"GET {url} → HTTP {e.code}: {body[:200]}")
        return None
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        _log(f"GET {url} failed: {e}")
        return None
    if debug:
        keys = sorted(data.keys()) if isinstance(data, dict) else type(data).__name__
        _log(f"usage payload keys: {keys}")
    now = datetime.now(timezone.utc)

    def slot(name: str, window_minutes: int) -> dict[str, Any] | None:
        side = data.get(name)
        if not isinstance(side, dict):
            return None
        reset_in = reset_seconds_from_iso(side.get("resets_at"), now)
        used = float(side.get("utilization", 0) or 0)
        return {
            "used_percent": max(0.0, min(100.0, used)),
            "window_minutes": window_minutes,
            "resets_in_seconds": reset_in,
            "reset": reset_label(reset_in),
            "source": "claude_desktop_usage_api",
        }

    primary = slot("five_hour", 300)
    secondary = slot("seven_day", 10080)
    if not primary and not secondary:
        return None
    claude: dict[str, Any] = {}
    if primary:
        claude["primary"] = primary
    if secondary:
        claude["secondary"] = secondary
    return {"claude": claude}


def _with_reset_label(slot: dict[str, Any], label: str) -> dict[str, Any]:
    if label:
        slot = dict(slot)
        slot["reset"] = label
    return slot


def payload_from_statusline(max_age_seconds: int = 3600) -> dict[str, Any] | None:
    if not CLAUDE_STATUS_PATH.exists():
        return None
    try:
        data = json.loads(CLAUDE_STATUS_PATH.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None
    captured = parse_ts(data.get("captured_at"))
    if captured is None:
        return None
    age = (datetime.now(timezone.utc) - captured).total_seconds()
    if age < 0 or age > max_age_seconds:
        return None
    primary = data.get("primary")
    secondary = data.get("secondary")
    if not isinstance(primary, dict) and not isinstance(secondary, dict):
        return None
    claude: dict[str, Any] = {}
    if isinstance(primary, dict):
        claude["primary"] = _status_slot(primary, 300)
    if isinstance(secondary, dict):
        claude["secondary"] = _status_slot(secondary, 10080)
    return {"claude": claude}


def build_payload(
    cap_5h: int,
    cap_7d: int,
    *,
    override_5h: float | None = None,
    override_7d: float | None = None,
    reset_label_5h: str = "",
    reset_label_7d: str = "",
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
    primary = {
        "used_percent": override_5h if override_5h is not None else pct(total_5h, cap_5h),
        "used": total_5h,
        "cap": cap_5h,
        "window_minutes": 300,
        "resets_in_seconds": reset_seconds(earliest_5h, window_5h, now),
        "source": "claude_official_override" if override_5h is not None else "claude_jsonl_estimate",
    }
    secondary = {
        "used_percent": override_7d if override_7d is not None else pct(total_7d, cap_7d),
        "used": total_7d,
        "cap": cap_7d,
        "window_minutes": 10080,
        "resets_in_seconds": reset_seconds(earliest_7d, window_7d, now),
        "source": "claude_official_override" if override_7d is not None else "claude_jsonl_estimate",
    }
    return {
        "claude": {
            "primary": _with_reset_label(primary, reset_label_5h),
            "secondary": _with_reset_label(secondary, reset_label_7d),
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
    parser.add_argument("--session-key", default=cfg.get("claude_session_key") or os.environ.get("CLAUDE_SESSION_KEY", ""),
                        help="claude.ai sessionKey cookie (copy from browser DevTools)")
    parser.add_argument("--organization-uuid", default=cfg.get("claude_organization_uuid", ""),
                        help="claude.ai org UUID; auto-discovered if omitted")
    parser.add_argument("--debug", action="store_true", help="print which data source was used and why fallbacks happened")
    args = parser.parse_args()

    use_manual = bool(cfg.get("claude_use_manual_usage_overrides", cfg.get("use_manual_usage_overrides")))
    desktop_payload = payload_from_desktop_usage_api(
        session_key=args.session_key or None,
        organization_uuid=args.organization_uuid or None,
        debug=args.debug,
    )
    if args.debug and desktop_payload is None:
        print("[claude_usage] desktop_usage_api unavailable, trying statusline", file=sys.stderr)
    statusline_payload = None if desktop_payload else payload_from_statusline(int(cfg.get("claude_status_max_age_seconds", 3600)))
    if args.debug and desktop_payload is None and statusline_payload is None:
        print("[claude_usage] statusline unavailable, falling back to JSONL token estimate", file=sys.stderr)
    payload = (
        desktop_payload
        or statusline_payload
        or build_payload(
        args.claude_5h_token_cap,
        args.claude_7d_token_cap,
        override_5h=_override_percent(cfg.get("claude_5h_used_percent")) if use_manual else None,
        override_7d=_override_percent(cfg.get("claude_7d_used_percent")) if use_manual else None,
        reset_label_5h=str(cfg.get("claude_5h_reset_label") or ""),
        reset_label_7d=str(cfg.get("claude_7d_reset_label") or ""),
        )
    )
    result = post_usage(args.relay, args.secret, payload)
    claude = result.get("usage", {}).get("claude", {})
    h5 = claude.get("h5", {}).get("pct", payload["claude"]["primary"]["used_percent"])
    d7 = claude.get("d7", {}).get("pct", payload["claude"]["secondary"]["used_percent"])
    print(f"posted claude usage: 5h={h5}% 7d={d7}%")


if __name__ == "__main__":
    main()
