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
import socketserver
import sys
import threading
import time
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
    with _CACHE_LOCK:
        claude_samples = scan_logs(Path(cfg["claude_log_dir"]))

    # Codex CLI writes the server-reported subscription rate limits
    # straight into rollout files, so we just lift them out. Fall back
    # to local token summing only if no rate_limits block is found —
    # that path is mostly a no-op since rollout events don't carry
    # per-turn token totals in a shape we can sum reliably.
    codex_rl = find_codex_rate_limits(codex_root)
    if codex_rl is not None:
        codex_view = {
            "5h": _codex_window(codex_rl.get("primary"), now),
            "7d": _codex_window(codex_rl.get("secondary"), now),
            "plan": codex_rl.get("plan_type") or "",
        }
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

    claude_view = {
        "5h": _window(claude_samples, _FIVE_HOURS_S, cfg["claude_5h_cap"], now),
        "7d": _window(claude_samples, _SEVEN_DAYS_S, cfg["claude_7d_cap"], now),
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


_DASHBOARD_HTML = """<!doctype html>
<meta charset=utf-8>
<title>Claude / Codex Quota</title>
<style>
  /* The screen mockup is 240x135 — same as the Cardputer LCD. Coords
     in JS below match buddy_ui_cp.py exactly so this preview is a
     faithful render of what the M5 will draw. */
  :root { --scale: 3; }
  html, body { background: #1f1f1f; color: #f0eee6;
               font-family: ui-monospace, Menlo, monospace;
               margin: 0; min-height: 100vh;
               display: flex; flex-direction: column;
               align-items: center; justify-content: center; gap: 16px; }
  .screen-wrap { position: relative; }
  .screen {
    width: calc(240px * var(--scale));
    height: calc(135px * var(--scale));
    background: #f0eee6;
    image-rendering: pixelated;
    position: relative;
    overflow: hidden;
    box-shadow: 0 0 0 4px #333, 0 0 0 8px #1f1f1f,
                0 12px 32px rgba(0,0,0,0.6);
    border-radius: 6px;
  }
  /* Inner uses absolute 240x135 coords and scales as a whole. */
  .inner { position: absolute; left: 0; top: 0; width: 240px; height: 135px;
           transform: scale(var(--scale)); transform-origin: top left;
           color: #111; font-family: 'DejaVu Sans Mono', monospace; }
  .header-title { position: absolute; top: 7px; width: 100%;
                  text-align: center; font-size: 9px; font-weight: bold;
                  letter-spacing: 0.05em; }
  .row { position: absolute; left: 6px; right: 6px; }
  .row .label { font-size: 14px; font-weight: bold; line-height: 14px; }
  .row .pct { position: absolute; right: 0; top: 5px;
              font-size: 9px; color: #111; }
  .row .bar { position: absolute; left: 0; right: 0; top: 22px; height: 5px;
              background: #d9d6cc; }
  .row .fill { height: 100%; transition: width 0.4s ease; }
  .row .reset { position: absolute; left: 0; top: 30px;
                font-size: 9px; color: #777; white-space: nowrap; }
  .icon { position: absolute; left: 6px; top: 3px; width: 17px; height: 17px; }
  .pager { font-size: 11px; color: #777; }
  .pager .key { color: #f0eee6; }
  .stamp { font-size: 11px; color: #555; }
  .err { color: #ff8866; }
</style>

<div class=screen-wrap>
  <div class=screen>
    <div class=inner>
      <svg class=icon viewBox="0 0 17 17" id=icon></svg>
      <div class=header-title id=title>—</div>

      <div class=row style="top:34px">
        <span class=label>5H</span>
        <span class=pct id=p5>0%</span>
        <div class=bar><div class=fill id=f5 style="background:#cc785c;width:0"></div></div>
        <div class=reset id=r5>RESETS --</div>
      </div>

      <div class=row style="top:80px">
        <span class=label>7D</span>
        <span class=pct id=p7>0%</span>
        <div class=bar><div class=fill id=f7 style="background:#cc785c;width:0"></div></div>
        <div class=reset id=r7>RESETS --</div>
      </div>
    </div>
  </div>
</div>

<div class=pager>
  <span class=key>← →</span> / <span class=key>A D</span> / <span class=key>Tab</span> switch
  &nbsp;·&nbsp; <span id=which>—</span>
</div>
<div class=stamp id=ts>warming up</div>

<script>
const CLAUDE_ICON = [
  "00000110001000000",
  "00000110011000000",
  "000001110110111000",
  "001110110110111000",
  "00111111111111000",
  "0001111111111000",
  "00000111111111111",
  "01111111111111111",
  "0111111111111110",
  "00000011111111111",
  "0000111111111001",
  "00011111111111",
  "000101101101111",
  "0000011011011",
  "0000010011001",
  "000000001",
];
const GPT_ICON = [
  "00000011111000",
  "00000111111111",
  "000011001111111",
  "0011110111000011",
  "0111111100111001",
  "0110111111101111",
  "0110101111110111",
  "01101111001111011",
  "01101111001111011",
  "00111011111101011",
  "00111101111111011",
  "00100111001111111",
  "0011000011101111",
  "00011111110011",
  "0000111111111",
  "000000011111",
];
function renderIcon(rows, color) {
  let rects = "";
  for (let y = 0; y < rows.length; y++) {
    const r = rows[y];
    for (let x = 0; x < r.length; x++) {
      if (r[x] === "1") rects += `<rect x="${x}" y="${y}" width="1" height="1" fill="${color}"/>`;
    }
  }
  return rects;
}

function fmtReset(sec) {
  if (!sec) return "--";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  if (h >= 24) {
    const d = Math.floor(h / 24);
    const rh = h % 24;
    return `IN ${d}D ${rh}H`;
  }
  if (h) return `IN ${h} HR ${m} MIN`;
  return `IN ${m} MIN`;
}

let viewIdx = 0;          // 0 = claude, 1 = codex
let latest = null;
const VIEWS = ["claude", "codex"];

function paint() {
  if (!latest) return;
  const key = VIEWS[viewIdx];
  const w5 = latest[key]["5h"], w7 = latest[key]["7d"];
  const accent = key === "claude" ? "#cc785c" : "#111111";

  document.getElementById("title").textContent = key.toUpperCase();
  const planText = key === "codex" && latest.codex.plan
    ? ` ${latest.codex.plan.toUpperCase()}` : "";
  document.getElementById("title").textContent = key.toUpperCase() + planText;

  document.getElementById("icon").innerHTML =
    renderIcon(key === "claude" ? CLAUDE_ICON : GPT_ICON, accent);

  document.getElementById("p5").textContent = w5.pct + "% USED";
  document.getElementById("p7").textContent = w7.pct + "% USED";

  document.getElementById("f5").style.background = accent;
  document.getElementById("f7").style.background = accent;
  document.getElementById("f5").style.width = w5.pct + "%";
  document.getElementById("f7").style.width = w7.pct + "%";

  document.getElementById("r5").textContent = "RESETS " + fmtReset(w5.reset_s);
  document.getElementById("r7").textContent = "RESETS " + fmtReset(w7.reset_s);

  document.getElementById("which").textContent =
    `viewing ${key} (${viewIdx + 1}/${VIEWS.length})`;
}

function switchView(delta) {
  viewIdx = (viewIdx + delta + VIEWS.length) % VIEWS.length;
  paint();
}

async function tick() {
  try {
    const r = await fetch("/api/quota");
    if (!r.ok) throw new Error(r.status);
    latest = await r.json();
    paint();
    document.getElementById("ts").textContent =
      "updated " + new Date(latest.generated_at * 1000).toLocaleTimeString();
  } catch (e) {
    document.getElementById("ts").innerHTML =
      "<span class=err>fetch error</span>";
  }
}

document.addEventListener("keydown", (e) => {
  if (e.key === "ArrowRight" || e.key === "Tab" || e.key === "d" || e.key === "D") {
    e.preventDefault(); switchView(1);
  } else if (e.key === "ArrowLeft" || e.key === "a" || e.key === "A") {
    e.preventDefault(); switchView(-1);
  }
});
document.querySelector(".screen").addEventListener("click", () => switchView(1));

tick(); setInterval(tick, 5000);
</script>
"""


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
            self._send(200, _DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
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
