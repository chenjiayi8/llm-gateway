#!/usr/bin/env python3
"""Collect spend-log evidence for auto-queue runs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect spend log evidence")
    parser.add_argument("--model", required=True, help="Target model for spend-log filtering")
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
    parser.add_argument("--start-epoch", type=float, required=True)
    parser.add_argument("--end-epoch", type=float)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--max-polls", type=int, default=6)
    parser.add_argument("--poll-interval-seconds", type=float, default=5.0)
    parser.add_argument("--output-file", default="/tmp/task3_spend_log_evidence.json")
    parser.add_argument(
        "--queued-expected",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fail when no matching rows or metadata.autoq rows are found (default: true)",
    )
    return parser.parse_args()


def _to_query_datetime(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_row_epoch(row: dict[str, Any]) -> float | None:
    value = row.get("startTime")
    if not isinstance(value, str):
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def _normalize_metadata(raw: Any) -> Any:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return raw
        return parsed
    return raw


def _fetch_spend_logs_page(
    base_url: str,
    auth_token: str,
    start_epoch: float,
    end_epoch: float,
    page: int,
    page_size: int,
) -> tuple[int | None, Any]:
    query = {
        "start_date": _to_query_datetime(start_epoch),
        "end_date": _to_query_datetime(end_epoch),
        "page": str(page),
        "page_size": str(page_size),
        "sort_by": "startTime",
        "sort_order": "asc",
    }
    url = f"{base_url.rstrip('/')}/spend/logs/v2?{parse.urlencode(query)}"
    req = request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        },
    )

    try:
        with request.urlopen(req, timeout=60) as resp:
            code = resp.status
            body = resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        code = exc.code
        body = exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover - defensive runtime path
        return None, {"error": f"{type(exc).__name__}: {exc}"}

    try:
        payload = json.loads(body)
    except Exception:
        payload = {"raw": body}
    return code, payload


def _matches_target_model(row: dict[str, Any], target_model: str) -> bool:
    target = target_model.strip().lower()
    if not target:
        return False
    candidates = [
        str(row.get("model") or "").strip().lower(),
        str(row.get("model_group") or "").strip().lower(),
        str(row.get("model_id") or "").strip().lower(),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if candidate == target or target in candidate or candidate in target:
            return True
    return False


def main() -> int:
    args = parse_args()
    if not args.auth_token:
        print(
            "Missing auth token. Set --auth-token or TASK1_AUTH_TOKEN/TASK1_CANONICAL_AUTH_TOKEN.",
            file=sys.stderr,
        )
        return 2

    end_epoch = args.end_epoch or time.time()
    if end_epoch < args.start_epoch:
        print("end-epoch must be >= start-epoch", file=sys.stderr)
        return 2

    all_rows: list[dict[str, Any]] = []
    fetch_attempts: list[dict[str, Any]] = []

    for poll_idx in range(max(1, args.max_polls)):
        poll_rows: list[dict[str, Any]] = []
        total_pages_seen = 0

        for page in range(1, args.max_pages + 1):
            code, payload = _fetch_spend_logs_page(
                base_url=args.base_url,
                auth_token=args.auth_token,
                start_epoch=args.start_epoch,
                end_epoch=end_epoch,
                page=page,
                page_size=args.page_size,
            )
            if code != 200:
                print(
                    f"Failed to fetch spend logs page={page} status={code} payload={payload}",
                    file=sys.stderr,
                )
                return 3
            if not isinstance(payload, dict):
                print(f"Unexpected spend logs response payload: {payload}", file=sys.stderr)
                return 3

            page_rows = payload.get("data")
            total_pages = int(payload.get("total_pages", 1))
            total_pages_seen = max(total_pages_seen, total_pages)
            if not isinstance(page_rows, list):
                print(f"Unexpected spend logs data format: {payload}", file=sys.stderr)
                return 3

            for row in page_rows:
                if isinstance(row, dict):
                    poll_rows.append(row)

            if page >= total_pages or not page_rows:
                break

        filtered_rows: list[dict[str, Any]] = []
        autoq_rows: list[dict[str, Any]] = []
        for row in poll_rows:
            row_epoch = _parse_row_epoch(row)
            if row_epoch is None:
                continue
            if row_epoch < args.start_epoch or row_epoch > end_epoch:
                continue

            if not _matches_target_model(row, args.model):
                continue

            row_copy = dict(row)
            metadata = _normalize_metadata(row_copy.get("metadata"))
            row_copy["metadata"] = metadata
            filtered_rows.append(row_copy)

            if isinstance(metadata, dict) and isinstance(metadata.get("autoq"), dict):
                autoq_rows.append(
                    {
                        "request_id": row_copy.get("request_id"),
                        "status": row_copy.get("status"),
                        "startTime": row_copy.get("startTime"),
                        "autoq": metadata.get("autoq"),
                    }
                )

        fetch_attempts.append(
            {
                "poll_index": poll_idx + 1,
                "rows_fetched": len(poll_rows),
                "rows_filtered": len(filtered_rows),
                "autoq_rows": len(autoq_rows),
                "total_pages_seen": total_pages_seen,
            }
        )

        all_rows = filtered_rows
        if not args.queued_expected:
            break
        if filtered_rows and autoq_rows:
            break
        if poll_idx + 1 < args.max_polls:
            time.sleep(max(0.0, args.poll_interval_seconds))

    autoq_rows: list[dict[str, Any]] = []
    for row in all_rows:
        metadata = row.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("autoq"), dict):
            autoq_rows.append(
                {
                    "request_id": row.get("request_id"),
                    "status": row.get("status"),
                    "startTime": row.get("startTime"),
                    "autoq": metadata.get("autoq"),
                }
            )

    output = {
        "config": {
            "base_url": args.base_url,
            "model": args.model,
            "start_epoch": args.start_epoch,
            "end_epoch": end_epoch,
            "page_size": args.page_size,
            "max_pages": args.max_pages,
            "max_polls": args.max_polls,
            "poll_interval_seconds": args.poll_interval_seconds,
            "queued_expected": args.queued_expected,
        },
        "summary": {
            "rows_filtered": len(all_rows),
            "autoq_rows": len(autoq_rows),
            "fetch_attempts": fetch_attempts,
        },
        "autoq_rows": autoq_rows,
    }

    out_path = Path(args.output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")

    print(json.dumps(output["summary"]))
    print(f"wrote_spend_evidence={out_path}")
    for row in autoq_rows:
        print(json.dumps({"event": "autoq_metadata", **row}))

    if args.queued_expected and len(all_rows) == 0:
        print(
            f"No spend logs matched time window/model (model={args.model}) while queued behavior was expected.",
            file=sys.stderr,
        )
        return 4
    if args.queued_expected and len(autoq_rows) == 0:
        print(
            "No matching spend logs contained metadata.autoq (queue behavior expected).",
            file=sys.stderr,
        )
        return 5

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
