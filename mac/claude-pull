#!/usr/bin/env python3
"""claude-pull — sync agent session artifacts from the Worker to your computer.

Designed to be run periodically by launchd (macOS) or Task Scheduler (Windows).
On every tick:

  1. Lists sessions via /console/sessions.
  2. For each session in {idle, terminated} that has files we haven't
     mirrored yet, lists files via /console/files.
  3. Downloads new files to ~/ClaudeRuns/<safe-title>-<sid8>/.
  4. Drops manifest.json + _done marker.
  5. Posts a desktop notification once per newly-completed session
     (macOS banner via osascript, Windows toast via PowerShell).

State is kept in ~/ClaudeRuns/.state.json so we don't re-download or
re-notify across runs.

Config: ~/.config/claude-pager/config.json
  {
    "worker_base": "https://....workers.dev",
    "device_secret": "...",
    "out_dir": "~/ClaudeRuns",       (optional, default ~/ClaudeRuns)
    "notify": true                    (optional, default true)
  }

Stdlib-only — no pip dependencies. Compatible with Python 3.9+.
On macOS uses /usr/bin/python3. On Windows uses any installed
python.exe / pythonw.exe.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

CONFIG_PATH = Path("~/.config/claude-pager/config.json").expanduser()
DEFAULT_OUT_DIR = Path("~/ClaudeRuns").expanduser()
STATE_FILENAME = ".state.json"
TIMEOUT = 30


# ---- config ---------------------------------------------------------

def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        die(
            f"missing {CONFIG_PATH}\n\n"
            "Create it with:\n"
            f"  mkdir -p {CONFIG_PATH.parent}\n"
            f'  cat > {CONFIG_PATH} <<EOF\n'
            "  {\n"
            '    "worker_base": "https://YOUR.workers.dev",\n'
            '    "device_secret": "YOUR_DEVICE_SECRET"\n'
            "  }\n"
            "  EOF\n",
        )
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as e:
        die(f"invalid JSON in {CONFIG_PATH}: {e}")
    for required in ("worker_base", "device_secret"):
        if not cfg.get(required):
            die(f"{CONFIG_PATH}: missing '{required}'")
    cfg["worker_base"] = cfg["worker_base"].rstrip("/")
    cfg["out_dir"] = Path(cfg.get("out_dir") or DEFAULT_OUT_DIR).expanduser()
    cfg.setdefault("notify", True)
    return cfg


def die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


# ---- state ----------------------------------------------------------

class State:
    """Tracks per-session sync state. Stored as a JSON blob in
    ``<out_dir>/.state.json``. Resilient to missing/corrupt files."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {"sessions": {}}
        if path.exists():
            try:
                loaded = json.loads(path.read_text())
                if isinstance(loaded, dict):
                    self.data = loaded
                    self.data.setdefault("sessions", {})
            except (json.JSONDecodeError, OSError):
                # Treat as empty; we'll write a fresh one.
                pass

    def get(self, sid: str) -> dict[str, Any]:
        return self.data["sessions"].setdefault(sid, {})

    def update(self, sid: str, **fields: Any) -> None:
        entry = self.get(sid)
        entry.update(fields)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        tmp.replace(self.path)


# ---- HTTP -----------------------------------------------------------

def _request(cfg: dict, path: str, *, method: str = "GET",
             body: dict | None = None, raw: bool = False):
    url = cfg["worker_base"] + path
    sep = "&" if "?" in path else "?"
    url = url + sep + "token=" + urllib.parse.quote(cfg["device_secret"])
    data = None
    headers = {"x-device-secret": cfg["device_secret"]}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx)
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", errors="replace")
        raise APIError(e.code, body_txt) from e
    except urllib.error.URLError as e:
        raise APIError(0, f"transport: {e.reason}") from e
    if raw:
        return resp
    text = resp.read().decode("utf-8", errors="replace")
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


class APIError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail[:300]}")
        self.status = status
        self.detail = detail


# ---- sync logic -----------------------------------------------------

# Filenames must be safe as macOS dir names. Strip / and other
# noise; collapse repeats; cap length.
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", (name or "").strip()).strip("-").lower()
    if not s:
        s = "untitled"
    return s[:48]


def session_dir(out: Path, sess: dict) -> Path:
    title = sess.get("title") or sess.get("session_id", "untitled")
    sid_tail = sess.get("session_id", "")[-8:]
    return out / f"{slugify(title)}-{sid_tail}"


def is_complete(sess: dict) -> bool:
    summary = sess.get("summary") or {}
    status = summary.get("status")
    return status in ("idle", "terminated")


def list_sessions(cfg) -> list[dict]:
    data = _request(cfg, "/console/sessions")
    if not isinstance(data, dict):
        raise APIError(0, "sessions: bad shape")
    return data.get("sessions") or []


def list_files(cfg, sid: str) -> list[dict]:
    data = _request(cfg, "/console/files?session=" + urllib.parse.quote(sid))
    if not isinstance(data, dict):
        return []
    return data.get("files") or []


