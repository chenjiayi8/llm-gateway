"""
Tests for the standalone proxy Prometheus metrics middleware.
"""

import importlib
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

import litellm_proxy_extras.metrics as proxy_metrics


@pytest.fixture
def metrics_module(monkeypatch):
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    return importlib.reload(proxy_metrics)


def _make_app(metrics_module):
    app = FastAPI()

    @app.get("/ok")
    async def ok():
        return JSONResponse({"ok": True})

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    metrics_module.install_metrics(app)
    return app


def test_is_not_base_http_middleware(metrics_module):
    assert not issubclass(metrics_module.PrometheusHTTPMiddleware, BaseHTTPMiddleware)


def test_has_asgi_call_protocol(metrics_module):
    assert "__call__" in metrics_module.PrometheusHTTPMiddleware.__dict__


def test_unmatched_routes_are_bucketed_and_metrics_route_is_excluded(metrics_module):
    client = TestClient(_make_app(metrics_module), raise_server_exceptions=False)

    assert client.get("/ok").status_code == 200
    assert client.get("/missing-one").status_code == 404
    assert client.get("/missing-two").status_code == 404

    metrics_text = client.get("/metrics").text

    assert 'route="/ok"' in metrics_text
    assert f'route="{metrics_module.UNMATCHED_ROUTE}"' in metrics_text
    assert 'route="/missing-one"' not in metrics_text
    assert 'route="/missing-two"' not in metrics_text
    assert 'route="/metrics"' not in metrics_text


def test_server_errors_are_counted(metrics_module):
    client = TestClient(_make_app(metrics_module), raise_server_exceptions=False)

    response = client.get("/boom")

    assert response.status_code == 500

    metrics_text = client.get("/metrics").text
    assert "app_http_errors_total" in metrics_text
    assert 'route="/boom"' in metrics_text
    assert 'status_code="500"' in metrics_text


def test_uses_multiprocess_registry_for_export(monkeypatch, tmp_path):
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    metrics_module = importlib.reload(proxy_metrics)

    with patch("prometheus_client.multiprocess.MultiProcessCollector") as collector:
        registry = metrics_module._metrics_registry_for_export()

    collector.assert_called_once_with(registry)
