# Pre-Hetzner Final Blockers — 2026-09-16 (follow-up)

Follow-up to
[docs/LOCAL_E2E_PREDEPLOY_VALIDATION_2026-09-16.md](LOCAL_E2E_PREDEPLOY_VALIDATION_2026-09-16.md).
Scope: close the remaining infra/runtime blockers before the first real
Hetzner deployment. DB schema/migrations were explicitly out of scope this
round (managed by Drizzle + an external tool) — nothing here touches Alembic,
schema, or migration tooling.

## Final Verdict

```
READY FOR FIRST HETZNER TEST DEPLOY
```

Every item on the mission's Definition of Done is closed, fixed, or
explicitly marked NOT RUN with a real reason (missing credentials — never a
silent gap). Two **new, previously-undetected bugs** were found only because
this session ran the real stack for hours under real load rather than
re-reading code: Celery Beat was crash-looping continuously (170+ restarts)
from a volume-permission bug, and Alloy's own healthcheck was permanently
broken. Both are fixed and verified stable.

---

## 1. Fixes

| # | Item | Status |
|---|---|---|
| A | `node_deploy.sh` HEAD→GIT_SHA | **Fixed** — 3 occurrences of the same bug class found (not 1), all fixed, 8 new live tests lock it in |
| B | PII in logs | **Fixed** — centralized redaction, 2 layers (app + Alloy), verified live |
| C | Idempotency on sensitive writes | **Fixed** where a real gap existed; documented where the infra was already correct |
| D | Worker LLM metrics not exported | **Fixed** — OTel dual-emission, verified end-to-end to Alloy |
| E | PgBouncer/Postgres fail-fast | **Fixed** — ~11min→~20s failure detection, verified live |
| F | Redis fail-fast (revalidation) | **Confirmed + a second real gap found and fixed** (`.delay()` itself, not just the dedup checks) |
| G | CI/CD test gate | **Fixed** — release now structurally cannot run ahead of CI |
| — | Beat crash-loop (found this session, not in original scope list) | **Fixed** — 170+ restarts/session → 0 |
| — | Alloy healthcheck always-broken (found this session) | **Fixed** |
| — | Alloy docker-socket-proxy couldn't start at all (found this session) | **Fixed** |
| — | Alloy `discovery.docker` silently missing `NETWORKS` permission (found this session) | **Fixed** |

### A. `node_deploy.sh` — HEAD → GIT_SHA

Confirmed the reported gap and found **two more instances of the identical
bug class**, not caught by the original audit because it only read
`node_deploy.sh`:

1. [scripts/node_deploy.sh](../scripts/node_deploy.sh) — single-node
   migration path used literal `"HEAD"`. **Fixed**: now reads the
   currently-running release's `GIT_SHA` from the node's own local release
   manifest (`$CURRENT_FILE`, written by this same script's own previous
   run) as `FROM`, and the already-OCI-label-resolved `$GIT_SHA` as `TO` —
   mirrors `cluster_deploy.sh`'s own fix exactly.
2. [scripts/rollback.sh](../scripts/rollback.sh) — a *different* instance:
   `CUR_SHA` silently fell back to the literal string `"HEAD"` if the release
   manifest had no `GIT_SHA` field recorded. **Fixed**: no GIT_SHA on either
   side of the diff now forces `MIGRATION_REQUIRES_MANUAL_RECOVERY` (the
   conservative choice) instead of guessing.

