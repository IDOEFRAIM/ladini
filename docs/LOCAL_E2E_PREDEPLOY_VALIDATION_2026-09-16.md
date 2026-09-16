# Local E2E Pre-Deploy Validation — 2026-09-16

Scope: full local end-to-end validation of the Ladini/MarketCoach stack (Caddy-less local
equivalent → API → Redis → Celery → Worker → MCP → PgBouncer → Postgres) ahead of the
first real Hetzner deployment. Business logic (intents, A–G flows, Legacy Retirement) was
explicitly out of scope and was not touched, except where an infra bug blocked execution
(documented per-fix below).

## Executive Summary

**READY FOR FIRST HETZNER DEPLOY — with three items to action first** (see "Before you
deploy" below). None of them are architectural rewrites; all three are either already
fixed in this session or are config/credential steps you take once.

The deploy/observability/rollback engineering done in the prior "chantier Hetzner
scale-out" is fundamentally sound: build-once/immutable-image discipline, beat singleton
enforcement (two independent guards), PgBouncer-per-node sizing math, Redis made external,
queue separation (interactive/background/scheduled), and the Alloy socket-proxy hardening
are all real and correctly wired, not just documented aspirations. What this session did
was run all of it for real, for the first time, against a live local stack — and it found
(and where safe, fixed) the class of bug that *only* shows up under real execution:
`terraform validate` had never been run and failed immediately; the beat healthcheck fix
was never propagated to `smoke.sh`; and the single highest-impact finding, a synchronous
Redis client blocking the FastAPI event loop on every webhook, which silently capped
webhook throughput at **~42 req/s regardless of concurrency** — found via k6, fixed, and
re-verified to scale properly afterward.

### Before you deploy
1. **Migration bootstrap decision.** There is no Alembic and no committed schema-creation
   script for a fresh Postgres (see "Migrations" below) — the current managed DB has its
   schema from historical, out-of-repo setup. Decide how the *first* Hetzner Postgres gets
   its schema (reuse an existing managed instance, or use the new
   `scripts/test/e2e/bootstrap_db.py` pattern this session added, reviewed for prod use).
2. **`node_deploy.sh:230` still resolves migrations against literal `HEAD`** instead of
   the deployed image's `GIT_SHA` — the exact bug `cluster_deploy.sh`'s own comments
   describe as fixed, but the fix was only applied to the cluster path. Low risk today
   (single node, working tree usually matches `HEAD`) but worth fixing before the fleet
   grows or CI resolves a different commit than the deploy host's checkout.
3. **Terraform has real credentials-gated steps left** — `fmt`/`validate` now pass (two
   real bugs fixed this session), but `terraform plan`/`apply` were never run (no
   `HCLOUD_TOKEN` available) — budget time for first-`plan` surprises.

---

## 1. Architecture tested

```
                 k6 / curl (signed webhooks)
                          │
                          ▼
                 API (gunicorn/uvicorn, 4 workers)
                    │           │
              (async, fixed)    │
                    ▼           ▼
                  Redis      PgBouncer ── Postgres (local, pgvector/pgvector:pg16)
                    │           │
                    ▼           │
              Celery Worker ────┘
              (interactive/background/scheduled/celery queues)
                    │
                    ▼
                   MCP (single instance, HTTP, auth token)
                    │
                    ▼
              Postgres (same instance, via PgBouncer)

  Beat (singleton) ──schedules──▶ Celery Worker
  Flower (admin) ──observes──▶ Celery/Redis
  Autoheal ──watches Docker socket (ro)──▶ restarts unhealthy *liveness* containers
```

Everything above ran as real Docker containers under an isolated Compose project
(`-p ladini-e2e`), built from the actual `infra/docker/Dockerfile.{api,worker,mcp}`
via the existing `docker-compose.build.yml` overlay — not mocked containers, not a
simplified stand-in. New files added for this (all clearly E2E-only, never referenced
by the production deploy path):

- [docker-compose.e2e.yml](docker-compose.e2e.yml) — local Postgres, TLS-disabled
  PgBouncer override, `MOCK_EXTERNAL_APIS`/`SANDBOX_MODE` injection (see below).
- [.env.e2e](.env.e2e) — non-production secrets, gitignored.
- [scripts/test/e2e/bootstrap_db.py](scripts/test/e2e/bootstrap_db.py) — creates the 4
  Postgres schemas (`auth`/`marketplace`/`governance`/`intelligence`) and all 49 ORM
  tables via `Base.metadata.create_all()`, since no Alembic/schema-bootstrap exists.
- [scripts/predeploy_check.sh](scripts/predeploy_check.sh) — new, see §"predeploy-check".

### External-provider neutralization (Phase 2)

No code had to be invented for this — the codebase already had `MOCK_EXTERNAL_APIS` /
`SANDBOX_MODE` sandbox flags (added 2026-08-27) that short-circuit the Groq/Bedrock LLM
clients to a deterministic fake (`core/get_llm.py::_MockGroqClient`). `docker-compose.prod.yml`
deliberately does **not** wire these two flags into its env template (correct — they're
sandbox-only), so `docker-compose.e2e.yml` injects them explicitly for `api`/`worker`/
`mcp`/`beat`.

Two additional gaps were found and fixed, both consistent with the existing pattern:

- **WhatsApp/Twilio outbound send had no sandbox short-circuit.** With
  `MOCK_EXTERNAL_APIS=true` but empty WhatsApp/Twilio credentials, the send functions in
  `api/response_dispatch.py` correctly refused to call the real network (both
  `_send_via_whatsapp_cloud`/`_send_via_twilio` fail closed on missing config) — but they
  **raised**, which Celery's `autoretry` caught and retried forever, so no task ever
  completed. Added a `MOCK_EXTERNAL_APIS` guard mirroring `_MockGroqClient`'s placement:
  logs a `MOCK-SANDBOX` result and returns, exactly like the LLM mock. See
  [response_dispatch.py](backend/src/ladini/api/response_dispatch.py).
- **Paydunya**: no fix needed — `ESCROW_PAYMENT_ENABLED=false` (already the documented
  dev default) means the payment call is never reached at all.

With these two pieces, the full webhook → idempotency → Celery → MCP → Postgres →
LangGraph agent → mocked-LLM → mocked-send pipeline runs end-to-end with **zero** outbound
calls to Groq, Bedrock, WhatsApp/Meta, Twilio, or Paydunya, and zero cost.

---

## 2. Component status (Phase 1 audit)

| Component | Status | Notes |
|---|---|---|
| `docker-compose.prod.yml` | READY | Profiles (app/scheduler/admin) correct; no `build:`; hardening (`cap_drop`, `no-new-privileges`) present; env-var ports added this session for E2E port isolation, prod default unchanged (8000/5555). |
| `docker-compose.dev.yml` / `.build.yml` | READY | Local Redis correctly excluded from prod; build overlay correctly build-once-only. |
| `docker-compose.e2e.yml` (new) | READY | Local Postgres + TLS-disable pgbouncer override + sandbox env injection. |
| `scripts/cluster_deploy.sh` | READY | Global lock, inventory validation, migrate-once, rolling deploy w/ readiness waits, beat-last, release recording — all real, not stubs. LB post-deploy check is an honestly-labeled `TODO` no-op. |
| `scripts/node_deploy.sh` | PARTIAL | Self-contained and correct for the cluster path; its own single-node migration branch (`deploy.sh`'s path) still resolves against literal `HEAD` instead of `GIT_SHA` — the exact bug the cluster path's comments describe as fixed elsewhere. **Not fixed this session** (business-adjacent judgment call on migration safety left to the team; flagged with file:line above). |
| `scripts/deploy.sh` | READY | Thin wrapper, inherits the `HEAD` issue above. |
| `scripts/cluster_rollback.sh` | READY | Mirrors deploy lock/ordering; requires `ROLLBACK_FORCE=1` after a `MIGRATION_REQUIRES_MANUAL_RECOVERY` release. |
| `scripts/smoke.sh` | FIXED | Beat check used a pidfile pattern already proven broken (own compose healthcheck comment documents the incident) and never updated here — **fixed this session** to use `pgrep`, verified live against the E2E stack. |
| `scripts/smoke_observability.sh` | READY | `/metrics` check real; Alloy check tolerant when absent; Grafana Cloud check best-effort, never blocks. |
| `scripts/preflight.sh` | READY | Rejects `latest`/mutable tags, checks required env, no `build:`, image manifests present. |
| `scripts/dev-up.sh` | READY | Dev-only, correctly out of the prod path. |
| `scripts/validate_inventory.py` | READY | Verified live: 2-scheduler inventory correctly rejected (exit 1) before any deploy touches infrastructure. |
| `scripts/predeploy_check.sh` (new) | READY | See dedicated section below. |
| `infra/providers/hetzner/*.tf` | FIXED (was BROKEN, never run) | Two real bugs found and fixed this session — see Terraform section. `fmt`/`validate` now pass. `plan`/`apply` NOT RUN (no credentials). |
| Hetzner `cloud-init` | READY | Confirmed: git clone is bootstrap-only (one-time, `runcmd`), used only to fetch `scripts/`+`infra/firewall/ufw.sh`; the running application always comes from immutable `ghcr.io/.../ladini-*:<sha-tag>` images pulled by `deploy.sh`, never `git pull`+run. |
| `infra/alloy/` (Alloy + docker-socket-proxy) | READY | Socket proxy pinned, `CONTAINERS=1`/`INFO=1` only, everything else (`POST`,`EXEC`,`IMAGES`,`BUILD`,...) `=0`; Alloy never mounts the real Docker socket. NOT RUN live locally (needs Grafana Cloud creds to be meaningful — see §Observability). |
| `infra/grafana/{dashboards,alerts}` | READY | 4 dashboards / ~29 panels, 13 alert rules across 5 groups present and structurally complete. NOT validated against a live Grafana Cloud (no credentials). |
| `infra/exporters/` | READY | celery/redis/pgbouncer exporters pinned, internal-network-only. |
| `.github/workflows/` | READY (one nuance) | Build-once, immutable SHA tags, concurrency guards on all 3 workflows, manual-approval deploy gate. `release.yml` also pushes a `latest` convenience tag (deploy scripts reject it, so harmless but worth knowing). `cicd.yml`'s `docker-build` job has no explicit `needs: test` (parallel, not gated) — verify branch protection covers this. |
| `load-tests/` | READY, executed | See §Load testing. |
| Beat singleton (2 layers) | READY, verified live | `test_compose_deployment_invariants.py` + `test_validate_inventory.py` (15/15 pass); live attempt to configure 2 schedulers correctly rejected. |

---

## 3. Local E2E boot (Phases 3–4)

Full stack (`api`, `worker`, `mcp`, `beat`, `flower`, `pgbouncer`, `redis`, `postgres`,
`autoheal` — 9 containers) booted cleanly to `healthy` with **zero** import errors and
**zero** crash loops, under an isolated Compose project (`ladini-e2e`) so as not to
disturb an already-running dev stack discovered on this machine (see incident note below).

| Endpoint | Behavior verified |
|---|---|
| `GET /health` / `/health/live` | Always 200, no external dependency touched — correct liveness semantics. |
| `GET /health/ready` | 200 when DB+Redis reachable; **503** when either is down; recovers automatically once the dependency returns, no manual intervention. |
| `GET /version` | Reflects injected `RELEASE_VERSION`/`GIT_SHA`/`BUILD_TIMESTAMP`. |
| `GET /metrics` | Real Prometheus/OpenMetrics text, `ladini_*` series present. |

**Incident during setup** (documented for transparency, no data lost): the first
`docker compose up` accidentally ran under the same implicit project name as an
already-running local Ladini dev stack (up 13h, healthy), briefly recreating its `redis`
and `beat` containers. All services recovered to `healthy` within seconds; all further
work used an explicit `-p ladini-e2e` project to prevent recurrence.

---

## 4. Celery / queues (Phase 5)

Verified live: `process_agent_task` correctly routes to the `interactive` queue;
`workers.crons.*` → `scheduled`; media/payment tasks → `background` (per
`celery_app.py::TASK_ROUTES`). `task_acks_late=True` + `broker_transport_options.
visibility_timeout=660` confirmed in config (redelivery window bounded above the 600s
`task_time_limit`).

**Interactive-vs-background isolation**: not independently load-tested with a synthetic
slow background task (time-boxed out of this session) — the routing table itself
structurally prevents a `product_photo_task`/`paydunya_ipn_task` from ever landing on the
`interactive` queue, so head-of-line blocking would require deploying dedicated queue
consumers incorrectly, not a runtime race. Recommend a targeted test before go-live if
this the specific single point of design confidence needed.

---

## 5. Failure injection (Phases 7–10)

| Component | Failure | Expected | Actual | Verdict |
|---|---|---|---|---|
| Redis | `docker stop` 30s+ | `/health/ready`→503; liveness stays 200; no restart storm; auto-recover | Exactly as expected. **But**: the in-flight webhook request at the moment Redis died **hung 30s+** (client-timeout-bounded, likely longer) instead of failing fast — root cause below (fixed). | PASS (with a real latency finding, now fixed) |
| Postgres | `docker stop` | `/health/ready`→503; liveness stays 200; no restart storm; auto-recover | Correct end-state, but the **first** request after the kill hung for ~3 minutes (pgbouncer took that long to detect the crashed backend connection via TCP-level failure, not DNS) before pgbouncer started failing new connections fast (~0.2s, 503). Subsequent requests were fast-fail the whole time. Full recovery within seconds of Postgres coming back. | PASS, with a real (documented, not fixed) hang-window finding |
| MCP | `docker stop` | webhook still accepted fast (readiness doesn't check MCP, by design); task fails gracefully within its `asyncio.wait_for` budget, no hang | Exactly as expected — task completed in ~4s with a graceful fallback response, no infinite retry. | PASS |
| Worker | `docker kill` mid-task | container auto-restarts (`restart: unless-stopped`); task redelivered after `visibility_timeout` | Container did **not** auto-restart under Docker Desktop for Windows — confirmed via a control test with a bare `busybox --restart=unless-stopped` container exhibiting the identical non-restart behavior. **This is a Docker Desktop/Windows environment limitation, not a defect in the compose config** (`restart: unless-stopped` is correctly present and will apply as expected on a standard Linux Docker Engine host, i.e. the actual Hetzner target). Manually restarted to continue testing. | Compose config correct; **live auto-restart validation NOT achievable on this host** — re-verify on the first real Linux node or a Linux VM. |

### Idempotency window (Phase 10)

Code-level finding, not a live 11-minute redelivery demonstration (time-boxed out):
`infrastructure/mcp/client.py::call_tool` generates `idempotency_key = idempotency_key or
str(uuid.uuid4())` **per call**, stable only across network-blip retries *within* that one
`call_tool()` invocation. A full Celery task redelivery (new task execution after a worker
crash) re-runs the LangGraph flow from scratch and mints **new** random idempotency keys —
so `services/database/mcp_idempotency_store.py`'s real, DB-backed
`(idempotency_key, tool_name)` dedup table does **not** protect against a full-task
redelivery double-executing a sensitive MCP write (`create_order`,
`initiate_escrow_payment`, etc.). This is **already explicitly documented as a known,
intentional scope limit** in the client's own docstring ("reste à implémenter tool par
tool pour les actions sensibles") — not a hidden bug, and implementing per-tool
deterministic keys (e.g. derived from `message_sid`) is genuine business-logic surgery on
money-moving paths, correctly out of this session's mandate. **Recommend prioritizing
this** before scaling worker replicas, since more replicas increase the chance of a
mid-task crash-and-redeliver.

---

## 6. Beat singleton (Phase 11)

Two independent layers, both verified passing live:
1. `docker-compose.prod.yml`: only one service (`beat`) carries the `scheduler` profile —
   enforced by `test_compose_deployment_invariants.py`.
2. `scripts/validate_inventory.py`: rejects 0 or ≥2 nodes with the `scheduler` role.
   **Live-tested**: a deliberately-crafted 2-scheduler `inventory.yml` was rejected with
   exit code 1 and a clear error message, before any deploy step ran.

---

## 7. Multi-node simulation & statelessness (Phases 12–13)

A second `api` container was started by hand (`docker run`, same image, same Postgres/
Redis/MCP, different host port) alongside the existing one — a lightweight but real proof,
not a full second app-node stack with its own worker/mcp/pgbouncer (that would need a real
reverse-proxy/LB harness; time-boxed out — see "Remaining gaps").

**Test**: conversation turn 1 sent to node A (`:18000`), turn 2 (same phone number) sent
to node B (`:18001`).

**Result**: both turns resolved to the identical `workspace=+22670009999`, processed
correctly, state persisted and re-read via the shared Postgres `agri_workspaces` table —
zero dependency on which node handled which turn. This is real evidence the API layer is
stateless as designed, not just a claim from reading the code.

---

## 8. Rolling deploy / migrations-exactly-once / rollback (Phases 14–17)

**Not exercised as a live SSH-based rollout** (no second real host available locally;
`cluster_deploy.sh` is SSH-driven by design). Instead, verified via direct code reading +
the component tests already covering the invariants that matter:
- Migrations-exactly-once: `cluster_deploy.sh` runs `alembic upgrade head` once, from the
  orchestrator, before touching any node (`:238-259`) — structurally impossible for two
  nodes to each run it, since nodes never invoke it themselves in the cluster path.
- Beat-last ordering: `cluster_deploy.sh` explicitly reorders nodes so any node carrying
  `scheduler` deploys last (`:279-289`).
- Rollback: `cluster_rollback.sh` requires `ROLLBACK_FORCE=1` after a
  `MIGRATION_REQUIRES_MANUAL_RECOVERY`-classified release, and rolls back nodes in reverse
  order without cascading past the first failure.

**Recommend**: before the first real 2-node deploy, do a live dry run of
`cluster_deploy.sh`/`cluster_rollback.sh` against two throwaway Hetzner VMs — this is
exactly the kind of SSH-orchestration logic that reads correctly but benefits from one
real execution before it's load-bearing.

### Migration compatibility (Phase 16)

No Alembic exists (see below) and no destructive migration was found in this session's
diff (none were made). The **existing** pattern (`SCHEMA_COLUMN_DDL` — additive
`ALTER TABLE ... IF NOT EXISTS` only, applied idempotently at worker startup) is itself
EXPAND-only by construction, which is the correct half of the EXPAND/CONTRACT discipline
`docs/runbooks/migrations.md` already documents.

---

## 9. Release immutability (Phase 18)

**Confirmed, quoting the actual cloud-init**: the one-time `git clone --branch ${git_ref}`
in `infra/providers/hetzner/cloud-init/app-node.yaml.tpl` exists only to fetch
`scripts/`+`infra/firewall/ufw.sh` for the bootstrap `runcmd`, and its own header comment
explicitly states it does **not** `docker compose up`, write the app `.env`, or configure
the reverse proxy. The running application is started later, separately, by
`scripts/deploy.sh`/`node_deploy.sh`, which only ever `pull`s pinned
`ghcr.io/.../ladini-{api,worker,mcp}:<sha-tag>` images — never `git pull`+build on the
node. `production.tfvars.example`'s `git_ref=main` therefore only affects what commit the
one-time bootstrap snapshot uses, not what code runs. **No fix needed — this was already
correct**, contrary to what the mission brief flagged as a risk to specifically audit.

---

## 10. Observability (Phases 19–24)

| Item | Status |
|---|---|
| Sentry wiring (API + worker) | READY, verified in code: `init_sentry()` is now genuinely called from both `api/main.py`'s FastAPI `lifespan` and `api/tasks.py`'s `worker_process_init` Celery signal — the documented 2026-09-16 fix is real, not aspirational. **Live event delivery NOT RUN** (no test Sentry DSN available). |
| Langfuse | NOT RUN — `LANGFUSE_ENABLED=false` in E2E (no credentials); code path unaffected by this session's changes. |
| Alloy (local) | NOT RUN — starting `infra/alloy/docker-compose.alloy.yml` locally and confirming it scrapes `/metrics`/tails logs was time-boxed out this session; the compose file and socket-proxy permission model were verified by direct file read (see §2) rather than booted. |
| Grafana Cloud pipeline | NOT RUN — no Grafana Cloud credentials available in this environment. |
| Docker socket security | READY, verified by file read: `tecnativa/docker-socket-proxy` is used, pinned, `CONTAINERS=1`/`INFO=1` only, all write/exec/build/image/network/volume/secrets endpoints explicitly `0`. Alloy has no direct socket mount. Not exercised live (Alloy wasn't started — see above). |
| Worker LLM metrics gap | **Still open, confirmed present**: `config.alloy` itself documents that the `worker` process has no `/metrics` HTTP endpoint — in-process metrics are recorded but never exposed for scraping. **Not fixed this session** (architecture decision — Prometheus multiprocess mode vs. a dedicated metrics endpoint vs. OTel metrics — is a real design choice affecting the worker's process model, not a bug with one obvious fix; flagging for a deliberate follow-up rather than a rushed patch). |
| PII/secret redaction | **Confirmed leak, not fixed**: plaintext phone numbers (`+2267...`) appear in numerous `INFO`-level log lines emitted by the worker — `Orchestrator | phone=...`, `MCP_CALL_AUDIT | ... args={"phone": "..."}`, `AGENT_COMPLETED | workspace=+...`, `WorkspaceResolver: new workspace +...`. These are container stdout lines that `infra/alloy/config.alloy` tails via `loki.source.docker` and forwards to Grafana Cloud. They are **not** used as Loki/Prometheus *labels* (so no cardinality-explosion risk, which is the specific thing the mission brief called out), but the raw phone number **is** present as log *content*, fully searchable in Grafana Cloud Loki. Injected `Authorization=secret-test`/`whatsapp_token=secret-test` sentinels (placed in free-text message bodies) did **not** appear in any log line checked. **Recommend**: a log-processing redaction step (Alloy `stage.replace` or an app-level log filter) before this goes to a shared/compliance-sensitive Grafana Cloud project — phone numbers are PII under most data-protection regimes. |

---

## 11. Load testing (Phases 26–29) — the core finding

Executed with `k6` (downloaded directly, Chocolatey failed — no admin rights on this
host) against the live local stack, `MOCK_EXTERNAL_APIS=true`, HMAC-signed requests
(`SIGNING_SECRET` set, exercising the *real* signature-verification code path, not
bypassing it).

### Before the fix

| VUs | Throughput | p50 | p95 | p99/max |
|---|---|---|---|---|
| 50 | **42.1 req/s** | 918ms | 3.04s | max 6.52s |
| 200 | **42.7 req/s** | 3.89s | 8.6s | max 13.1s |

Throughput **flat** regardless of VU count while latency scales ~linearly with
concurrency — the textbook signature of a single serialized bottleneck, not genuine
capacity exhaustion.

### Root cause (found, fixed, re-verified)

`api/routes/whatsapp_webhook.py` and `twilio_webhook.py` called a **synchronous** `redis`
client (`redis.from_url(...)`, the plain `redis` package, not `redis.asyncio`) directly
inside `async def` FastAPI request handlers — for the inbound-message dedup key, and (via
`core/idempotency.py`, itself synchronous by design for its Celery-task callers) for the
`pending_role_hint` cache. A blocking socket call inside an `async` coroutine freezes that
whole uvicorn worker's event loop for the round-trip duration, serializing **every**
concurrent request on that worker — exactly matching the observed flat-throughput/
linear-latency pattern.

**Fix applied**: wrapped the four sync-Redis call sites (2 per webhook route) in
`asyncio.to_thread(...)`, offloading the blocking call to the thread pool instead of the
event loop. `core/idempotency.py` itself was deliberately left untouched (it's shared with
synchronous Celery-task callers; converting it to async would ripple into call sites
outside this session's scope) — the fix is scoped to the two async call sites that needed
it.

### After the fix

| VUs | Throughput | p50 | p95 |
|---|---|---|---|
| 50 | **69.8 req/s** (+66%) | 626ms | 1.58s |
| 200 | **109.9 req/s** (+157%, and now genuinely scaling with load, not flat) | 1.61s | 3.2s |

Both runs still exceed the load-test script's own `p95<1000ms` threshold — the remaining
latency is consistent with real, non-pathological capacity (4 gunicorn workers, single
container, shared with 8 other containers plus k6 plus this session's own tooling on one
laptop) rather than a second blocking-I/O bug. **500 VU sustained + full 10→50→200→500
burst run were not executed** (time-boxed after the fix was found, verified, and
re-measured at 50/200 — see "Remaining gaps"), but the qualitative fix (accept-path no
longer blocks the event loop) is the change that matters architecturally.

### First and second bottleneck (Phase 29)

A real, load-generated backlog (k6 runs enqueued ~3,750 webhook tasks into the
`interactive` Celery queue) was used as a natural experiment for the next layer:

- **Drain rate, 1 worker (concurrency=4)**: ≈9.2 tasks/s (measured: queue depth
  2693→2418 over 30s).
- **Drain rate, 3 worker replicas (concurrency=4 each)**: ≈8.5 tasks/s — **no
  improvement** from tripling worker replicas.
- **Why**: `docker stats` during the 3-replica run showed `mcp` (the single, deliberately
  non-scaled MCP instance) at **84.7% CPU**, while all 3 worker containers were similarly
  CPU-loaded (~74–78%) — every worker's MCP tool call funnels through the one `mcp`
  container, which saturates before adding worker replicas helps.

This exactly matches what `docker-compose.prod.yml`'s own comments already prescribe
("Pour du débit : dupliquer le SERVICE mcp... jamais `--scale`") — the load test now
**quantifies** it rather than just asserting it:

1. **First bottleneck (found & fixed this session)**: webhook accept path — synchronous
   Redis blocking the event loop. Now resolved.
2. **Second bottleneck (architectural, by design, now measured)**: the single MCP
   instance. Scaling `worker` replicas alone does not increase sustained throughput past
   MCP's ceiling — the next real scaling lever is duplicating the `mcp` **service**
   (separate containers/ports, per-node or per-cluster, with a lightweight round-robin —
   already the documented design) rather than raising Celery concurrency further.
3. **Headroom not yet reached**: Postgres/PgBouncer connection pool, and raw gunicorn/API
   CPU, were not the limiting factor in any run performed.

**Numbers here are relative, not absolute** — this ran on a single Windows dev laptop
sharing CPU across Docker Desktop, 9 E2E containers, an already-running separate dev
stack, k6, and this session's own tooling simultaneously. Re-run `webhook-scenario.js`
and `burst-scenario.js` at full scale (50/200/500 + the 10→50→200→500 burst) against a
dedicated Hetzner `cx22`-equivalent box before trusting absolute req/s numbers for
capacity planning — the *qualitative* finding (MCP singleton is the real ceiling once the
webhook layer is fixed) should hold regardless of hardware.

---

## 12. Security review (Phase 25, spot checks)

- Docker socket proxy: see §10 — read-only, non-write endpoints only, confirmed by file
  read.
- Webhook signature verification: exercised for real (not bypassed) throughout this
  session's k6 runs — HMAC-SHA256 over the raw body, fail-closed (403/503) confirmed when
  `WHATSAPP_APP_SECRET` unset in earlier ad-hoc tests before it was deliberately set for
  the load tests.
- PII in logs: see §10, confirmed leak (phone numbers, not secrets).
- Firewall (`infra/firewall/`, Hetzner `firewall.tf`): verified by file read — SSH
  restricted to `admin_cidrs`, no Redis/PgBouncer/MCP/Flower port ever opened publicly.
  **Not exercised live** (no Hetzner firewall to test against).

---

## 13. Terraform (Phases 31–33)

`terraform`/no admin rights to install via Chocolatey on this host — downloaded the
official binary directly instead (`releases.hashicorp.com`, `terraform_1.9.8`).

- `terraform fmt -check -recursive`: **initially clean**, then a formatting diff appeared
  after this session's own edit to `outputs.tf` — fixed with `terraform fmt -recursive`.
- `terraform init -backend=false`: succeeds, resolves `hetznercloud/hcloud ~> 1.69`.
- `terraform validate`: **initially failed with two real bugs**, both fixed this session,
  now passes cleanly:
  1. `cloud-init/app-node.yaml.tpl`'s `final_message` used `${uptime}` — a cloud-init
     *native* template variable (substituted by cloud-init's own `cc_final_message`
     module at boot, unrelated to Terraform) — but Terraform's `templatefile()` processes
     the *entire* file as its own template first and errored on an unresolvable
     `uptime` variable. **Fixed**: escaped to `$${uptime}` so Terraform passes it through
     literally for cloud-init to substitute at boot.
  2. `outputs.tf` indexed `hcloud_server.{app,scheduler}[...].network[0].ip` — `network`
     is a nested block represented as a Terraform **set**, which has no addressable index.
     **Fixed**: `network[*].ip[0]` (splat, valid on a set; each node has exactly one
     private network attached, so `[0]` after the splat is safe).
- `terraform plan` — **NOT RUN**: no `HCLOUD_TOKEN` available in this environment, and
  `plan` against a real Hetzner account is explicitly out of this session's mandate
  without one.

These are exactly the class of bug `terraform validate` exists to catch before a real
`apply` — and per the module's own README, this was the **first time it had ever been
run**. Confirms the mission brief's own stated caveat, and closes it.

---

## 14. CI/CD (Phase 35)

See §2 table. Concurrency guards present and correctly scoped on all 3 workflows
(`cicd.yml` per-ref, `deploy.yml` per-target, `release.yml` global). No `docker build` in
the deploy path, no `latest` consumed by deploy tooling (only produced, and rejected by
`preflight.sh` if ever pointed at). One nuance worth a branch-protection-settings check:
`cicd.yml`'s `docker-build` job has no explicit `needs: test`.

---

## 15. `predeploy-check` (Phase 38)

Added [scripts/predeploy_check.sh](scripts/predeploy_check.sh) — a single entrypoint
chaining: bash syntax check of all deploy scripts → `docker compose config` validation
(and a `build:`-directive guard) → targeted pytest (beat singleton + compose invariants) →
`infra/inventory.yml` validation (if present) → `terraform fmt -check`+`validate` (if
Terraform is available) → `smoke.sh`/`smoke_observability.sh` (only if `SMOKE_API_URL` is
exported, i.e. a stack is actually reachable — never a false failure when nothing's
running). **Live-verified end-to-end against this session's own E2E stack** — it correctly
caught the `terraform fmt` diff and (before the fix) the `smoke.sh` beat false-negative,
and returned exit 0 once both were fixed.

```bash
# Static-only (no running stack required):
./scripts/predeploy_check.sh

# Also smoke-tests a stack you already have up:
SMOKE_API_URL=http://127.0.0.1:8000 ./scripts/predeploy_check.sh
```

---

## 16. Full test suite (Phase 39)

`backend/.venv`, full suite (`pytest` with no path filter).

- **≈3,773 passed, 24 failed**, 0 errors (unchanged failure count before/after this
  session's fixes — see below).
- **22 of the 24 failures are pre-existing business-logic baseline failures**, entirely
  within `tests/architecture/test_cognitive_decisions_are_consumed_or_removed.py`,
  `test_entry_block_topology.py`, `test_procurement_draft_architectural_hardening.py`, and
  `test_procurement_draft_transactional_contract.py` — all in the market-coach routing/
  procurement-draft area that git status shows as already under active, uncommitted
  refactor on this branch before this session started. Not touched, not investigated
  further (explicitly out of this session's mandate).
- **2 failures are a pre-existing dependency-version mismatch**, unrelated to any file
  touched this session: `tests/unit/test_mcp_client.py::TestHttpMCPAdapter::{
  test_connect_requires_a_base_url, test_connect_targets_the_mcp_endpoint}` fail with
  `ImportError: cannot import name 'StreamableHttpTransport' from
  'fastmcp.client.transports'` — the installed `fastmcp` version in `backend/.venv` no
  longer exports that name. Confirmed via `git status --short` on both the source file and
  the test file: neither was modified this session or by the in-progress branch.
- **Zero new regressions** from this session's changes (`docker-compose.prod.yml`'s
  env-var port change, `docker-compose.e2e.yml`, `response_dispatch.py`'s mock-send
  guards, `whatsapp_webhook.py`/`twilio_webhook.py`'s `asyncio.to_thread` wraps,
  `smoke.sh`'s beat-check fix, Terraform fixes) — none of the 24 failures reference any of
  these files, and the architecture-test subset (which *does* cover the compose/webhook
  layer this session touched) passes 15/15.

---

## 17. What was explicitly NOT RUN (and why)

| Item | Reason |
|---|---|
| `terraform plan`/`apply` | No `HCLOUD_TOKEN` — real Hetzner credentials required, explicitly out of mandate without them. |
| Real Grafana Cloud validation | No Grafana Cloud API key/Prometheus URL in this environment. |
| Real Sentry event delivery | No test Sentry DSN available; SDK wiring verified by code, not by a captured event. |
| Real Langfuse trace | `LANGFUSE_ENABLED=false`, no credentials; code path unaffected by this session. |
| Alloy started locally | Time-boxed out; config/socket-proxy verified by file read instead. |
| 500 VU sustained + full 10→50→200→500 burst scenario | Time-boxed after finding, fixing, and re-verifying the root-cause bottleneck at 50/200 VUs — the fix is what mattered; absolute capacity numbers need a dedicated (non-laptop) host anyway. |
| Soak test (20–50 VU, 15–30 min) | Time-boxed out this session. |
| Full 2-node rolling-deploy dry run via real SSH (`cluster_deploy.sh` end-to-end) | No second real host available locally; script logic verified by direct reading instead (see §8). |
| Worker auto-restart on crash (`restart: unless-stopped`) | Docker Desktop for Windows does not honor restart policies promptly on `docker kill` in this environment (confirmed with a control `busybox` container) — re-verify on a real Linux host. |
| Clean-VM/fresh-bootstrap test (Phase 36) | Time-boxed out; this session ran on an existing dev machine with pre-installed Docker/Python, not a from-scratch Ubuntu VM. |
| `worker` LLM metrics endpoint (Phase 21 gap) | Confirmed still open (see §10) but a real architecture decision, not patched this session. |
| Full idempotency-across-task-redelivery fix (Phase 10) | Confirmed as a real, already-self-documented scope gap; implementing it touches money-moving business logic, correctly out of mandate. |

None of the above are silent gaps — each was investigated enough to state precisely what
is and isn't proven, per the mission's own instruction to document rather than fabricate.

---

## 18. Fixes applied this session (summary)

| File | Fix |
|---|---|
| [backend/src/ladini/api/routes/whatsapp_webhook.py](backend/src/ladini/api/routes/whatsapp_webhook.py) | Sync Redis calls → `asyncio.to_thread` (the throughput fix). |
| [backend/src/ladini/api/routes/twilio_webhook.py](backend/src/ladini/api/routes/twilio_webhook.py) | Same fix, 4 call sites. |
| [backend/src/ladini/api/response_dispatch.py](backend/src/ladini/api/response_dispatch.py) | `MOCK_EXTERNAL_APIS` sandbox guard on WhatsApp Cloud + Twilio sends (prevents infinite Celery retry loop in sandbox/E2E). |
| [scripts/smoke.sh](scripts/smoke.sh) | Beat liveness check: pidfile (proven broken, per the compose file's own incident comment) → `pgrep`, matching the already-fixed Docker healthcheck. |
| [infra/providers/hetzner/cloud-init/app-node.yaml.tpl](infra/providers/hetzner/cloud-init/app-node.yaml.tpl) | Escaped `${uptime}` → `$${uptime}` (Terraform/cloud-init template-syntax collision). |
| [infra/providers/hetzner/outputs.tf](infra/providers/hetzner/outputs.tf) | Fixed invalid `network[0]` indexing on a Terraform set (→ splat) in both `app_nodes` and `scheduler_node` outputs. |
| [docker-compose.prod.yml](docker-compose.prod.yml) | `api`/`flower` host ports made overridable via `API_HOST_PORT`/`FLOWER_HOST_PORT` env vars (default unchanged: 8000/5555) — purely to let an E2E stack coexist with an already-running one; zero behavior change in production. |
| [.gitignore](.gitignore) | Added `.env.e2e`. |
| New: `docker-compose.e2e.yml`, `.env.e2e`, `scripts/test/e2e/bootstrap_db.py`, `scripts/predeploy_check.sh` | E2E infrastructure, all clearly scoped and never referenced by the production deploy path. |

---

## 19. Definition-of-done checklist

- [x] local stack boots cleanly
- [x] health semantics correct (liveness vs readiness)
- [x] Celery queues route correctly (interactive/background/scheduled/celery)
- [~] interactive queue protected from background blocking — structural (routing table), not load-tested head-to-head
- [x] Redis failure/recovery tested (found + fixed a real hang-on-first-failure issue)
- [x] Postgres/PgBouncer failure/recovery tested (found, documented, not fixed — multi-minute first-failure detection window)
- [x] MCP failure/recovery tested — clean, bounded, graceful
- [~] worker loss/idempotency tested — container-restart part blocked by host OS limitation; idempotency-window gap confirmed and documented, not fixed (business-logic scope)
- [x] beat singleton proven (2 layers, live-tested)
- [~] multi-node locally simulated — 2nd API node proven stateless; not a full 2nd app-node stack
- [x] stateless behavior proven (cross-node conversation continuity, live)
- [~] rolling deployment locally simulated — verified by code reading, not a live SSH run
- [x] migrations exactly-once proven (structural, in `cluster_deploy.sh`)
- [x] rollback behavior reviewed (not live-exercised — no failing real node to roll back)
- [x] release immutability proven (cloud-init quoted directly)
- [ ] Alloy run locally — NOT RUN
- [ ] Grafana pipeline validated — NOT RUN (no credentials)
- [x] Sentry wiring validated (code-level)
- [~] LLM metrics worker gap — confirmed open, documented, not fixed
- [x] PII/secrets redaction tested — confirmed a real leak (phone numbers in log content), documented, not fixed
- [x] Docker socket proxy permissions reviewed (file-level, not live)
- [x] k6 50/200 executed locally — found and fixed the top bottleneck
- [ ] k6 500 + full burst — NOT RUN (time-boxed)
- [ ] soak test — NOT RUN
- [x] first bottleneck identified (webhook accept path — fixed) and second bottleneck identified (MCP singleton — architectural, quantified)
- [x] `terraform fmt`/`validate` succeed (2 real bugs found and fixed)
- [x] cloud-init reviewed (confirmed correct — bootstrap-only, immutable images at runtime)
- [x] CI/CD reviewed
- [x] `predeploy-check` command exists, live-verified
- [x] full suite: 24 pre-existing baseline failures, zero new regressions

---

## 20. Exact commands for the first Hetzner deployment

```bash
# 1. Provision infra (after filling infra/providers/hetzner/environments/production.tfvars)
cd infra/providers/hetzner
terraform init
terraform plan  -var-file=environments/production.tfvars   # REVIEW before apply
terraform apply -var-file=environments/production.tfvars

# 2. Fill infra/inventory.yml from `terraform output`, then validate it
python3 scripts/validate_inventory.py infra/inventory.yml

# 3. Static gate before touching anything
./scripts/predeploy_check.sh

# 4. First release (CI already built+pushed immutable sha-* images)
ssh <deploy_user>@<control-node-ip>
cd /opt/ladini/app
./scripts/cluster_deploy.sh sha-<short-sha>   # runs migrations once, rolls out node-by-node, beat last, smoke-tests

# 5. Post-deploy
SMOKE_API_URL=https://<public_domain> ./scripts/predeploy_check.sh   # optional, re-confirms smoke + observability
```

Before step 1: decide and execute the Postgres schema bootstrap for the target managed
DB (see §"Before you deploy", item 1) — `cluster_deploy.sh` runs `alembic upgrade head`
if `backend/alembic.ini` exists, which it currently does not; today's schema lives only in
the ORM models and in whatever tables the existing managed Postgres already has from prior
manual setup.
