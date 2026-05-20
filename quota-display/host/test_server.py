"""End-to-end test: synthesize a few JSONL log files in a temp dir,
run the parser, and assert the snapshot matches expectations.

Run from the repo root:
    python quota-display/host/test_server.py
"""

import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import server  # noqa: E402


def write_jsonl(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def claude_event(ts: float, tokens: int) -> dict:
    """Shape Claude Code writes."""
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
        "message": {"usage": {"input_tokens": tokens // 2, "output_tokens": tokens // 2}},
    }


def codex_rate_limits_event(ts: float, primary_pct: float, secondary_pct: float,
                            primary_reset: float, secondary_reset: float) -> dict:
    """Shape Codex CLI's rollout event_msg/token_count event."""
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {"total_tokens": 36906},
                "last_token_usage": {"total_tokens": 36906},
                "model_context_window": 258400,
            },
            "rate_limits": {
                "primary": {
                    "used_percent": primary_pct,
                    "window_minutes": 300,
                    "resets_at": primary_reset,
                },
                "secondary": {
                    "used_percent": secondary_pct,
                    "window_minutes": 10080,
                    "resets_at": secondary_reset,
                },
                "plan_type": "plus",
            },
        },
    }


def write_claude_hud_cache(claude_home: Path, data: dict) -> None:
    """Write the shape that jarrodwatts/claude-hud caches."""
    path = claude_home / "plugins" / "claude-hud" / ".usage-cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = {"data": data, "timestamp": int(time.time() * 1000), "lastGoodData": data}
    path.write_text(json.dumps(blob), encoding="utf-8")


def main() -> int:
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Match the real layout: claude_log_dir = .claude/projects so
        # the parent gives us .claude/ for the claude-hud cache.
        claude_home = root / "claude"
        claude_dir = claude_home / "projects"
        codex_dir = root / "codex"

        # Two claude sessions:
        # - 30 minutes ago: 5000 tokens (counts toward 5h + 7d)
        # - 2 days ago: 20000 tokens (counts toward 7d only)
        write_jsonl(
            claude_dir / "proj-a" / "session1.jsonl",
            [claude_event(now - 30 * 60, 5000)],
        )
        write_jsonl(
            claude_dir / "proj-a" / "session2.jsonl",
            [claude_event(now - 2 * 86400, 20000)],
        )

        # claude-hud cache: Pro plan, 12% 5h / 47% 7d.
        from datetime import datetime, timezone
        def iso(t: float) -> str:
            return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        write_claude_hud_cache(claude_home, {
            "planName": "Pro",
            "fiveHour": 12,
            "sevenDay": 47,
            "fiveHourResetAt": iso(now + 3 * 3600),
            "sevenDayResetAt": iso(now + 5 * 86400),
        })

        # One codex rollout file with two rate_limits snapshots; the
        # later one should win.
        write_jsonl(
            codex_dir / "2026" / "05" / "20" / "rollout-1.jsonl",
            [
                codex_rate_limits_event(
                    now - 7200, primary_pct=10, secondary_pct=20,
                    primary_reset=now - 3600,        # already past
                    secondary_reset=now + 5 * 86400,
                ),
                codex_rate_limits_event(
                    now - 600, primary_pct=42, secondary_pct=38,
                    primary_reset=now + 4 * 3600,    # in the future
                    secondary_reset=now + 5 * 86400,
                ),
            ],
        )

        cfg = {
            "claude_log_dir": str(claude_dir),
            "codex_log_dir": str(codex_dir),
            "claude_5h_cap": 200000,
            "claude_7d_cap": 1_400_000,
            "codex_5h_cap": 50000,
            "codex_7d_cap": 350_000,
        }

        # Reset module-level caches so a previous test run doesn't
        # leak state, and short-circuit the OAuth fetch (we don't want
        # the test to actually hit api.anthropic.com).
        server._FILE_CACHE.clear()
        server._OAUTH_USAGE_CACHE.update({"ts": 0.0, "data": None})
        server.fetch_claude_oauth_usage = lambda _home: None

        snap = server.build_snapshot(cfg)
        hb = server.heartbeat_payload(snap)

        # Second snapshot with a mock OAuth response, while the tempdir
        # is still alive (so find_claude_hud_usage can still read the
        # cache file for plan name).
        server._OAUTH_USAGE_CACHE.update({"ts": 0.0, "data": None})
        server.fetch_claude_oauth_usage = lambda _home: {
            "five_hour":  {"utilization": 78, "resets_at": iso(now + 2 * 3600)},
            "seven_day":  {"utilization": 55, "resets_at": iso(now + 6 * 86400)},
        }
        snap2 = server.build_snapshot(cfg)

    failures = []

    def expect(label, got, want):
        if got != want:
            failures.append(f"  {label}: got {got!r}, want {want!r}")

    # Claude now comes from claude-hud cache, not summed tokens.
    expect("claude 5h pct",   snap["claude"]["5h"]["pct"], 12)
    expect("claude 7d pct",   snap["claude"]["7d"]["pct"], 47)
    expect("claude 5h source", snap["claude"]["5h"]["source"], "claude_hud")
    expect("claude plan",     snap["claude"]["plan"], "Pro")

    # Codex from rate_limits — newer snapshot (42% / 38%) wins over older.
    expect("codex 5h pct",   snap["codex"]["5h"]["pct"], 42)
    expect("codex 7d pct",   snap["codex"]["7d"]["pct"], 38)
    expect("codex 5h source", snap["codex"]["5h"]["source"], "rate_limits")
    expect("codex plan",     snap["codex"]["plan"], "plus")

    expect("hb usage_view", hb["usage_view"], "claude")
    expect("hb claude_5h_pct", hb["claude_5h_pct"], 12)
    expect("hb codex_5h_pct",  hb["codex_5h_pct"], 42)

    # OAuth response wins over hud cache for percentages.
    expect("oauth wins claude 5h pct", snap2["claude"]["5h"]["pct"], 78)
    expect("oauth wins claude 7d pct", snap2["claude"]["7d"]["pct"], 55)
    expect("oauth source",             snap2["claude"]["5h"]["source"], "claude_oauth")
    # Plan still comes from hud since OAuth response doesn't carry it.
    expect("oauth + hud plan",         snap2["claude"]["plan"], "Pro")

    # Claude hud cache sets fiveHourResetAt = now + 3h, so the
    # snapshot's reset_s should land just under 3 * 3600.
    reset = snap["claude"]["5h"]["reset_s"]
    if not (3 * 3600 - 60 <= reset <= 3 * 3600):
        failures.append(f"  claude 5h reset_s: got {reset}, want ~3h")

    if failures:
        print("FAIL")
        for line in failures:
            print(line)
        return 1
    print("ok — all 9 assertions passed")
    print(json.dumps(snap, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
