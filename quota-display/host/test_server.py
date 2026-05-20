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
        },
    }


def main() -> int:
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        claude_dir = root / "claude"
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

        # Reset the module-level file cache so a previous test run
        # doesn't leak state.
        server._FILE_CACHE.clear()

        snap = server.build_snapshot(cfg)
        hb = server.heartbeat_payload(snap)

    failures = []

    def expect(label, got, want):
        if got != want:
            failures.append(f"  {label}: got {got!r}, want {want!r}")

    expect("claude 5h tokens", snap["claude"]["5h"]["tokens"], 5000)
    expect("claude 7d tokens", snap["claude"]["7d"]["tokens"], 25000)

    # Codex now comes from rate_limits, not summed tokens. The newer
    # snapshot (42% / 38%) should override the older (10% / 20%).
    expect("codex 5h pct",   snap["codex"]["5h"]["pct"], 42)
    expect("codex 7d pct",   snap["codex"]["7d"]["pct"], 38)
    expect("codex 5h source", snap["codex"]["5h"]["source"], "rate_limits")
    expect("codex plan",     snap["codex"]["plan"], "plus")

    # _pct floors: 5000 / 200000 = 2.5 → 2
    expect("claude 5h pct", snap["claude"]["5h"]["pct"], 2)

    expect("hb usage_view", hb["usage_view"], "claude")
    expect("hb claude_5h_pct", hb["claude_5h_pct"], 2)
    expect("hb codex_5h_pct",  hb["codex_5h_pct"], 42)

    # Reset time: oldest event in 5h window is 30 min old → reset in ~4h30m
    reset = snap["claude"]["5h"]["reset_s"]
    if not (4 * 3600 <= reset <= 5 * 3600):
        failures.append(f"  claude 5h reset_s: got {reset}, want 4h..5h")

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
