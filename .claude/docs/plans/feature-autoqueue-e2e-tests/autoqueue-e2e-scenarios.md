# Auto-Queue E2E Scenario Matrix

Task 1 freezes the request contract and environment assumptions before any harness code is added.

## Canonical Request (Frozen)

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TASK1_CANONICAL_AUTH_TOKEN}" \
  -d '{
     "model": "glm-5.1",
     "messages": [{"role": "user", "content": "Hello, who are you!"}]
   }'
```

Canonical request freezes request shape (method, path, payload, and headers). The frozen shape originated from the `localhost:4000` request path; Task 1 validation runtime target is profile-driven via `$TASK1_BASE_URL`. Token value is supplied from `TASK1_CANONICAL_AUTH_TOKEN`.

## Security and Reproducibility Note

- Never commit real bearer tokens into Task 1 docs or logs.
- Use environment variables for all auth values.

## Active Validation Profile (Single Source of Truth)

```bash
export TASK1_ACTIVE_VALIDATION_PROFILE="controlled-runtime-4001"
export TASK1_BASE_URL="http://localhost:4001"
export TASK1_AUTH_TOKEN="${TASK1_AUTH_TOKEN:?set TASK1_AUTH_TOKEN}"
```

| Active profile variable value | Base URL / Port | Auth token source | Purpose |
| --- | --- | --- | --- |
| `controlled-runtime-4001` | `http://localhost:4001` | `TASK1_AUTH_TOKEN` | Active runtime for reproducible Task 1 queue-status validation and baseline reruns |

All validation commands below derive from the active profile variables above.

```bash
curl "$TASK1_BASE_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TASK1_AUTH_TOKEN" \
  -d '{
     "model": "glm-5.1",
     "messages": [{"role": "user", "content": "Hello, who are you!"}]
   }'

curl "$TASK1_BASE_URL/queue/status" \
  -H "Authorization: Bearer $TASK1_AUTH_TOKEN" \
  -H "Content-Type: application/json"
```

## Environment Contract (Required Assumptions)

- proxy listening on localhost:4000
- canonical request shape originated from the localhost:4000 request path
- for reproducible operator reruns, an additional controlled validation runtime is selected via `$TASK1_BASE_URL` from the active validation profile
- AUTOQ_ENABLED=true
- Redis reachable by proxy
- auth key valid for /v1/chat/completions and /queue/status
- model path capable of successful responses under low load

## Task 1 Evidence References (Brief)

- Requirement 3 baseline success evidence reference: commit `ef22dea798` (`HTTP 200` with valid completion body).
- Requirement 4 queue-status contract evidence reference: progress timeline entry `2026-04-08 11:08` ("Sanitized Manual Evidence: `/queue/status` HTTP 200", including `active`, `queued`, `limit`, `ceiling`, `local_waiters`).
- Current rerun blocker evidence reference: progress timeline entries `2026-04-08 11:28` and `2026-04-08 11:31`.

## First-Pass Scenario Table

| Scenario name | Request count / concurrency | Expected status code mix | Expected `/queue/status` behavior | Expected spend-log evidence |
| --- | --- | --- | --- | --- |
| baseline single request | 1 request / concurrency 1 | 1x `200` | Endpoint responds `200`; if `glm-5.1` row exists, queue fields present and settle at `active=0`, `queued=0`, `local_waiters=0` after completion | One completion spend event for `glm-5.1` with non-empty request identifier, status `200`, latency metadata, and cost/token metadata populated |
| 2 concurrent requests | 2 requests / concurrency 2 | 2x `200` | Endpoint responds `200`; transient non-zero `active` allowed during run, then drains to `active=0`, `queued=0` | Two spend events tied to the two requests; model `glm-5.1`; statuses `200`; timestamps in same run window |
| 5 concurrent requests | 5 requests / concurrency 5 | 5x `200` under normal local load | Endpoint responds `200`; `active` rises during run; `queued` may briefly rise above 0 but drains to 0 post-run | Five spend events for `glm-5.1`; each has completion metadata and consistent auth key attribution |
| 10 concurrent requests | 10 requests / concurrency 10 | Predominantly `200`; no unexpected `5xx`; bounded throttling (`429`) is acceptable only if queue ceilings are intentionally tight | Endpoint responds `200`; visible queue pressure (`queued` and/or `local_waiters` above 0) allowed during run, then drains | Ten terminal spend records (success or explicit throttled outcome) with per-request status and latency/cost fields |
| queue-depth overflow run | 50 requests / concurrency 50 (`overflow`) | Bounded overload failures are required (`503` preferred; `429` acceptable). `200` responses are allowed but not required under aggressive overload. | Endpoint responds `200`; during pressure, `queued` and/or `local_waiters` rises; after load stops, queue drains to 0 | Spend logs show accepted requests and bounded failures with timestamps around overload window |
| timeout run with intentionally slow upstream | 20 requests / concurrency 20 (`timeout`, `expect_timeout=true`) | At least one timeout-class failure (`504`) is required when intentionally slow conditions are active. `200` responses are allowed but not required under aggressive timeout budgets. | Endpoint responds `200`; `active` may stay elevated longer; queue should still drain after timeout horizon | Spend logs capture timeout/error status metadata plus latency values demonstrating slow-upstream behavior |
| post-run queue drain verification | 0 completion requests / concurrency 0 (status polling only) | `200` from status polls | Repeated polls return `200`; `active=0`, `queued=0`, `local_waiters=0`; row includes queue state fields (`active`, `queued`, `limit`, `ceiling`, `local_waiters`) when `glm-5.1` is present | No new completion spend entries should appear after final scenario window closes |
| spend-log metadata verification | 0 completion requests / concurrency 0 (artifact audit only) | N/A for completion API; log retrieval path should be successful | Queue status unchanged from final drained state | Validate required metadata fields across recorded runs: model, request id, auth/key attribution, status code, latency, and spend/cost tokens |

## Timeout Precondition (Task 4)

- Timeout assertions require an intentionally slow runtime path.
- For local proof runs, use one of:
  - intentionally slow upstream model/runtime configuration, or
  - an aggressive client timeout budget (for example, low `--timeout-seconds`) to force timeout behavior.
- Without this precondition, timeout scenario assertions are expected to fail by design.

## Task 4 Assertion Contract (Runner)

- For `overflow` and `timeout`, assertion checks fail when `transport_errors > 0` unless an explicit per-scenario override allows transport errors.
- This is the crash-safety guard for requirement alignment: overload/timeout may fail requests in bounded ways, but the proxy process must not crash.
- Runner summary uses `bounded_failure_count` for `429+503`; `queue_full_count` remains as a backwards-compatible alias.
