#!/usr/bin/env python3
"""Local auto-queue E2E load runner for canonical chat-completion requests."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from urllib import error, request

SCENARIOS = {
    "baseline": {"requests": 1, "concurrency": 1},
    "burst-5": {"requests": 5, "concurrency": 5},
    "burst-10": {"requests": 10, "concurrency": 10},
    "soak-20": {"requests": 20, "concurrency": 5},
}


def build_payload(model: str) -> dict[str, Any]:
    # Keep canonical payload shape unchanged.
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Hello, who are you!"}],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local auto-queue E2E scenarios")
    parser.add_argument("--scenario", choices=SCENARIOS.keys(), required=True)
    parser.add_argument(
        "--base-url",
        default=os.getenv("TASK1_BASE_URL") or os.getenv("AUTOQ_BASE_URL") or "http://localhost:4000",
        help="Base URL for proxy runtime (default: TASK1_BASE_URL|AUTOQ_BASE_URL|http://localhost:4000)",
    )
    parser.add_argument(
        "--auth-token",
        default=os.getenv("TASK1_AUTH_TOKEN") or os.getenv("TASK1_CANONICAL_AUTH_TOKEN"),
        help="Bearer token (default: TASK1_AUTH_TOKEN|TASK1_CANONICAL_AUTH_TOKEN)",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("TASK2_MODEL") or "glm-5.1",
        help="Model for baseline/load requests (default: TASK2_MODEL|glm-5.1)",
    )
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--excerpt-chars", type=int, default=240)
    return parser.parse_args()


def _now_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _excerpt(text: str, limit: int) -> str:
    return text.replace("\n", " ").strip()[:limit]


def send_request(
    request_index: int,
    endpoint: str,
    auth_token: str,
    payload_bytes: bytes,
    timeout_seconds: float,
    excerpt_chars: int,
) -> dict[str, Any]:
    start_ts = time.time()
    status_code: int | None = None
    body_text = ""
    error_type: str | None = None

    req = request.Request(
        endpoint,
        data=payload_bytes,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth_token}",
        },
    )

    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:
            status_code = resp.status
            body_text = resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        status_code = exc.code
        body_text = exc.read().decode("utf-8", errors="replace")
        error_type = "HTTPError"
    except error.URLError as exc:
        error_type = "URLError"
        body_text = f"{type(exc.reason).__name__}: {exc.reason}"
    except Exception as exc:  # pragma: no cover - defensive runner path
        error_type = type(exc).__name__
        body_text = str(exc)

    end_ts = time.time()
    return {
        "request_index": request_index,
        "start_time": _now_iso(start_ts),
        "end_time": _now_iso(end_ts),
        "latency_ms": round((end_ts - start_ts) * 1000.0, 3),
        "status_code": status_code,
        "response_excerpt": _excerpt(body_text, excerpt_chars),
        "error_type": error_type,
    }


def main() -> int:
    args = parse_args()
    if not args.auth_token:
        print(
            "Missing auth token. Set --auth-token or TASK1_AUTH_TOKEN/TASK1_CANONICAL_AUTH_TOKEN.",
            file=sys.stderr,
        )
        return 2

    scenario_cfg = SCENARIOS[args.scenario]
    request_count = scenario_cfg["requests"]
    concurrency = scenario_cfg["concurrency"]
    endpoint = f"{args.base_url.rstrip('/')}/v1/chat/completions"
    payload = build_payload(args.model)
    payload_bytes = json.dumps(payload).encode("utf-8")

    print(
        json.dumps(
            {
                "event": "run_start",
                "scenario": args.scenario,
                "requests": request_count,
                "concurrency": concurrency,
                "endpoint": endpoint,
                "model": args.model,
                "timeout_seconds": args.timeout_seconds,
            }
        )
    )

    run_start = time.time()
    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(
                send_request,
                i,
                endpoint,
                args.auth_token,
                payload_bytes,
                args.timeout_seconds,
                args.excerpt_chars,
            )
            for i in range(request_count)
        ]
        for future in as_completed(futures):
            records.append(future.result())

    records.sort(key=lambda r: int(r["request_index"]))
    for record in records:
        print(json.dumps({"event": "request_result", **record}))

    status_counts = {"200": 0, "503": 0, "504": 0}
    other_status_counts: dict[str, int] = {}
    transport_errors = 0
    for record in records:
        status_code = record.get("status_code")
        if status_code is None:
            transport_errors += 1
            continue
        key = str(status_code)
        if key in status_counts:
            status_counts[key] += 1
        else:
            other_status_counts[key] = other_status_counts.get(key, 0) + 1

    summary = {
        "event": "run_summary",
        "scenario": args.scenario,
        "requests": request_count,
        "concurrency": concurrency,
        "endpoint": endpoint,
        "model": args.model,
        "status_counts": status_counts,
        "other_status_counts": other_status_counts,
        "transport_errors": transport_errors,
        "success_200": status_counts["200"],
        "duration_ms": round((time.time() - run_start) * 1000.0, 3),
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
