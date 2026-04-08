#!/usr/bin/env python3
"""Local auto-queue E2E load runner for canonical chat-completion requests."""

from __future__ import annotations

import argparse
import json
import math
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
    "overflow": {
        "requests": 50,
        "concurrency": 50,
        "expect_bounded_failure": True,
        "bounded_failure_statuses": [503, 429],
        "verify_queue_drain": True,
    },
    "timeout": {
        "requests": 20,
        "concurrency": 20,
        "expect_timeout": True,
        "timeout_seconds": 0.001,
        "verify_queue_drain": True,
    },
}


def _env_truthy(name: str) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def build_payload(model: str) -> dict[str, Any]:
    # Keep canonical chat-completions shape unchanged; model value is runtime-selectable.
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
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=None,
        help="Client timeout in seconds; default uses scenario timeout or 60.0",
    )
    parser.add_argument("--excerpt-chars", type=int, default=240)
    parser.add_argument(
        "--allow-queue-drain-skip",
        action=argparse.BooleanOptionalAction,
        default=_env_truthy("AUTOQ_ALLOW_QUEUE_DRAIN_SKIP"),
        help="Allow degraded pass when queue-drain verification is skipped (default: false; env override: AUTOQ_ALLOW_QUEUE_DRAIN_SKIP=1). Use --no-allow-queue-drain-skip to force strict mode.",
    )
    parser.add_argument(
        "--queue-drain-settle-seconds",
        type=float,
        default=float(os.getenv("AUTOQ_QUEUE_DRAIN_SETTLE_SECONDS", "15")),
        help="Max seconds to poll /queue/status for idle convergence after pressure runs (default: 15; env AUTOQ_QUEUE_DRAIN_SETTLE_SECONDS).",
    )
    parser.add_argument(
        "--queue-drain-poll-interval-seconds",
        type=float,
        default=float(os.getenv("AUTOQ_QUEUE_DRAIN_POLL_INTERVAL_SECONDS", "1")),
        help="Polling interval for queue-drain checks (default: 1; env AUTOQ_QUEUE_DRAIN_POLL_INTERVAL_SECONDS).",
    )
    return parser.parse_args()


def _now_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _excerpt(text: str, limit: int) -> str:
    return text.replace("\n", " ").strip()[:limit]


def _is_timeout_reason(reason: Any) -> bool:
    if isinstance(reason, TimeoutError):
        return True
    reason_text = str(reason).lower()
    return "timed out" in reason_text or "timeout" in reason_text


