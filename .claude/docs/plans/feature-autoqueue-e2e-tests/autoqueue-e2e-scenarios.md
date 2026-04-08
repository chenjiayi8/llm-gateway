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
| queue-depth overflow run | 50 requests / concurrency 50 (`overflow`) | Bounded overload failures are required (`503` preferred; `429` acceptable). `200` responses are allowed but not required under aggressive overload. | Strict mode: queue-drain proof is required (`queue_drain_check=passed`). Degraded mode: if status route is unavailable, run may continue only with `--allow-queue-drain-skip` and is marked partial. | Spend logs show accepted requests and bounded failures with timestamps around overload window |
| timeout run with intentionally slow upstream | 20 requests / concurrency 20 (`timeout`, `expect_timeout=true`, default runner timeout budget `0.001s`) | At least one timeout-class failure (`504`) is required when intentionally slow conditions are active. `200` responses are allowed but not required under aggressive timeout budgets. | Strict mode: queue-drain proof is required (`queue_drain_check=passed`). Degraded mode: if status route is unavailable, run may continue only with `--allow-queue-drain-skip` and is marked partial. | Spend logs capture timeout/error status metadata plus latency values demonstrating slow-upstream behavior |
| post-run queue drain verification | 0 completion requests / concurrency 0 (status polling only) | `200` from status polls | Repeated polls return `200`; `active=0`, `queued=0`, `local_waiters=0`; row includes queue state fields (`active`, `queued`, `limit`, `ceiling`, `local_waiters`) when `glm-5.1` is present | No new completion spend entries should appear after final scenario window closes |
| spend-log metadata verification | 0 completion requests / concurrency 0 (artifact audit only) | N/A for completion API; log retrieval path should be successful | Queue status unchanged from final drained state | Validate required metadata fields across recorded runs: model, request id, auth/key attribution, status code, latency, and spend/cost tokens |

## Timeout Precondition (Task 4)

- Timeout assertions require an intentionally slow runtime path.
- For local proof runs, use one of:
  - intentionally slow upstream model/runtime configuration, or
  - an aggressive client timeout budget to force timeout behavior.
- Runner default for `--scenario timeout` is `0.001` seconds when `--timeout-seconds` is omitted (aggressive local-proof budget to force timeout-class behavior).
- Without this precondition, timeout scenario assertions are expected to fail by design.

## Task 4 Assertion Contract (Runner)

- For `overflow` and `timeout`, assertion checks fail when `transport_errors > 0` unless an explicit per-scenario override allows transport errors.
- This is the crash-safety guard for requirement alignment: overload/timeout may fail requests in bounded ways, but the proxy process must not crash.
- Runner summary uses `bounded_failure_count` for `429+503`; `queue_full_count` remains as a backwards-compatible alias.
- Runner now emits `queue_drain_check` for pressure scenarios (`overflow`/`timeout`) with deterministic boundary states:
  - `passed`: `/queue/status` is available and target model drains to `active=0`, `queued=0`, `local_waiters=0`.
  - `failed`: auth/permission errors (`401`/`403`), unexpected non-`200` statuses, malformed `200` response schema, or queue failing to converge to idle before settle deadline.
  - `skipped`: endpoint-unavailable/degraded conditions where proof is impossible (for example `404`, `503`, or connection-level failures with no HTTP response).
- Default behavior (strict): pressure-scenario expectations fail when `queue_drain_check.status` is `failed` **or** `skipped`.
- Queue-drain uses bounded polling, not a single sample:
  - settle window: `--queue-drain-settle-seconds` (env `AUTOQ_QUEUE_DRAIN_SETTLE_SECONDS`, default `15`)
  - poll interval: `--queue-drain-poll-interval-seconds` (env `AUTOQ_QUEUE_DRAIN_POLL_INTERVAL_SECONDS`, default `1`)