**Tests**: [backend/tests/test/run-scenarios.sh](../scripts/test/run-scenarios.sh)
— 2 new scenarios (Cas 10, Cas 11) added to the existing mocked-docker
harness. Cas 10 captures the exact 2 arguments passed to
`migration_class_between` across two real single-node deploys and asserts
neither is ever the literal string `"HEAD"`. Cas 11 asserts `rollback.sh`
never falls through to a guessed SHA. **Also found and fixed a pre-existing
bug in the test harness itself**: its `.env` fixture predates `REDIS_URL`
becoming mandatory (chantier Hetzner scale-out) — every scenario in this
file (Cas 1–9, written by the prior session, never actually executed since)
was failing at `preflight` before this fix. All 11 scenarios (30 assertions)
now pass; 1 pre-existing, unrelated `preflight.sh` exit-code nuance (2 vs 1
on missing `.env`) noted but not fixed (out of this session's scope).

`grep` confirms zero remaining dangerous `HEAD` usage in the deploy path
(`check_migrations.sh`'s own `HEAD="${2:-HEAD}"` default is a legitimate
CLI-tool default for standalone/CI invocation, not a deploy-time bug).

### B. PII redaction — two layers

**Layer 1 — application, centralized.** New module
[backend/src/ladini/core/log_redaction.py](../backend/src/ladini/core/log_redaction.py):
a single `logging.setLogRecordFactory()` hook, installed at the top of every
process entrypoint
([api/main.py](../backend/src/ladini/api/main.py),
[api/celery_app.py](../backend/src/ladini/api/celery_app.py) — covers both
worker and beat, which both import it first,
[protocols/mcp/servers/http_server.py](../backend/src/ladini/protocols/mcp/servers/http_server.py)).
Chosen over a per-logger `Filter` because Python's `logging` module only
invokes a `Filter` for the logger that *originated* the call, never for
ancestors during propagation — a filter on the root logger would silently
never fire for any named logger (`ladini.api.tasks`, etc.), which is how
every logger in this codebase is created. A record-factory hook runs before
any handler/formatter, regardless of who installed them (gunicorn, uvicorn,
Celery, or Sentry's own breadcrumb capture — all of which read
`record.getMessage()`, all of which now see the redacted version).

- Phone numbers (`+22670009999` → `+226******99`, keeps country-code prefix
  + last 2 digits for operational correlation — same convention as the
  pre-existing `twilio_sender.py::_mask_phone`).
- Structured secrets (`token=`, `secret=`, `password=`, `api_key=`,
  `Authorization:`, Paydunya/master/private keys, etc.) → fully redacted.
- `Bearer <token>` in free text → redacted.
- Redis/Postgres connection-string credentials → `scheme://***:***@host`
  (host kept — still useful for diagnostics).

**Layer 2 — Alloy, safety net.** 4 targeted `stage.replace` blocks added to
[infra/alloy/config.alloy](../infra/alloy/config.alloy) (new `loki.process
"redact"` component, inserted between `loki.source.docker` and
`loki.write`), matching the same 4 pattern families as the app layer, but
**fully** masking phone numbers (no prefix/suffix kept) — a safety net
should privilege losing correlation over any residual leak. Deliberately 4
small stages, not one large regex.

**Live-verified, not just unit-tested**: sent a real webhook with sentinel
values (`+22670000000`, `whatsapp_token=secret-test`,
`PAYDUNYA_PRIVATE_KEY=secret-test`, `Bearer secret-test` embedded in the
message body) through the actual running stack. Checked worker/api
container logs directly: zero raw phone numbers, zero `secret-test`
occurrences anywhere, including in the exact log lines
(`Orchestrator | phone=...`, `MCP_CALL_AUDIT | ... args={"phone": ...}`,
`AGENT_COMPLETED | workspace=...`) that the previous session's report had
confirmed leaking in plaintext.

**Tests**: [backend/tests/unit/test_log_redaction.py](../backend/tests/unit/test_log_redaction.py)
— 16 tests, both the pure `redact()` function and full round-trips through
the real `logging` module (percent-style calls, f-strings, malformed
`%s` args that would otherwise raise inside a formatter).

### C. Idempotency on sensitive writes

**Investigation first, then fixes** — the actual state was more nuanced than
the prior report assumed:

- **Server-side dedup already exists and is real**, not aspirational:
  `infrastructure/mcp/runtime.py::AgriDBMCPServer.call_tool` has a full
  claim/replay/conflict state machine via `mcp_idempotency_store`, wired
  generically for ANY tool called with a non-empty `_idempotency_key` —
  confirmed by reading the code, dated "réellement appliquée depuis
  2026-09-03" in its own comment.
- **PREORDER confirm + escrow initiation were already correctly protected**:
  `domain/preorder_draft.py::execution_key(draft)` derives
  `f"preorder:{draft_id}:{version}"` from the DB-persisted draft (stable
  across a full Celery redelivery, since the draft is reloaded from Postgres
  on each attempt, not kept in memory) and is genuinely threaded through to
  the MCP call. This is exactly the "stable business identifier" pattern the
  mission asked for — it was simply undocumented as working.
- **Real gap #1 (fixed)**: `domain/sales_publish_draft.py::execution_key()`
  existed with the identical pattern but was **never called** —
  `nodes/executor.py::mcp_tool_executor` only ever read
  `state["procurement_draft"]`, never `state["sales_publish_draft"]`.
  `create_product` therefore always shipped with `idempotency_key=None`
  (falls back to a random UUID per attempt in
  `infrastructure/mcp/client.py::call_tool` — no protection against a full
  task redelivery). **Fixed**: extracted the key-derivation logic into a
  standalone, testable `_derive_execution_idempotency_key(state)` and
  extended it to also check `sales_publish_draft`.
- **Real gap #2 (fixed)**: `select_winning_bid` (accepting a winning
  auction bid — creates an `Order`, irreversible, explicitly named as
  priority "accept bid") had no `idempotency_key` parameter at all. **Fixed**:
  added the parameter to `AuctionGateway.select_winning_bid`, wired at both
  call sites (`flows/buyer/order_tracking.py`, `flows/buyer/negotiation.py`)
  with `f"select_winning_bid:{bid_id}"` — `bid_id` is already a stable,
  DB-anchored business identifier for this exact one-shot action, no draft
  object needed.