def download_file(cfg, sid: str, file_id: str, dest: Path) -> int:
    """Stream a single file to disk. Returns size in bytes."""
    qs = urllib.parse.urlencode({"session": sid, "file_id": file_id})
    resp = _request(cfg, "/console/file?" + qs, raw=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    total = 0
    try:
        with tmp.open("wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
        tmp.replace(dest)
    finally:
        if tmp.exists():
            try: tmp.unlink()
            except OSError: pass
    return total


def notify(title: str, body: str) -> None:
    """Best-effort desktop notification. Supports macOS and Windows."""
    safe_title = title[:80]
    safe_body = body[:200]

    if sys.platform == "darwin":
        # macOS: osascript display notification
        t = safe_title.replace('"', '\\"')
        b = safe_body.replace('"', '\\"')
        script = f'display notification "{b}" with title "{t}"'
        try:
            subprocess.run(
                ["osascript", "-e", script],
                check=False, timeout=10,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return

    if sys.platform == "win32":
        # Windows: PowerShell + Windows.UI.Notifications via XML toast.
        # Stdlib-only; no need to install win10toast.
        t = safe_title.replace("'", "''")
        b = safe_body.replace("'", "''")
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager,"
            "Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null;"
            "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,"
            "ContentType=WindowsRuntime] | Out-Null;"
            f"$xml = '<toast><visual><binding template=\"ToastText02\">"
            f"<text id=\"1\">{t}</text><text id=\"2\">{b}</text>"
            f"</binding></visual></toast>';"
            "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument;"
            "$doc.LoadXml($xml);"
            "$toast = New-Object Windows.UI.Notifications.ToastNotification $doc;"
            "[Windows.UI.Notifications.ToastNotificationManager]"
            "::CreateToastNotifier('ClaudePager').Show($toast);"
        )
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden",
                 "-Command", ps],
                check=False, timeout=10,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return

    # Other platforms: silent no-op
    return


def sync_session(cfg, state: State, sess: dict) -> int:
    """Sync one session. Returns count of newly downloaded files."""
    sid = sess["session_id"]
    sdir = session_dir(cfg["out_dir"], sess)
    sdir.mkdir(parents=True, exist_ok=True)

    entry = state.get(sid)
    seen_ids = set(entry.get("seen_file_ids") or [])
    files = list_files(cfg, sid)
    if not files:
        return 0

    new_count = 0
    manifest = []
    for f in files:
        fid = f.get("id")
        name = f.get("filename") or fid or "unnamed"
        size = f.get("size")
        # Strip any leading slashes so we can't write outside sdir.
        rel_name = name.lstrip("/")
        # Collapse any ".." path traversal — we only honor the leaf
        # name to keep things flat under sdir. The Files API's
        # `filename` is server-controlled, but defense-in-depth.
        if ".." in Path(rel_name).parts:
            rel_name = Path(rel_name).name
        dest = sdir / rel_name
        manifest.append({"file_id": fid, "filename": rel_name, "size": size})
        if fid in seen_ids and dest.exists():
            continue
        try:
            actual = download_file(cfg, sid, fid, dest)
        except APIError as e:
            print(f"  ! download failed for {rel_name}: {e}", file=sys.stderr)
            continue
        seen_ids.add(fid)
        new_count += 1
        print(f"  + {dest} ({actual}B)")

    # Manifest is rewritten every run — cheap and self-healing.
    (sdir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))

    # Mark done — first time only — and notify.
    if new_count > 0 and is_complete(sess):
        (sdir / "_done.json").write_text(json.dumps({
            "session_id": sid,
            "title": sess.get("title"),
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "summary": sess.get("summary"),
        }, indent=2, sort_keys=True))
        if cfg["notify"] and not entry.get("notified"):
            notify(
                f"Claude · {sess.get('title') or sid[:12]}",
                f"{new_count} file(s) ready in {sdir}",
            )
            entry["notified"] = True

    entry["seen_file_ids"] = sorted(seen_ids)
    entry["last_status"] = (sess.get("summary") or {}).get("status")
    entry["last_synced_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return new_count


# ---- main -----------------------------------------------------------

def main(argv: list[str]) -> int:
    cfg = load_config()
    out = cfg["out_dir"]
    out.mkdir(parents=True, exist_ok=True)
    state = State(out / STATE_FILENAME)

    verbose = "-v" in argv or "--verbose" in argv
    once = "--once" in argv  # default behavior, kept for clarity

    try:
        sessions = list_sessions(cfg)
    except APIError as e:
        if verbose: print(f"sessions: {e}", file=sys.stderr)
        # Don't error-loud on a transient network blip — launchd's
        # exit-code histogram would fill with noise.
        return 0

    total_new = 0
    for s in sessions:
        sid = s.get("session_id")
        if not sid:
            continue
        try:
            n = sync_session(cfg, state, s)
            total_new += n
            if verbose and n > 0:
                print(f"~ {sid[-8:]}: {n} new")
        except APIError as e:
            print(f"~ {sid[-8:]}: error {e}", file=sys.stderr)
        except OSError as e:
            print(f"~ {sid[-8:]}: filesystem error {e}", file=sys.stderr)

    state.save()
    if verbose:
        print(f"done · {total_new} new file(s) across {len(sessions)} session(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