def _fetch_queue_status(base_url: str, auth_token: str) -> tuple[int | None, str, Any]:
    status_endpoint = f"{base_url.rstrip('/')}/queue/status"
    req = request.Request(
        status_endpoint,
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
        with request.urlopen(req, timeout=15.0) as resp:
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


def _safe_parse_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _evaluate_queue_status_sample(
    status_code: int | None,
    body_text: str,
    body_json: Any,
    model: str,
) -> dict[str, Any]:
    excerpt = _excerpt(body_text, 240)
    if status_code is None:
        return {
            "status": "skipped",
            "reason": f"queue status request failed before HTTP response: {excerpt}",
            "http_status": None,
        }
    if status_code in (404, 503):
        return {
            "status": "skipped",
            "reason": f"/queue/status unavailable/degraded (http_status={status_code}).",
            "http_status": status_code,
        }
    if status_code in (401, 403):
        return {
            "status": "failed",
            "reason": f"/queue/status returned auth/permission error (http_status={status_code}).",
            "http_status": status_code,
        }
    if status_code != 200:
        return {
            "status": "failed",
            "reason": f"/queue/status returned unexpected status {status_code}.",
            "http_status": status_code,
        }
    if not isinstance(body_json, dict):
        return {
            "status": "failed",
            "reason": "/queue/status returned 200 but body was not valid JSON object.",
            "http_status": status_code,
        }
    models = body_json.get("models")
    if not isinstance(models, dict):
        return {
            "status": "failed",
            "reason": "/queue/status returned 200 but missing or invalid 'models' object.",
            "http_status": status_code,
        }
    model_row = models.get(model)
    if not isinstance(model_row, dict):
        return {
            "status": "failed",
            "reason": f"/queue/status returned 200 but model row '{model}' was missing/invalid.",
            "http_status": status_code,
        }
    active = _safe_parse_int(model_row.get("active"))
    queued = _safe_parse_int(model_row.get("queued"))
    local_waiters = _safe_parse_int(model_row.get("local_waiters"))
    model_row_summary = {
        "active": active,
        "queued": queued,
        "local_waiters": local_waiters,
    }
    if active is None or queued is None or local_waiters is None:
        return {
            "status": "failed",
            "reason": "/queue/status returned 200 but active/queued/local_waiters fields were missing or non-numeric.",
            "http_status": status_code,
            "model_row": model_row_summary,
        }
    if active == 0 and queued == 0 and local_waiters == 0:
        return {
            "status": "passed",
            "reason": "queue idle observed after scenario run.",
            "http_status": status_code,
            "model_row": model_row_summary,
        }
    return {
        "status": "pending",
        "reason": f"queue not yet idle (active={active}, queued={queued}, local_waiters={local_waiters}).",
        "http_status": status_code,
        "model_row": model_row_summary,
    }


def _queue_drain_check(
    base_url: str,
    auth_token: str,
    model: str,
    settle_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    poll_interval = max(0.1, float(poll_interval_seconds))
    settle_window = max(0.0, float(settle_seconds))
    deadline = time.monotonic() + settle_window
    attempts = 0

    while True:
        status_code, body_text, body_json = _fetch_queue_status(base_url, auth_token)
        sample = _evaluate_queue_status_sample(status_code, body_text, body_json, model)
        attempts += 1

        if sample["status"] == "passed":
            return {
                "status": "passed",
                "reason": sample["reason"],
                "http_status": sample.get("http_status"),
                "model_row": sample.get("model_row"),
                "attempts": attempts,
                "settle_seconds": settle_window,
                "poll_interval_seconds": poll_interval,
            }
        if sample["status"] == "failed":
            return {
                "status": "failed",
                "reason": sample["reason"],
                "http_status": sample.get("http_status"),
                "model_row": sample.get("model_row"),
                "attempts": attempts,
                "settle_seconds": settle_window,
                "poll_interval_seconds": poll_interval,
            }
        if time.monotonic() >= deadline:
            terminal_status = "skipped" if sample["status"] == "skipped" else "failed"
            terminal_reason = (
                sample["reason"]
                if terminal_status == "skipped"
                else (
                    "Queue did not converge to idle before settle deadline. "
                    f"Last sample: {sample['reason']}"
                )
            )
            return {
                "status": terminal_status,
                "reason": terminal_reason,
                "http_status": sample.get("http_status"),
                "model_row": sample.get("model_row"),
                "attempts": attempts,
                "settle_seconds": settle_window,
                "poll_interval_seconds": poll_interval,
            }
        time.sleep(poll_interval)


def _percentile(values: list[float], percentile: float) -> float | None:
    if len(values) == 0:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return round(ordered[lower], 3)
    lower_value = ordered[lower]
    upper_value = ordered[upper]
    interpolated = lower_value + (upper_value - lower_value) * (rank - lower)
    return round(interpolated, 3)


def _status_count(
    status_counts: dict[str, int],
    other_status_counts: dict[str, int],
    status_code: int,
) -> int:
    key = str(status_code)
    if key in status_counts:
        return status_counts[key]
    return other_status_counts.get(key, 0)


def _validate_scenario_expectations(
    scenario_name: str,
    scenario_cfg: dict[str, Any],
    status_counts: dict[str, int],
    other_status_counts: dict[str, int],
    transport_errors: int,
    allow_queue_drain_skip: bool,
    queue_drain_check: dict[str, Any] | None = None,
) -> tuple[bool, str | None, str | None]:
    expects_pressure_behavior = scenario_cfg.get("expect_bounded_failure") or scenario_cfg.get(
        "expect_timeout"
    )
    if expects_pressure_behavior and transport_errors > 0 and not scenario_cfg.get("allow_transport_errors", False):
        return (
            False,
            f"Scenario '{scenario_name}' observed transport_errors={transport_errors}; expected 0 to prove proxy crash-safety.",
            None,
        )
    if scenario_cfg.get("expect_bounded_failure"):
        statuses = scenario_cfg.get("bounded_failure_statuses") or [503]
        bounded_failure_count = sum(
            _status_count(status_counts, other_status_counts, int(status_code))
            for status_code in statuses
        )
        if bounded_failure_count < 1:
            status_list = ",".join(str(x) for x in statuses)
            return (
                False,
                f"Scenario '{scenario_name}' expected at least one bounded failure status in [{status_list}].",
                None,
            )
    if scenario_cfg.get("expect_timeout"):
        timeout_count = _status_count(status_counts, other_status_counts, 504)
        if timeout_count < 1:
            return (
                False,
                f"Scenario '{scenario_name}' expected at least one 504 timeout. "
                "Precondition: intentionally slow upstream or an aggressive client timeout budget (for example --timeout-seconds 0.001).",
                None,
            )
    if scenario_cfg.get("verify_queue_drain"):
        if queue_drain_check is None:
            return (
                False,
                f"Scenario '{scenario_name}' requires queue drain verification but no check was recorded.",
                None,
            )
        queue_status = queue_drain_check.get("status")
        if queue_status == "failed":
            return (
                False,
                f"Scenario '{scenario_name}' queue drain check failed: {queue_drain_check.get('reason')}",
                None,
            )
        if queue_status == "skipped":
            if not allow_queue_drain_skip:
                return (
                    False,
                    f"Scenario '{scenario_name}' queue drain verification was skipped: {queue_drain_check.get('reason')} "
                    "Use --allow-queue-drain-skip (or AUTOQ_ALLOW_QUEUE_DRAIN_SKIP=1) only for degraded environments.",
                    None,
                )
            return (
                True,
                None,
                f"Scenario '{scenario_name}' passed in degraded mode because queue-drain verification was skipped.",
            )
    return True, None, None


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
        if _is_timeout_reason(exc.reason):
            status_code = 504
            error_type = "TimeoutError"
        else:
            error_type = "URLError"
        body_text = f"{type(exc.reason).__name__}: {exc.reason}"
    except TimeoutError as exc:
        status_code = 504
        error_type = "TimeoutError"
        body_text = str(exc)
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
    timeout_seconds = (
        float(args.timeout_seconds)
        if args.timeout_seconds is not None
        else float(scenario_cfg.get("timeout_seconds", 60.0))
    )
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
                "timeout_seconds": timeout_seconds,
                "allow_queue_drain_skip": args.allow_queue_drain_skip,
                "queue_drain_settle_seconds": args.queue_drain_settle_seconds,
                "queue_drain_poll_interval_seconds": args.queue_drain_poll_interval_seconds,
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
                timeout_seconds,
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

    bounded_failure_count = _status_count(status_counts, other_status_counts, 429) + _status_count(
        status_counts, other_status_counts, 503
    )
    queue_drain_check = None
    if scenario_cfg.get("verify_queue_drain"):
        queue_drain_check = _queue_drain_check(
            args.base_url,
            args.auth_token,
            args.model,
            args.queue_drain_settle_seconds,
            args.queue_drain_poll_interval_seconds,
        )

    summary = {
        "event": "run_summary",
        "scenario": args.scenario,
        "requests": request_count,
        "concurrency": concurrency,
        "endpoint": endpoint,
        "model": args.model,
        "timeout_seconds": timeout_seconds,
        "status_counts": status_counts,
        "other_status_counts": other_status_counts,
        "bounded_failure_count": bounded_failure_count,
        "queue_full_count": bounded_failure_count,  # Backwards-compatible alias; prefer bounded_failure_count.
        "timeout_count": _status_count(status_counts, other_status_counts, 504),
        "transport_errors": transport_errors,
        "success_200": status_counts["200"],
        "latency_p50_ms": _percentile([float(r["latency_ms"]) for r in records], 0.50),
        "latency_p90_ms": _percentile([float(r["latency_ms"]) for r in records], 0.90),
        "latency_p95_ms": _percentile([float(r["latency_ms"]) for r in records], 0.95),
        "duration_ms": round((time.time() - run_start) * 1000.0, 3),
    }
    if queue_drain_check is not None:
        summary["queue_drain_check"] = queue_drain_check

    summary["expectations_scope"] = "full"
    if scenario_cfg.get("verify_queue_drain") and isinstance(queue_drain_check, dict):
        queue_status = queue_drain_check.get("status")
        if queue_status == "skipped":
            summary["expectations_scope"] = "partial" if args.allow_queue_drain_skip else "not_met"
        elif queue_status == "failed":
            summary["expectations_scope"] = "not_met"
    summary["degraded_mode"] = summary["expectations_scope"] == "partial"

    expectations_ok, expectation_error, expectation_warning = _validate_scenario_expectations(
        scenario_name=args.scenario,
        scenario_cfg=scenario_cfg,
        status_counts=status_counts,
        other_status_counts=other_status_counts,
        transport_errors=transport_errors,
        allow_queue_drain_skip=args.allow_queue_drain_skip,
        queue_drain_check=queue_drain_check,
    )
    summary["expectations_ok"] = expectations_ok
    if expectation_error is not None:
        summary["expectation_error"] = expectation_error
    if expectation_warning is not None:
        summary["expectation_warning"] = expectation_warning

    print(json.dumps(summary))
    if not expectations_ok:
        print(expectation_error, file=sys.stderr)
        return 7
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
