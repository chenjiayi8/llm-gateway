import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
sys.path.insert(0, REPO_ROOT)
_loaded_litellm = sys.modules.get("litellm")
_loaded_litellm_path = getattr(_loaded_litellm, "__file__", None)
_needs_reload = (
    _loaded_litellm_path is not None
    and os.path.commonpath([REPO_ROOT, os.path.abspath(_loaded_litellm_path)])
    != REPO_ROOT
)
if _needs_reload:
    for _name in list(sys.modules):
        if _name == "litellm" or _name.startswith("litellm."):
            sys.modules.pop(_name, None)


def _build_queue_status_test_harness(monkeypatch):
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.testclient import TestClient

    from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
    from litellm.proxy.spend_tracking import spend_management_endpoints

    class FakeRedis:
        async def scan_iter(self, match=None):
            yield b"autoq:limit:gpt-4"

        async def aclose(self):
            return None

    class FakeAQR:
        redis = FakeRedis()

        async def get_model_info(self, model):
            assert model == "gpt-4"
            return {"active": 1, "limit": 2, "queued": 0, "ceiling": 5}

    monkeypatch.setattr(
        spend_management_endpoints,
        "get_auto_queue_status_aqr",
        lambda: FakeAQR(),
    )

    app = FastAPI()
    app.include_router(spend_management_endpoints.router)
    admin_auth = UserAPIKeyAuth(
        user_role=LitellmUserRoles.PROXY_ADMIN,
        user_id="admin-user",
    )

    async def _require_auth(request: Request):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing auth token")
        return admin_auth

    with TestClient(app) as client:
        app.dependency_overrides[spend_management_endpoints.user_api_key_auth] = (
            _require_auth
        )
        unauthorized_response = client.get("/queue/status")
        authorized_response = client.get(
            "/queue/status",
            headers={"Authorization": "Bearer sk-test"},
        )

    return {
        "unauthorized_response": unauthorized_response,
        "authorized_response": authorized_response,
    }


async def _run_bounded_overload_scenario(
    *,
    make_middleware_app,
    asgi_client_factory,
):
    from starlette.responses import JSONResponse

    from litellm.proxy.middleware.auto_queue_scripts import AdmitDecision, ReleaseTransfer
    from litellm.proxy.middleware.auto_queue_state import AutoQueueRequestState

    allow_active_request_to_finish = asyncio.Event()
    active_request_started = asyncio.Event()

    class FakeAQR:
        def __init__(self):
            self.calls = 0

        async def admit_or_enqueue(self, model, request_id, priority, deadline_at_ms, worker_id):
            self.calls += 1
            if self.calls == 1:
                return AdmitDecision(
                    decision="admit_now",
                    claim_token="claim-1",
                    request_state=AutoQueueRequestState(
                        request_id=request_id,
                        model=model,
                        priority=priority,
                        state="active",
                        enqueued_at_ms=deadline_at_ms - 10,
                        deadline_at_ms=deadline_at_ms,
                        worker_id=worker_id,
                        claim_token="claim-1",
                        claimed_at_ms=deadline_at_ms - 5,
                        started_at_ms=deadline_at_ms - 5,
                    ),
                )
            return AdmitDecision(
                decision="queue_full",
                request_state=AutoQueueRequestState(
                    request_id=request_id,
                    model=model,
                    priority=priority,
                    state="queued",
                    enqueued_at_ms=deadline_at_ms - 1,
                    deadline_at_ms=deadline_at_ms,
                    worker_id=worker_id,
                ),
            )

        async def release_and_claim_next(self, model, request_id, **kwargs):
            return ReleaseTransfer(claimed_request_id=None, claim_token=None)

        async def on_success(self, model):
            return None

        async def on_429(self, model):
            return None

    aqr = FakeAQR()

    async def slow_handler(request):
        await request.body()
        active_request_started.set()
        await allow_active_request_to_finish.wait()
        return JSONResponse({"ok": True})

    app = make_middleware_app(slow_handler, aqr=aqr, enabled=True, max_queue_depth=1)
    client = await asgi_client_factory(app)

    admitted_task = asyncio.create_task(
        client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": []},
        )
    )
    await active_request_started.wait()

    overloaded_responses = await asyncio.gather(
        client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": []},
        ),
        client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": []},
        ),
    )
    allow_active_request_to_finish.set()
    admitted_response = await admitted_task

    return {
        "admitted_status": admitted_response.status_code,
        "overloaded_statuses": [
            overloaded_responses[0].status_code,
            overloaded_responses[1].status_code,
        ],
    }