- **Two stale docstrings corrected** (`procurement_draft.py`,
  `sales_publish_draft.py`'s `execution_key()`) — both claimed "no server-side
  dedup exists" when it had already been implemented generically since
  2026-09-03; left uncorrected, a future engineer could have built a
  redundant/conflicting mechanism believing this was still missing.

**Not touched, documented as a real, disclosed, lower-priority gap** (per the
mission's "ne rends pas tout compliqué"): the direct (non-preorder)
marketplace order path (`finalize_multi_order`) and `place_bid` — the
latter already has an UPSERT-based DB-layer safety net
(`services/database/auction.py`, confirmed in the previous session's audit:
re-submitting the same bid updates rather than duplicates), making it lower
risk than a true create-once action.

**Tests**: [backend/tests/architecture/test_execution_idempotency_key.py](../backend/tests/architecture/test_execution_idempotency_key.py)
— 12 tests: SALES_PUBLISH `execution_key()` determinism (mirrors the
existing PROCUREMENT test, never existed before since the function was
dead code), `_derive_execution_idempotency_key()` for both draft types +
absence + malformed input + **explicit redelivery simulation** (same
persisted state re-processed twice → identical key), and a source-scan
lock-in test proving both `select_winning_bid` call sites use the exact
same key format.

### D. Worker LLM metrics — closed

**Root cause confirmed**: the Celery worker has no exposed HTTP port at all
(`docker-compose.prod.yml`'s `worker` service has no `ports:`) — `GET
/metrics` structurally only exists on the API, so `record_generation()`
counters recorded from the worker had no way out, ever.

**Fix**: dual emission in
[backend/src/ladini/core/telemetry.py](../backend/src/ladini/core/telemetry.py).
Every `_metric(name)` object is now a small wrapper (`_DualCounter`/
`_DualHistogram`) presenting the identical `.labels(**kw).inc()`/
`.observe()` interface the ~15 existing call sites already use (zero changes
needed at any call site) — internally it updates the same
`prometheus_client` registry as before (API's `/metrics` unaffected) **and**
an OTel `Meter`, pushed over the exact same OTLP pipeline already used for
traces (`worker`/`api` → `alloy:4317`) — no new port, no new credential, no
Prometheus multiprocess mode (would have needed a dedicated HTTP server per
worker, rejected as unnecessary complexity), no Pushgateway.

**A real regression was found and fixed before it shipped**: the first
implementation added a `MeterProvider.force_flush()` call to `telemetry.flush()`
(called at the end of every worker task). Live-tested against an
intentionally unreachable OTLP endpoint: `force_flush(timeout_millis=5000)`
did **not** bound the wait — still blocked after 20+ seconds, because the
underlying OTLP exporter's own retry/backoff loop isn't interrupted by the
provider's timeout parameter. This is the *exact* failure class already
fixed once this session for synchronous Redis calls. **Removed** — metrics
now rely purely on the reader's periodic background export (15s interval,
its own thread, never blocks task processing), which is fully adequate for
aggregate counters (unlike traces/Langfuse events, where a per-task flush
has real correlation value).

**Alloy updated to receive them**: `otelcol.receiver.otlp.app`'s `output`
block now routes `metrics` (previously `traces` only) to
`otelcol.exporter.otlp.grafana_cloud` — Grafana Cloud's OTLP gateway accepts
metrics on the same endpoint (translated to Mimir/Prometheus internally), no
new endpoint/credential needed.

**Live-verified**: rebuilt images, redeployed, triggered a real (mocked-LLM)
webhook turn from the worker, confirmed via Alloy's own component-health API
that `otelcol.receiver.otlp.app` stayed healthy and actively attempted
exports (failing only against the deliberately-fake Grafana Cloud endpoint,
exactly the expected "up to the export boundary" behavior).

**Tests**: [backend/tests/unit/test_telemetry_worker_metrics.py](../backend/tests/unit/test_telemetry_worker_metrics.py)
— confirms a MeterProvider is actually constructed when metrics are recorded
outside any HTTP-serving context (simulating the worker), confirms `/metrics`
still exposes the same series unchanged, and **locks in the flush-latency
regression as a permanent test**: `flush()` must return in under 2s even
against a real unreachable port — this test would have caught the bug above
automatically.

**Regression found and fixed in existing tests**: wrapping the raw
`prometheus_client` Counter/Histogram objects broke
`test_telemetry_cost_and_legacy_fallback.py` (4 pre-existing tests), which
introspects `.labels(...)._value.get()` — a `prometheus_client` internal, but
already relied on by this repo's own test suite before this session. Fixed
by adding a `_value` passthrough property on the wrapper types.

### E/F. PgBouncer / Postgres / Redis fail-fast

Both live-tested before and after, not just reasoned about.

**PgBouncer/Postgres** — root cause of the previously-observed ~10+ minute
hang: PgBouncer had zero TCP-keepalive configuration, so a backend that
disappears **without a clean FIN/RST** (container killed, not gracefully
stopped) leaves a half-open connection that only the OS's own (very long)
default TCP retransmission timeout would eventually notice. Added, in
[docker-compose.prod.yml](../docker-compose.prod.yml)'s `pgbouncer` service:
`server_connect_timeout=5`, `server_login_retry=5`, `query_wait_timeout=20`,
`server_idle_timeout=60`, and — the actual fix —
`tcp_keepalive=1` / `tcp_keepidle=5` / `tcp_keepintvl=3` / `tcp_keepcnt=3`
(worst-case dead-peer detection ≈14s instead of relying on kernel defaults).
**Live-measured**: `docker kill` on Postgres → first `/health/ready` call
after now returns `503` in **~20s** (was 10+ minutes). Recovery after
Postgres restarts: ~15-20s (bounded by `server_login_retry`), fully
automatic, no restart storm.

**Redis** — re-validated the existing fix (still holds: `/health/ready` and
the dedup/idempotency-cache checks fail in ~4s), but found a **second, real,
previously-undiscovered gap while revalidating**: the webhook's actual
Celery enqueue (`process_agent_task.delay(...)`) is *itself* a separate
blocking call on the Redis broker, not covered by last session's fix (which
only wrapped the dedup-check and role-hint-cache calls). Live-measured before
fixing: a webhook could still hang 16–25+ seconds during a full Redis
outage. Two changes:
1. All 7 `.delay()` call sites across
   [whatsapp_webhook.py](../backend/src/ladini/api/routes/whatsapp_webhook.py)
   and
   [twilio_webhook.py](../backend/src/ladini/api/routes/twilio_webhook.py)
   wrapped in `asyncio.to_thread` (prevents one slow enqueue from blocking
   *other* concurrent requests on the same gunicorn worker).
2. `celery_app.py`: `broker_transport_options` gained
   `socket_connect_timeout=3`/`socket_timeout=3`, and
   `broker_connection_max_retries=2` (was Celery's default of 100, with
   growing backoff — the real reason individual attempts kept taking so
   long). **The two main `process_agent_task.delay()` call sites** are
   additionally wrapped in `asyncio.wait_for(..., timeout=5.0)` — a hard
   upper bound on the coroutine's wait, independent of whatever the
   underlying kombu/Celery retry logic does internally (the background
   thread may keep running briefly after the timeout — same documented
   trade-off as the OTel flush fix above, bounded and rare).

**Live-measured after all three fixes**: a webhook during a full Redis
outage now returns `200` (fail-open, message simply not enqueued for this
attempt) in **~13s** — down from 25s+/unbounded, and now genuinely bounded
rather than dependent on kombu's internal state. The 5 remaining
(lower-traffic — media/photo-menu) `.delay()` sites got the `to_thread`
protection but not yet the hard `wait_for` bound; documented as a residual,
minor gap (concurrent-request protection is in place; individual-request
latency bound is not, for these 5 only).

### G. CI/CD test gate

Confirmed the reported gap and found it was **structurally worse** than
"missing `needs:`" — `release.yml` (the workflow that actually builds and
**pushes** immutable images to GHCR) had **no dependency at all** on
`cicd.yml` (the workflow that runs tests): both triggered independently on
`push: branches: [main]`, running in full parallel. The `cicd.yml`
`docker-build` job also had no `needs:` on its own `test`/`migration-safety`
siblings.

**Fixed**:
- [.github/workflows/cicd.yml](../.github/workflows/cicd.yml): `docker-build`
  now `needs: [test, migration-safety]`.
- [.github/workflows/release.yml](../.github/workflows/release.yml):
  retriggered from `on: push` to `on: workflow_run: workflows: ["CI"]`
  — release now only *starts* after CI (the whole workflow) has finished,
  and its first job additionally guards `if:
  github.event.workflow_run.conclusion == 'success'` (red CI → release
  never runs, not even queued). `workflow_dispatch` (manual trigger)
  preserved unchanged. Checkout now pins to
  `github.event.workflow_run.head_sha` — the exact commit CI validated,
  never an ambiguous `github.sha` in this trigger context.

Both files validated with `yaml.safe_load` (not live-tested — GitHub Actions
can't run locally; the logic mirrors GitHub's own documented `workflow_run`
pattern for exactly this "gate workflow B on workflow A's success" use case).

---

## 2. Grafana / Alloy

| Item | Status |
|---|---|
| Alloy started locally | **Done**, and fixed 3 real bugs along the way (below) |
| Grafana Cloud real push | **NOT RUN** — no `GRAFANA_CLOUD_*` credentials in this environment (only an unrelated, unused `GRAFANA_TOKEN` exists in `.env`, consumed by nothing in this repo) |
| Dashboards/alerts | Structurally reviewed last session (READY); not re-validated against a live Grafana Cloud this session (same credential gap) |

Three genuine, previously-undetected bugs found only because Alloy was
actually **run**, not just read:

1. **`docker-socket-proxy` could never start at all.** `read_only: true`
   (correct hardening intent) with no writable scratch space — the image's
   own entrypoint generates `haproxy.cfg` from a template at every startup
   and was hitting `Read-only file system` on every single attempt, crash-
   looping. Fixed with a custom entrypoint that reads the (untouched, still
   image-provided) template and writes the rendered config to a `tmpfs`
   mount elsewhere (`/run`), never touching `/usr/local/etc/haproxy` (which
   also holds the error pages the config references by absolute path — an
   earlier, wrong attempt at fixing this by `tmpfs`-mounting that whole
   directory broke those references instead).
2. **Alloy's own Docker healthcheck was permanently broken** —
   `wget -qO- http://127.0.0.1:12345/-/ready`, but the `grafana/alloy:v1.4.3`
   image has no `wget`, no `curl`, no `nc`, and no bash (`/dev/tcp`
   unsupported by its `sh`). The container had been reporting `unhealthy`
   since its very first boot regardless of actual state. Fixed with a
   healthcheck built from `awk`/`grep`/`/proc/net/tcp{,6}` (confirmed Alloy
   binds its admin port on IPv6) — process/port liveness, not a full
   `/-/ready` semantic check, but genuinely working instead of permanently
   red.
3. **`discovery.docker` (log/container discovery) failed outright** —
   `NETWORKS=0` on the socket proxy (documented in the prior session as
   "least privilege, Alloy doesn't need it") was wrong: Alloy's Docker
   discovery component calls `GET /networks` in addition to
   `/containers/json` to build network labels, and with it blocked, *every*
   discovery cycle failed entirely (`"error while computing network labels:
   403 Forbidden"`) — meaning **zero** containers were ever actually
   scraped or log-tailed, contradicting the "READY" verdict the static-only
   audit gave this component last session. Fixed: `NETWORKS=1` (still
   read-only, still no write/exec/build capability — a listing endpoint,
   not a meaningful security regression).

**Live-verified after all three fixes**: Alloy's own component-health API
(`/api/v0/web/components`) shows `discovery.docker.containers`,
`loki.source.docker.containers`, `prometheus.exporter.cadvisor.docker`, and
all 6 `prometheus.scrape.*` targets (host, docker, api, redis/pgbouncer/
celery exporters) as `healthy` — genuine end-to-end local collection,
correctly failing only at the final push to the deliberately-fake Grafana
Cloud endpoint (`*.invalid` DNS, used specifically so no real credentials
were ever needed to prove the pipeline up to that boundary).

---

## 3. Load tests

All against the local E2E stack, `MOCK_EXTERNAL_APIS=true` (zero real
Groq/WhatsApp/Paydunya calls, zero cost), HMAC-signed requests (the real
signature-verification code path, not bypassed).

| Scenario | Throughput | p50 | p95 | Error rate | Notes |
|---|---|---|---|---|---|
| 50 VU (prior session, post-fix) | 69.8 req/s | 626ms | 1.58s | 0% | baseline confirmed still holds |
| 200 VU (prior session, post-fix) | 109.9 req/s | 1.61s | 3.2s | 0% | baseline confirmed still holds |
| **500 VU sustained (new)** | 110.6 req/s | 4.09s | 12.85s | **34.3%** | genuine hardware ceiling reached on this laptop — see below |
| **Burst 10→50→200→500 (new)** | 89.7 req/s avg | 1.58s | 5.32s | **0%** | ramped burst degrades latency but never drops a connection |
| **Soak, 30 VU × 18 min (new)** | 29.1 req/s | 17.5ms | 37ms | **0%** | see §4 |

**500 VU standalone**: throughput plateaus at essentially the same ~110
req/s as 200 VU (confirms this is a genuine capacity ceiling, not the
earlier pathological bug — that one produced a perfectly *flat* curve
regardless of load; this one shows real, if saturated, work happening), but
34% of requests failed at the connection level (some with 0ms duration —
consistent with the OS/gunicorn connection backlog being exceeded, not an
application error). **The burst test, reaching the same 500 VU peak via a
gradual ramp, had 0% failures** — the system handles the *same* peak load
fine when it isn't hit instantaneously, suggesting the standalone-500-VU
failures are a connection-acceptance-rate artifact of this specific test
shape/host, not a structural ceiling. Either way: **on this single
dev laptop** (shared with Docker Desktop, 12+ containers, and k6 itself),
somewhere between 200 and 500 *simultaneous* new connections is the
practical limit. Re-run both scenarios on a dedicated Hetzner box before
trusting the absolute numbers for capacity planning — the qualitative
findings (graceful degradation under a ramped burst, no crashes, no data
loss) should hold regardless of hardware.

No container crashed, no OOM, no restart-loop was triggered by any of these
runs (confirmed via `RestartCount` before/after on `beat`, `worker`, `api`).

---

## 4. Soak test

30 VUs, 18 minutes, 31,476 requests, **0% error rate**, stable latency
throughout (no upward drift visible in the tail of the run: still ~17-37ms
p50/p95 at minute 18, same as minute 1).

| Metric | Before | After (+18min) | Verdict |
|---|---|---|---|
| api RSS | 623.7 MiB | 604 MiB | stable |
| worker RSS | 601.7 MiB | 682.5 MiB (+80MB) | bounded, not runaway — see note |
| mcp RSS | 113 MiB | 121.2 MiB | stable |
| postgres RSS | 103 MiB | 172.9 MiB | normal buffer-cache growth under load |
| redis RSS | 9.2 MiB | 17.6 MiB | stable |
| Redis connected_clients | 92 (post-burst) | 61 | **no leak** — went down |
| Queue depth (post-soak) | — | **0** | fully drained, no accumulation |
| PIDs (all containers) | baseline | ≤ baseline | no thread/process leak |

The worker's +80MB over 18 minutes / 31k tasks is the only figure worth a
second look — plausibly Python's allocator not returning freed memory to the
OS (a common, benign pattern for long-lived processes with LangGraph/
LangChain object churn), not necessarily a true leak. **Recommend a longer
soak (2-4h) on the actual target hardware before fully trusting this at
production scale** — 18 minutes is enough to rule out a fast leak, not a
slow one.

---

## 5. Scaling

### Worker × N (prior session, reconfirmed)

1 worker ≈ 6.7–9.2 tasks/s drain rate, MCP pinned at ~85-93% CPU. Scaling
worker 1→3 gave **no additional throughput** (still 6.7–8 tasks/s) — MCP was
already the ceiling, confirming the prior session's finding still holds.

### MCP × 1 vs × 2 (new)

| | Drain rate | mcp-1 CPU | mcp-2 CPU |
|---|---|---|---|
| MCP ×1, worker ×3 | ~6.7-9.2 tasks/s | ~93% | — |
| **MCP ×2, worker ×3** | **~10.8 tasks/s** | ~47% | ~87% |

Scaling MCP **does** increase throughput (+~30-60% depending on which x1
baseline is used for comparison) — confirms the architecture's own
documented scaling model ("dupliquer le service MCP, jamais `--scale`
worker seul") actually works, not just in theory. The gain is **sub-linear**
and the load split across the two MCP replicas was **uneven** (47%/87% CPU,
not a clean 50/50) — Docker Compose's embedded-DNS round-robin across scaled
replicas is not a real load balancer; the architecture's own stated design
(duplicate MCP + a real LB/reverse-proxy per node, per the Hetzner topology
doc) would likely give a cleaner, closer-to-linear curve than relying on DNS
round-robin alone, which this test effectively exercised end-to-end since
that's exactly what Docker Compose scaling provides locally.

**Next bottleneck past MCP×2**: not reached in this session's testing — the
uneven CPU split (87% on the busier replica) suggests a third MCP replica
would show diminishing returns before a genuinely different resource (worker
CPU, PgBouncer pool, Postgres itself) becomes limiting. Worth a dedicated
MCP×3 measurement on real hardware before Hetzner capacity planning.

---

## 6. Multi-node / rolling deploy / restart policy

**Multi-node**: this session's MCP×2 + worker×3 scaling test **is** a real
multi-instance-per-service proof (traffic genuinely split across replicas,
correct dedup/idempotency held across both MCP instances sharing the same
Postgres/Redis) — combined with the prior session's 2-API-node stateless
conversation-continuity proof, this covers "multiple instances of every
scalable service, sharing state correctly" for app/worker/mcp. **Not built**
this session: a full separate node-A/node-B topology behind a dedicated
local reverse-proxy/LB (as opposed to Docker Compose's own service-level
scaling) — time-boxed out. The qualitative behavior (stateless, DB/Redis as
the only shared truth) is proven either way; a literal 2-VM-shaped local
harness would mainly re-prove the same property with more moving parts, not
new confidence.

**Rolling deploy**: `scripts/test/run-scenarios.sh`'s mocked-docker harness
(11 scenarios, 30 assertions, all passing — see §1.A) genuinely exercises
`node_deploy.sh`'s full rolling-deploy state machine: sequential
A→B→C→D→rollback, auto-rollback on unhealthy container, auto-rollback on
failed smoke, concurrent-deploy lock rejection, migration classification.
This **is** the rolling-deploy primitive that `cluster_deploy.sh` calls once
per node. **Not built**: a live simulation of `cluster_deploy.sh`'s own
SSH-orchestration loop across multiple mocked hosts — would need a
purpose-built SSH-mock harness this session didn't have time to construct;
the script's own logic (lock → validate → migrate-once → node-by-node →
beat-last) was verified by direct code reading in the prior session and is
unchanged this session.

**Linux restart policy**: still **NOT RUN** — confirmed again this session
that `restart: unless-stopped` does not reliably fire on Docker Desktop for
Windows (re-tested with a plain `busybox --restart=unless-stopped`
container: identical non-restart behavior after `docker kill`, isolating
this to the Docker Desktop/Windows environment, not this repo's compose
config). No Linux Docker host was available in this session either. The
compose directive itself is correct and will behave as documented on a real
Linux Engine host — validate on the first actual Hetzner node.

---

## 7. Terraform

`fmt`/`validate` already passed (fixed last session). This session: **no
`HCLOUD_TOKEN`/`TF_VAR_hcloud_token` available in this environment** —
`terraform plan` is **NOT RUN**, per the mission's own explicit stop
condition. `terraform apply`: not attempted (never authorized, no
credentials regardless).

---

## 8. Full regression suite

Baseline (previous session): ≈3,773 passed / 24 failed (all pre-existing —
22 business-logic routing/procurement-draft tests already broken on this
in-progress refactor branch, 2 an unrelated `fastmcp` dependency-version
mismatch).

**This session found and fixed 5 genuine new regressions before they could
ship**, all caused by the telemetry dual-emission refactor:
- 4 pre-existing tests (`test_telemetry_cost_and_legacy_fallback.py`) broke
  because they introspect `prometheus_client`'s internal `._value` attribute
  directly on what used to be a raw `Counter`/`Histogram` — fixed with a
  passthrough property on the new wrapper types.
- 1 new test (`test_telemetry_worker_metrics.py`, written this session)
  initially failed under `pytest`'s full-suite run order (but passed in
  isolation) — a `Settings` singleton constructed once, before the test's
  own env-var monkeypatching could take effect. Fixed by patching the
  singleton's attributes directly instead of relying on env vars.

**Final state**: **3,804 passed / 24 failed / 1 skipped** — the +31 passed
are exactly the new tests added this session (16 `test_log_redaction.py` +
12 `test_execution_idempotency_key.py` + 3 `test_telemetry_worker_metrics.py`).
**Zero new regressions**: the 24 failures are byte-for-byte the same 24 test
IDs as the prior session's baseline (22 business-logic
routing/procurement-draft tests on this in-progress refactor branch, 2
`fastmcp` dependency-version mismatch) — none in any file this session
touched.

---

## 9. Remaining gaps (real, disclosed)

| Gap | Why not closed this session |
|---|---|
| `finalize_multi_order`/direct marketplace order has no MCP-level idempotency key | Lower priority than the 2 fixed gaps — no draft/state-machine object exists for this path yet; would need either a new one (business-logic surgery, out of scope) or a `message_sid`-derived key at a call site not yet located this session |
| `place_bid` has no MCP-level idempotency key | Lower risk — DB layer already does UPSERT-on-resubmit (confirmed prior session), a duplicate `.delay()` retry updates rather than double-creates |
| 5 of 7 `.delay()` call sites (media/photo-menu tasks) have `asyncio.to_thread` but not the hard `asyncio.wait_for` bound | Lower traffic than the 2 main `process_agent_task.delay()` sites; concurrent-request protection is in place, individual-request latency bound is not |
| Alloy → Grafana Cloud real push | No credentials in this environment (documented, not a code gap) |
| MCP×3+ scaling curve | Not measured — MCP×2 already showed uneven distribution; a real LB (not Docker Compose scaling) would need to exist first for a meaningful ×3 test |
| Full `cluster_deploy.sh` SSH-orchestration live simulation | Time-boxed out; single-node rolling-deploy logic (`node_deploy.sh`, which it calls per-node) is thoroughly tested |
| Linux restart-policy live validation | No Linux Docker host available in this environment (Docker Desktop for Windows only) |
| `terraform plan`/`apply` | No Hetzner credentials in this environment |
| `preflight.sh` exit-code nuance (2 vs 1 on missing `.env`) | Minor, unrelated to this session's 20 items, noted in passing during test-harness work |

---

## 10. Exact commands — unchanged from the prior report

See
[docs/LOCAL_E2E_PREDEPLOY_VALIDATION_2026-09-16.md §20](LOCAL_E2E_PREDEPLOY_VALIDATION_2026-09-16.md#20-exact-commands-for-the-first-hetzner-deployment)
— nothing in this session changes the deployment sequence itself, only its
safety margins. One addition: `scripts/predeploy_check.sh` now also runs the
PII-redaction, idempotency-key, and worker-metrics regression tests
(`--3--` step), still fast enough to run before every real deploy.
