#!/usr/bin/env python3
"""Poll /queue/status snapshots before/during/after load."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Poll /queue/status snapshots")
    parser.add_argument("--model", required=True, help="Target model row to extract")
    parser.add_argument(
        "--base-url",
        default=os.getenv("TASK1_BASE_URL") or os.getenv("AUTOQ_BASE_URL") or "http://localhost:4000",
        help="Proxy base URL (default: TASK1_BASE_URL|AUTOQ_BASE_URL|http://localhost:4000)",
    )
    parser.add_argument(
        "--auth-token",
        default=os.getenv("TASK1_AUTH_TOKEN") or os.getenv("TASK1_CANONICAL_AUTH_TOKEN"),
        help="Bearer token (default: TASK1_AUTH_TOKEN|TASK1_CANONICAL_AUTH_TOKEN)",
    )
    parser.add_argument("--before-snapshots", type=int, default=1)
    parser.add_argument("--during-seconds", type=float, default=30.0)
    parser.add_argument("--after-snapshots", type=int, default=3)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--output-file", default="/tmp/task3_queue_snapshots.json")
    parser.add_argument("--require-during-snapshot", action="store_true")
    parser.add_argument("--require-post-idle", action="store_true")
    return parser.parse_args()


def _now_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _fetch_queue_status(base_url: str, auth_token: str) -> tuple[int | None, str, Any]:
    url = f"{base_url.rstrip('/')}/queue/status"
    req = request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        },
    )
    status_code: int | None = None
    body_text = ""
    body_json: Any = None
    try:
        with request.urlopen(req, timeout=30) as resp:
            status_code = resp.status
            body_text = resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        status_code = exc.code
        body_text = exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover - defensive runtime path
        body_text = f"{type(exc).__name__}: {exc}"
    try:
        body_json = json.loads(body_text)
    except Exception:
        body_json = None
    return status_code, body_text, body_json


def _extract_model_row(body_json: Any, target_model: str) -> tuple[bool, Any, int]:
    if not isinstance(body_json, dict):
        return False, None, 0
    models = body_json.get("models")
    if not isinstance(models, dict):
        return False, None, 0
    model_row = models.get(target_model)
    return isinstance(model_row, dict), model_row, len(models)


def _is_idle_model_row(model_row: Any) -> bool:
    if not isinstance(model_row, dict):
        return False
    active = int(model_row.get("active", -1))
    queued = int(model_row.get("queued", -1))
    local_waiters = int(model_row.get("local_waiters", -1))
    return active == 0 and queued == 0 and local_waiters == 0


def _capture_snapshot(
    snapshots: list[dict[str, Any]],
    phase: str,
    model: str,
    base_url: str,
    auth_token: str,
    excerpt_chars: int = 300,
) -> None:
    ts = time.time()
    status_code, body_text, body_json = _fetch_queue_status(base_url, auth_token)
    model_present, model_row, model_count = _extract_model_row(body_json, model)
    snapshots.append(
        {
            "phase": phase,
            "timestamp": _now_iso(ts),
            "epoch": ts,
            "http_status": status_code,
            "target_model": model,
            "target_model_present": model_present,
            "target_model_row": model_row,
            "models_count": model_count,
            "response_excerpt": body_text.replace("\n", " ")[:excerpt_chars],
        }
    )


def main() -> int:
    args = parse_args()
    if not args.auth_token:
        print(
            "Missing auth token. Set --auth-token or TASK1_AUTH_TOKEN/TASK1_CANONICAL_AUTH_TOKEN.",
            file=sys.stderr,
        )
        return 2

    snapshots: list[dict[str, Any]] = []

    for _ in range(max(0, args.before_snapshots)):
        _capture_snapshot(snapshots, "before", args.model, args.base_url, args.auth_token)
        time.sleep(max(0.0, args.interval_seconds))

    during_start = time.time()
    while time.time() - during_start < max(0.0, args.during_seconds):
        _capture_snapshot(snapshots, "during", args.model, args.base_url, args.auth_token)
        time.sleep(max(0.0, args.interval_seconds))

    for _ in range(max(0, args.after_snapshots)):
        _capture_snapshot(snapshots, "after", args.model, args.base_url, args.auth_token)
        time.sleep(max(0.0, args.interval_seconds))

    http_counts: dict[str, int] = {}
    phase_counts: dict[str, int] = {}
    for snap in snapshots:
        http_key = str(snap.get("http_status"))
        http_counts[http_key] = http_counts.get(http_key, 0) + 1
        phase = str(snap.get("phase"))
        phase_counts[phase] = phase_counts.get(phase, 0) + 1

    during_count = sum(1 for s in snapshots if s.get("phase") == "during")
    post_idle = any(
        s.get("phase") == "after" and _is_idle_model_row(s.get("target_model_row"))
        for s in snapshots
    )

    output = {
        "config": {
            "base_url": args.base_url,
            "target_model": args.model,
            "before_snapshots": args.before_snapshots,
            "during_seconds": args.during_seconds,
            "after_snapshots": args.after_snapshots,
            "interval_seconds": args.interval_seconds,
        },
        "summary": {
            "snapshot_count": len(snapshots),
            "phase_counts": phase_counts,
            "http_status_counts": http_counts,
            "during_snapshot_count": during_count,
            "post_run_idle_observed": post_idle,
        },
        "snapshots": snapshots,
    }

    out_path = Path(args.output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")

    print(json.dumps(output["summary"]))
    print(f"wrote_snapshots={out_path}")

    if args.require_during_snapshot and during_count < 1:
        print("No during-load snapshots captured.", file=sys.stderr)
        return 3
    if args.require_post_idle and not post_idle:
        print("Post-run idle state not observed for target model.", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
