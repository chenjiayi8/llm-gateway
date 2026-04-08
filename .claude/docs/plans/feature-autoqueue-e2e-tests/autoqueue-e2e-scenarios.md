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

## Operator Validation Evidence (Task 1 Step 3/4)

### Runtime A: existing host process on `localhost:4000` (contract mismatch)

- Process evidence:
  - `ps -ef | rg '/usr/bin/litellm --port 4000'`
  - Result: `root ... /usr/bin/python3.13 /usr/bin/litellm --port 4000`
- Endpoint check:
  - `curl http://localhost:4000/queue/status ...`
  - Result: `HTTP 404` with body `{"detail":"Not Found"}`
- Interpretation: the currently running host-managed proxy on `:4000` does not expose `/queue/status`.

### Runtime B: controlled local proxy for reproducible Task 1 validation

- Runtime dependencies used for reproducibility:
  - Redis (local): `docker run -d --name task1-queue-redis -p 6379:6379 redis:7-alpine`
  - Postgres (already running in workspace): `litellm_db` on `localhost:5432`
- Controlled proxy launch (repo code + explicit auto-queue env) on `localhost:4001`:

```bash
AUTOQ_ENABLED=true \
AUTOQ_REDIS_HOST=127.0.0.1 AUTOQ_REDIS_PORT=6379 AUTOQ_REDIS_DB=3 \
REDIS_HOST=127.0.0.1 REDIS_PORT=6379 \
DATABASE_URL='postgresql://llmproxy:dbpassword9090@127.0.0.1:5432/litellm' \
LITELLM_MASTER_KEY='sk-1234' \
poetry run litellm --port 4001
```

- Route exposure proof in controlled runtime:
  - `curl -sS http://localhost:4001/routes | rg -o '"/[^"]*queue[^"]*"' | sort -u`
  - Result includes both `"/queue/chat/completions"` and `"/queue/status"`.
- Manual queue-status request in controlled runtime:
  - `curl http://localhost:4001/queue/status -H "Authorization: Bearer sk-1234"`
  - Result: `HTTP 200` with payload:

```json
{"models":{"glm-5.1":{"active":0,"limit":2,"queued":0,"ceiling":50,"local_waiters":0}}}
```

- Manual canonical chat request in controlled runtime:
  - `curl http://localhost:4001/v1/chat/completions ...`
  - Current result: `HTTP 429` from upstream throttling/limit state for `glm-5.1` in this environment.
- Follow-up queue-status poll after the canonical request:
  - Result remained `HTTP 200` with queue state fields present (example observed snapshot: `active=1`, `queued=0`, `local_waiters=0`), then drained (`active=0`) on the subsequent poll.

- Conclusion: `/queue/status` contract is valid in the intended auto-queue runtime; the prior Task 1 miss was due to hitting a different pre-existing process bound to `:4000`, not an endpoint path mismatch.

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