- Degraded-environment opt-out: pass `--allow-queue-drain-skip` (or set `AUTOQ_ALLOW_QUEUE_DRAIN_SKIP=1`) to allow `skipped` queue-drain checks; pass `--no-allow-queue-drain-skip` to force strict mode even when env default is enabled.
- When degraded opt-out is used and queue-drain is skipped, runner marks summary as partial (`expectations_scope=partial`, `degraded_mode=true`) so the run is never reported as a full pass.
- Full pass requires `expectations_ok=true` and `expectations_scope=full` (which implies queue-drain `passed` for pressure scenarios).

## Final Operator Checklist (Task 6)

Use env-sourced auth and runtime values only:

```bash
export TASK1_BASE_URL="${TASK1_BASE_URL:-http://localhost:4000}"
export TASK1_AUTH_TOKEN="${TASK1_AUTH_TOKEN:?set TASK1_AUTH_TOKEN}"
export TASK2_MODEL="${TASK2_MODEL:-glm-5}"
```

### Required commands

```bash
# 1) Baseline request command (frozen request shape)
curl "$TASK1_BASE_URL/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TASK1_AUTH_TOKEN" \
  -d '{
    "model": "'"$TASK2_MODEL"'",
    "messages": [{"role": "user", "content": "Hello, who are you!"}]
  }'

# 2) Queue status command
curl "$TASK1_BASE_URL/queue/status" \
  -H "Authorization: Bearer $TASK1_AUTH_TOKEN" \
  -H "Content-Type: application/json"

# 3) Burst run command
poetry run python .claude/docs/plans/feature-autoqueue-e2e-tests/run_autoqueue_e2e.py \
  --scenario burst-10

# 4) Overflow run command
poetry run python .claude/docs/plans/feature-autoqueue-e2e-tests/run_autoqueue_e2e.py \
  --scenario overflow

# 5) Timeout run command
poetry run python .claude/docs/plans/feature-autoqueue-e2e-tests/run_autoqueue_e2e.py \
  --scenario timeout

# 6) Spend-log evidence command
poetry run python .claude/docs/plans/feature-autoqueue-e2e-tests/collect_spend_log_evidence.py \
  --model "$TASK2_MODEL" \
  --start-epoch <start_epoch_seconds> \
  --end-epoch <end_epoch_seconds> \
  --output-file /tmp/autoqueue_spend_evidence.json
```

Timeout checklist note: run without `--timeout-seconds` to use the scenario default deterministic timeout budget.

Strict-mode note:
- `overflow`/`timeout` commands require queue-drain proof and return non-zero when `/queue/status` is unavailable/degraded.
- For degraded-environment evidence collection only, rerun with `AUTOQ_ALLOW_QUEUE_DRAIN_SKIP=1` and treat outcomes as partial (`expectations_scope=partial`), never full pass.

### Pass/Fail rubric (release gates)

| Gate | Pass condition | Fail condition |
| --- | --- | --- |
| no proxy crash | each scenario emits JSON `run_summary` and reports `transport_errors=0` | no `run_summary` is emitted due runtime failure, or `transport_errors>0` |
| queue drains after each run | post-run queue snapshots show `active=0`, `queued=0`, `local_waiters=0` for target model | queue stays non-idle after post-run polling horizon |
| status endpoint available under load | `/queue/status` responds `HTTP 200` before/during/after load polling window | `/queue/status` returns non-`200` (including `503`/`404`) or times out during load window |
| spend metadata present when queueing occurs | spend evidence includes at least one row with `metadata.autoq` for queued/admitted events in load window | no matching `metadata.autoq` rows when queueing pressure was observed |

### Operational caveat (canonical host)

In this environment, canonical host `http://localhost:4000` currently returns `HTTP 503` for `/queue/status` (`{"error":"Auto-queue unavailable for model queue-status"}`), so queue-drain and status-under-load gates cannot be proven on `:4000` via endpoint evidence. Use the controlled runtime profile (`http://localhost:4001`) for those two gates when the route is exposed there.