async def _capture_spend_log_autoq_metadata(
    *,
    make_middleware_app,
    asgi_client_factory,
):
    from starlette.responses import JSONResponse

    from litellm.proxy.middleware.auto_queue_scripts import AdmitDecision, ReleaseTransfer
    from litellm.proxy.middleware.auto_queue_state import AutoQueueRequestState
    from litellm.proxy.spend_tracking.spend_tracking_utils import get_logging_payload

    class FakeAQR:
        async def admit_or_enqueue(self, model, request_id, priority, deadline_at_ms, worker_id):
            return AdmitDecision(
                decision="admit_now",
                claim_token="claim-metadata",
                request_state=AutoQueueRequestState(
                    request_id=request_id,
                    model=model,
                    priority=priority,
                    state="active",
                    enqueued_at_ms=deadline_at_ms - 10,
                    deadline_at_ms=deadline_at_ms,
                    worker_id=worker_id,
                    claim_token="claim-metadata",
                    claimed_at_ms=deadline_at_ms - 5,
                    started_at_ms=deadline_at_ms - 5,
                ),
            )

        async def release_and_claim_next(self, model, request_id, **kwargs):
            return ReleaseTransfer(claimed_request_id=None, claim_token=None)

        async def on_success(self, model):
            return None

        async def on_429(self, model):
            return None

    captured_autoq_metadata = None
    aqr = FakeAQR()

    async def handler(request):
        nonlocal captured_autoq_metadata
        await request.body()
        captured_autoq_metadata = getattr(request.state, "autoq_metadata", None)
        return JSONResponse({"ok": True})

    app = make_middleware_app(handler, aqr=aqr, enabled=True)
    client = await asgi_client_factory(app)
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4", "messages": []},
    )

    assert response.status_code == 200
    assert isinstance(captured_autoq_metadata, dict)

    payload = get_logging_payload(
        kwargs={
            "model": "gpt-4",
            "call_type": "acompletion",
            "litellm_params": {
                "metadata": {
                    "status": "success",
                    "user_api_key": "sk-test",
                },
                "proxy_server_request": {
                    "body": {
                        "autoq_metadata": captured_autoq_metadata,
                    }
                },
            },
        },
        response_obj={
            "id": "chatcmpl-test-autoq",
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        },
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    payload_metadata = json.loads(payload["metadata"])

    return payload_metadata["autoq"]


def test_auto_queue_status_endpoint_requires_auth_and_returns_models(monkeypatch):
    harness = _build_queue_status_test_harness(monkeypatch)
    unauthorized_response = harness["unauthorized_response"]
    authorized_response = harness["authorized_response"]

    assert unauthorized_response.status_code == 401
    assert authorized_response.status_code == 200
    assert authorized_response.json() == {
        "models": {
            "gpt-4": {
                "active": 1,
                "limit": 2,
                "queued": 0,
                "ceiling": 5,
                "local_waiters": 0,
            }
        }
    }


@pytest.mark.asyncio
async def test_auto_queue_bounded_overload_returns_expected_status_mix(
    make_middleware_app,
    asgi_client_factory,
):
    status_summary = await _run_bounded_overload_scenario(
        make_middleware_app=make_middleware_app,
        asgi_client_factory=asgi_client_factory,
    )

    assert status_summary["admitted_status"] == 200
    assert status_summary["overloaded_statuses"] == [503, 503]


@pytest.mark.asyncio
async def test_auto_queue_metadata_is_preserved_in_spend_logs(
    make_middleware_app,
    asgi_client_factory,
):
    autoq = await _capture_spend_log_autoq_metadata(
        make_middleware_app=make_middleware_app,
        asgi_client_factory=asgi_client_factory,
    )

    assert isinstance(autoq, dict)
    assert autoq["summary"]["model"] == "gpt-4"
    assert autoq["summary"]["decision"] == "admit_now"
    assert any(event["event"] == "forwarded" for event in autoq["events"])
