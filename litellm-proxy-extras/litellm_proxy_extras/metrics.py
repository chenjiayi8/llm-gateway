"""Standalone Prometheus HTTP metrics for the llm-gateway proxy.

Same metric names and buckets as the KnowledgeCenter platform so dashboards
work consistently. Depends only on prometheus-client and FastAPI/starlette.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any, Callable

from fastapi import FastAPI, Request, Response
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    make_asgi_app,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Match

HTTP_DURATION_SECONDS_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)
HTTP_SIZE_BYTES_BUCKETS = (
    128, 512, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304,
)

SERVICE_NAME = "llm-gateway"

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


def _normalize_route(app: FastAPI, request: Request) -> str:
    for route in app.router.routes:
        match, _child_scope = route.matches(request.scope)
        if match == Match.FULL:
            return str(getattr(route, "path", request.url.path))
    return request.url.path


def _header_as_int(headers: Any, name: str) -> int:
    value = headers.get(name)
    return int(value) if value and value.isdigit() else 0


class PrometheusHTTPMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, *, fastapi_app: FastAPI) -> None:
        super().__init__(app)
        self._fastapi_app = fastapi_app

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        route = _normalize_route(self._fastapi_app, request)
        if route == "/metrics":
            return await call_next(request)

        method = request.method
        http_labels = {"service": SERVICE_NAME, "method": method, "route": route}

        http_in_flight_requests.labels(**http_labels).inc()
        http_request_size_bytes.labels(**http_labels).observe(
            _header_as_int(request.headers, "content-length")
        )
        start = perf_counter()

        try:
            response = await call_next(request)
        except BaseException:
            status_code = "500"
            labels = {**http_labels, "status_code": status_code}
            http_requests_total.labels(**labels).inc()
            http_request_duration_seconds.labels(**labels).observe(perf_counter() - start)
            http_errors_total.labels(**labels).inc()
            raise
        finally:
            http_in_flight_requests.labels(**http_labels).dec()

        status_code = str(response.status_code)
        labels = {**http_labels, "status_code": status_code}
        http_requests_total.labels(**labels).inc()
        http_request_duration_seconds.labels(**labels).observe(perf_counter() - start)
        http_response_size_bytes.labels(**labels).observe(
            _header_as_int(response.headers, "content-length")
        )
        if response.status_code >= 500:
            http_errors_total.labels(**labels).inc()
        return response


def install_metrics(app: FastAPI) -> None:
    app.add_middleware(PrometheusHTTPMiddleware, fastapi_app=app)
    app.mount("/metrics", make_asgi_app(registry=_registry))
