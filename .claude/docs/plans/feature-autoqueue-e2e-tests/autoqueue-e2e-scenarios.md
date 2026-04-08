# Auto-Queue E2E Scenario Matrix

Task 1 freezes the request contract and environment assumptions before any harness code is added.

## Canonical Request (Frozen)

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-HpY1curLZDTt0NmpfWfH-g" \
  -d '{
     "model": "glm-5.1",
     "messages": [{"role": "user", "content": "Hello, who are you!"}]
   }'
```

## Environment Contract (Required Assumptions)

- proxy listening on localhost:4000
- AUTOQ_ENABLED=true
- Redis reachable by proxy
- auth key valid for /v1/chat/completions and /queue/status
- model path capable of successful responses under low load

## First-Pass Scenario Table

| Scenario name | Request count / concurrency | Expected status code mix | Expected `/queue/status` behavior | Expected spend-log evidence |
| --- | --- | --- | --- | --- |
| baseline single request | 1 request / concurrency 1 | 1x `200` | Endpoint responds `200`; if `glm-5.1` row exists, queue fields present and settle at `active=0`, `queued=0`, `local_waiters=0` after completion | One completion spend event for `glm-5.1` with non-empty request identifier, status `200`, latency metadata, and cost/token metadata populated |
| 2 concurrent requests | 2 requests / concurrency 2 | 2x `200` | Endpoint responds `200`; transient non-zero `active` allowed during run, then drains to `active=0`, `queued=0` | Two spend events tied to the two requests; model `glm-5.1`; statuses `200`; timestamps in same run window |
| 5 concurrent requests | 5 requests / concurrency 5 | 5x `200` under normal local load | Endpoint responds `200`; `active` rises during run; `queued` may briefly rise above 0 but drains to 0 post-run | Five spend events for `glm-5.1`; each has completion metadata and consistent auth key attribution |
| 10 concurrent requests | 10 requests / concurrency 10 | Predominantly `200`; no unexpected `5xx`; bounded throttling (`429`) is acceptable only if queue ceilings are intentionally tight | Endpoint responds `200`; visible queue pressure (`queued` and/or `local_waiters` above 0) allowed during run, then drains | Ten terminal spend records (success or explicit throttled outcome) with per-request status and latency/cost fields |
| queue-depth overflow run | 50 requests / concurrency 25 | Mix of `200` and overflow/throttle responses (`429` and/or configured rejection code) once queue ceiling is exceeded | Endpoint responds `200`; during pressure, `queued` approaches ceiling and `local_waiters` rises; after load stops, queue trends down to 0 | Spend logs show accepted requests and rejected/throttled attempts with distinct status metadata and timestamps around overload window |
| timeout run with intentionally slow upstream | 10 requests / concurrency 5 (using intentionally slowed upstream path) | Mix of `200` and timeout-class failures (`408`/`504` or gateway timeout equivalent) | Endpoint responds `200`; `active` remains elevated longer; `queued` may accumulate then clears after timeout horizon | Spend logs capture timeout/error status metadata plus latency values demonstrating slow-upstream behavior |
| post-run queue drain verification | 0 completion requests / concurrency 0 (status polling only) | `200` from status polls | Repeated polls return `200`; `active=0`, `queued=0`, `local_waiters=0`; row includes queue state fields (`active`, `queued`, `limit`, `ceiling`, `local_waiters`) when `glm-5.1` is present | No new completion spend entries should appear after final scenario window closes |
| spend-log metadata verification | 0 completion requests / concurrency 0 (artifact audit only) | N/A for completion API; log retrieval path should be successful | Queue status unchanged from final drained state | Validate required metadata fields across recorded runs: model, request id, auth/key attribution, status code, latency, and spend/cost tokens |

