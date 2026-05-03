"""Standalone Prometheus HTTP metrics for the llm-gateway proxy.

Same metric names and buckets as the KnowledgeCenter platform so dashboards
work consistently. Depends only on prometheus-client and FastAPI/starlette.
"""

from __future__ import annotations

import os
from time import perf_counter
from typing import Iterable

from fastapi import FastAPI
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    make_asgi_app,
)
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HTTP_DURATION_SECONDS_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)
HTTP_SIZE_BYTES_BUCKETS = (
    128, 512, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304,
)

SERVICE_NAME = "llm-gateway"
UNMATCHED_ROUTE = "/__unmatched__"
_CONTENT_LENGTH_HEADER = b"content-length"

_registry = CollectorRegistry(auto_describe=True)

http_requests_total = Counter(
    "app_http_requests_total",
    "Completed HTTP requests",
    ("service", "method", "route", "status_code"),
    registry=_registry,
)
http_request_duration_seconds = Histogram(
    "app_http_request_duration_seconds",
    "HTTP request duration",
    ("service", "method", "route", "status_code"),
    buckets=HTTP_DURATION_SECONDS_BUCKETS,
    registry=_registry,
)
http_request_size_bytes = Histogram(
    "app_http_request_size_bytes",
    "HTTP request size",
    ("service", "method", "route"),
    buckets=HTTP_SIZE_BYTES_BUCKETS,
    registry=_registry,
)
http_response_size_bytes = Histogram(
    "app_http_response_size_bytes",
    "HTTP response size",
    ("service", "method", "route", "status_code"),
    buckets=HTTP_SIZE_BYTES_BUCKETS,
    registry=_registry,
)
http_errors_total = Counter(
    "app_http_errors_total",
    "HTTP error responses",
    ("service", "method", "route", "status_code"),
    registry=_registry,
)
http_in_flight_requests = Gauge(
    "app_http_in_flight_requests",
    "In-flight HTTP requests",
    ("service", "method", "route"),
    registry=_registry,
)


def _normalize_route(app: FastAPI, scope: Scope) -> str:
    for route in app.router.routes:
        match, _child_scope = route.matches(scope)
        if match == Match.FULL:
            return str(getattr(route, "path", scope.get("path", UNMATCHED_ROUTE)))
    return UNMATCHED_ROUTE
def _raw_header_as_int(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> int:
    for key, value in headers:
        if key.lower() != name:
            continue

        decoded_value = value.decode("latin-1")
        return int(decoded_value) if decoded_value.isdigit() else 0

    return 0


def _should_skip_metrics(path: str) -> bool:
    return path == "/metrics" or path.startswith("/metrics/")


def _metrics_registry_for_export() -> CollectorRegistry:
    if "PROMETHEUS_MULTIPROC_DIR" in os.environ:
        from prometheus_client import multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return registry

    return _registry


class PrometheusHTTPMiddleware:
    def __init__(self, app: ASGIApp, *, fastapi_app: FastAPI) -> None:
        self.app = app
        self._fastapi_app = fastapi_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if _should_skip_metrics(path):
            await self.app(scope, receive, send)
            return

        route = _normalize_route(self._fastapi_app, scope)
        method = scope.get("method", "")
        http_labels = {"service": SERVICE_NAME, "method": method, "route": route}

        http_in_flight_requests.labels(**http_labels).inc()
        http_request_size_bytes.labels(**http_labels).observe(
            _raw_header_as_int(scope.get("headers", []), _CONTENT_LENGTH_HEADER)
        )
        start = perf_counter()
        status_code = "500"
        response_size = 0
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_size, response_started, status_code

            if message["type"] == "http.response.start":
                response_started = True
                status_code = str(message["status"])
                response_size = _raw_header_as_int(
                    message.get("headers", []), _CONTENT_LENGTH_HEADER
                )

            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            labels = {
                **http_labels,
                "status_code": status_code if response_started else "500",
            }
            http_requests_total.labels(**labels).inc()
            http_request_duration_seconds.labels(**labels).observe(perf_counter() - start)
            http_errors_total.labels(**labels).inc()
            raise
        finally:
            http_in_flight_requests.labels(**http_labels).dec()

        labels = {**http_labels, "status_code": status_code}
        http_requests_total.labels(**labels).inc()
        http_request_duration_seconds.labels(**labels).observe(perf_counter() - start)
        http_response_size_bytes.labels(**labels).observe(response_size)
        if int(status_code) >= 500:
            http_errors_total.labels(**labels).inc()


def install_metrics(app: FastAPI) -> None:
    app.add_middleware(PrometheusHTTPMiddleware, fastapi_app=app)
    app.mount("/metrics", make_asgi_app(registry=_metrics_registry_for_export()))
