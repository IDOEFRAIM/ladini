# Commercial Pricing — Final Pilot-Readiness Certification

Date: 2026-09-29. Scope: Phases A, B1, B2a, B2b, B2c.1–B2c.5 of the commercial-pricing series.
This report follows the mission's central rule — readiness is not "unit tests pass," it is
*what the user says = what the system understands = what the user confirms = what is persisted =
what is executed = what is shown later*, across WhatsApp, Web, PostgreSQL, legacy data, retries,
and concurrency. Every phase below states what was actually verified, how, and what was not run
(and why), rather than assuming green.

## Verdict

**COMMERCIAL PRICING PILOT NOT READY.**

One P0 blocker (Phase 2/36) and one landing gap (Phase 0/1) must close first. Everything else
audited in this pass is either solid or already documented as an accepted, deliberate tradeoff.

---

## 1–2. PR graph, merge order, writer audit

**Critical finding, fixed in this pass**: PRs #45 (B2c.3), #46 (B2c.4), #47 (B2c.5) all show
**MERGED** on GitHub — but #46 and #47 were stacked (base = the previous phase's branch) and were
merged into their *parent branches*, never retargeted to `main` before merging. `main` currently
contains only B2c.3. All of B2c.4's buyer-display fixes and B2c.5's procurement/recurring fixes are
missing from `main` despite showing as merged. Fixed by reconciling the branch (which also required
merging in PR #44's independently-merged content, avoided losing
`generate_web_pricing_parity_vectors.py`) and opening **PR #48** (`b2c5 branch → main`), verified
clean via `git diff --stat`, full suite green. **Not merged automatically — awaiting review.**

**Writer audit** (full backend scan for `price=`/`unit_price`/`offered_price`/`max_price_per_unit`/
`total_amount`/`pricing_snapshot`/`commercial_pricing`/`pricing_tiers` + every `insert`/`create_*`/
`award*`/`allocate*` call site):

| Writer | Classification | Note |
|---|---|---|
| `create_product` | CERTIFIED | `certify_commercial_offer` before write |
| `update_product_price_and_qty` | LEGACY_ONLY (by design) | invalidates stale snapshot on edit, never fake-certifies |
| `declare_future_production` | CERTIFIED | B2c.3 |
| `create_auction` (procurement) | CERTIFIED (upstream) | derived via `offer_execution_payload`, not enforced at DB layer |
| **`update_auction_fields`** | **LEGACY_ONLY / gap** | buyer can change an auction's price ceiling, zero basis check |
| **`initiate_negotiation_session`** | **LEGACY_ONLY / P0 gap** | second, parallel Auction-creation path, raw float, no `CommercialOffer` at all |
| **`update_negotiation_offer`** | **LEGACY_ONLY / P0 gap** | same root cause, separate MCP tool |
| `place_bid` / `update_bid_price` | CERTIFIED | `bid_snapshot_columns`/`reprice_bid_columns` |
| award / winner selection | CERTIFIED | `award_total_and_snapshot` |
| `finalize_multi_order` / `create_preorder_draft` | CERTIFIED | `order_item_snapshot_columns` |
| RECURRING allocation → order | CERTIFIED | same builder, with the allocation's own price as an override |
| `record_sale` | LEGACY_ONLY (by design) | already-completed sale, TOTAL_LOT bookkeeping, no ambiguity possible |
| escrow (`initiate_escrow_payment`/`mark_escrow_paid`) | DISPLAY_ONLY | copies an already-certified total, never decides one |
| `StandardPrice` | OUT_OF_SCOPE | no writer in this repo (admin-seeded) |
| `add_expense` / `delivery_fee` | OUT_OF_SCOPE / DISPLAY_ONLY | unrelated to marketplace pricing / static 0.0 |

**The P0 finding**: `initiate_negotiation_session` (`services/database/buyer.py:1993`) and
`update_negotiation_offer` (`buyer.py:2672`), reached via `flows/buyer/negotiation.py`, take a raw
`offered_price: float` straight from conversational extraction with **no** `CommercialOffer`/
`PriceBasis`/provenance check — confirmed by reading `flows/buyer/negotiation.py::_initiate_negotiation`,
which does `offer = float(payload.get("price"))` with zero basis handling, the same pattern that
caused the original procurement bug this series fixed. If a buyer negotiates "4 millions pour tout,"
it is stored as `max_price_per_unit = 4,000,000` (assumed per-unit) — the identical 10x+
misinterpretation risk B2c.5 closed for `PROCUREMENT_CREATE_REQUEST`, in a sibling flow B2c.5 never
touched. `update_auction_fields` has the same gap (ownership check is present — this is a pricing gap,
not an authorization gap).

Per the mission's own instruction not to auto-fix a new-scope discovery without agreement: **this
was not fixed in this pass.** Recommended fix mirrors B2c.5 exactly (route through
`_COMMERCIAL_OFFER_GOALS`, derive via `offer_execution_payload`, certified confirmation) — sized
similarly to the procurement fix, i.e. its own phase (call it B2c.6) rather than a quick patch.

## 3. Reader audit

Prior phases' fixes spot-checked and confirmed intact (buyer search, cart, menus, confirmations,
proactive alerts, procurement/recurring confirmations — see B2c.4/B2c.5 reports for the exhaustive
list). The negotiation flow's *display* of producer bids (`negotiation.py` lines ~55-70) is already
safe (`pricing_label`, B2b-certified) — only the *write* side (buyer's own offer) is the gap above.
A broader reader sweep (admin analytics endpoints, remaining `workers/outbox/templates.py`
functions, order-history views) was attempted via a dedicated audit agent but the agent
infrastructure stalled/errored twice during this run; not completed to the same exhaustive depth as
the writer audit. Recommend a follow-up pass specifically on: `api/routes/analytics_admin*.py`
(admin GMV views), remaining `templates.py` render functions beyond `_render_new_product_alert`.

## 4–5. Migration chain, schema parity

No local PostgreSQL available in this environment (confirmed pre-existing constraint, see B2c.3/4/5
reports). Static checks only:
- Migration files are additive-only for the tables this series touches; no destructive migration in
  the B1→B2c.5 chain.
- `Auction` still has no `pricing_snapshot`/basis column (confirmed — matches the B2c.5 no-migration
  decision).
- `tests/schema/test_schema_contract_static.py::test_sqlalchemy_mirrors_drizzle_exactly` exists and
  is a pure static comparison (no DB) — it would catch a Drizzle/SQLAlchemy drift; confirmed present
  and would run in CI.
- CI does run a real `postgres:16` service container for `tests/schema/` (confirmed in
  `.github/workflows/cicd.yml`) — this is the authoritative gate for Phase 4/5, not this local
  environment. Not independently re-verified against a fresh install in this pass; recommend
  confirming the CI schema-test job is green on PR #48 before merging.

## 6–7. Data-quality / taxonomy audits

Both requested read-only audit scripts already existed from earlier phases
(`docs/domain/sql/pricing_persistence_data_quality_READONLY.sql`,
`docs/domain/sql/taxonomy_pilot_audit_READONLY.sql`) — extended the former in this pass with two
queries for the B2c.5 writers (`marketplace.auctions`, `marketplace.recurring_needs`), which were
gaps against this phase's own ask. Neither script was executed against any real database (no access,
and doing so on a live/staging DB requires human authorization per the mission's own instruction) —
they are ready for a human to run.

## 8–9. WhatsApp E2E, real LLM

The embedded smoke-test suite (`run_manual_smoke_tests`, exercising the real compiled graph with all
real nodes) was **red on CI** going into this pass — `PRODUCTION_DECLARE_FUTURE`'s demo scenario
never provided the price provenance the B2c.3 gate requires. Fixed and verified green (see commit
`535b1f6`, propagated through the stack). Golden A–N from the mission's flow matrix are covered
piecemeal across the domain-level test suites for each phase (SALES_PUBLISH/PRODUCTION_DECLARE/
PROCUREMENT/PREORDER/RECURRING all have their own golden tests, listed in each phase's own report),
but were not replayed as one continuous WhatsApp session in this pass.

**Real LLM test (Phase 9) was not run** — this environment has no exercised live LLM credential in
this session, and the mission's hard cases ("500f le sachet," "4 millions pour tout," mid-confirmation
correction) need one. All prior phases' "goldens on the real compiled graph" tests use a *scripted*
LLM stand-in, which proves the domain/graph wiring but not actual model extraction quality. Recommend
running this explicitly before pilot, ideally against the same model/prompt version that will be
live.

## 10–17. Correction-at-confirmation, stale state, duplicates, pagination, package/TOTAL_LOT/unit E2E, legacy

Covered by existing golden tests per-phase (e.g. B2c.3's Golden E "finalement 3,5 millions pour tout"
switches PER_BASE_UNIT→TOTAL_LOT with old basis purged; B2c.4's `test_...corrupted_raw_fields_never_leak`;
B2c.5's PER_PACKAGE preorder end-to-end). Not independently re-run as a single continuous scenario in
this pass — the domain-level proofs exist and passed in each phase's own test run; a full
conversational replay through the real graph for all of A–N in one session was not performed here
(time/scope; would benefit from being scripted once as a dedicated `tests/integration/` suite rather
than re-verified manually each certification pass).

## 18–19. Idempotency, concurrency

Existing test files confirmed present: `tests/architecture/test_execution_idempotency_key.py`,
`tests/unit/test_mcp_idempotency.py`, `tests/integration/test_recurring_need_execution_idempotency.py`,
`tests/architecture/test_procurement_draft_persistence.py` (real-thread CAS concurrency test, per its
own docstring), `tests/schema/test_need_matching_service.py`, `tests/schema/test_sales_invariants.py`.
These require real Postgres and run in CI, not locally. Per memory from the Procurement CAS
persistence phase, this exact test class already found and fixed 2 real concurrency bugs previously —
no new concurrency work was done or needed in B2c.4/B2c.5 (neither touched locking/CAS code).

## 20. Price-change races

B2c.4 confirmed direct-purchase's policy (live price at execution, no reconfirmation, deliberate).
B2c.5 confirmed preorder's *different*, also-deliberate policy (frozen at INIT, a reservation
guarantee). Both documented, both intentional, both explicitly confirmed with the repo owner in
their respective phases — this is not an inconsistency to "fix," it's two different products
(spot order vs. reservation) with two different, sound policies. Procurement/award races not
independently re-tested this pass; existing CAS/idempotency-key architecture (Phase 18-19) is the
mechanism that would need to hold under a real race and wasn't touched by this series.

## 21. Stock shortage

`StockShortageDecision` (TAKE_AVAILABLE/START_TENDER/CANCEL) predates this series (see prior "Buyer
direct purchase flow" memory) and was not touched by B2c.3–B2c.5. Not re-verified in this pass.

## 22. Web E2E

No Playwright/Cypress config found in `ladinifront`. Not run. `ladinifront` PRs #13/#14/#15 (schema
carry-forward, web bid/award, web award UX) were confirmed OPEN at the start of B2c.3 and not
re-checked in this final pass — recommend a fresh `gh pr list` on that repo before pilot, matching
this report's own "don't trust prior reports" principle for the backend PRs.

## 23. Observability

Confirmed present: `COMMERCIAL_OFFER_*` (7 occurrences), `BID_PRICING_*` (6), `BID_AWARD_*` (8),
`FUTURE_OFFER_*` (5), `PROCUREMENT_PRICING_*` (3), `PRICE_BASIS_*` (7). **Gap found and fixed**:
B2c.4's own mission text asked for `BUYER_PRICING_VIEW_CERTIFIED`/`_LEGACY` and related buyer-search
events, but these were never actually added during B2c.4. Added in this pass — one aggregate log
line per search call (`BUYER_PRICING_VIEW_CERTIFIED`/`_MIXED` with a masked phone suffix, result
count, certified/legacy split), never per-row (volume) and never the raw search query text.

## 24. Error handling

Not independently tested this pass (DB timeout / MCP timeout / Redis unavailable / LLM unavailable
injection). Existing degraded-mode logging (`*_DB_UNAVAILABLE_FALLBACK_TO_CACHE`,
`*_PERSISTENCE_UNAVAILABLE | ... | DEGRADED`) is visible in the pasted CI log from this same session
and behaves as designed — draft persistence fails soft to cache-only mode rather than crashing the
turn, and logs loudly rather than silently.

## 25. Redis/Celery local env issue — root cause identified

Expected local dev config (`.env.example`): `REDIS_URL=redis://localhost:6379/1`, no credentials.
This machine's actual `backend/.env` has a `rediss://` URL pointing at a remote host with embedded
credentials that fail to parse (`urlparse` chokes on the value — same failure class as a documented
prior incident: `.github/workflows/cicd.yml` runs a dedicated
`test-preflight-redis-url-parseable.sh` referencing "incident réel sha-efc4ff8, 2026-09-20,
migration Upstash → Valkey"). CI's test job has **no Redis service container at all** (confirmed in
`cicd.yml` — only `postgres:16`); it relies on the code default (`redis://localhost:6379/0`, which
`.env` normally shouldn't override) that parses fine even with nothing listening on that port, since
`redis.from_url()` only constructs a client lazily. **Root cause: a stale/broken `REDIS_URL` override
in this one developer machine's local `.env`, unrelated to CI, staging, or production.** Not modified
(credential-bearing line, out of scope for autonomous edits) — recommend the machine owner either
delete the override (falls back to the safe default) or point it at a real local Redis. **CI remains
the authoritative gate**, as the mission's own fallback instruction anticipates.

## 26. Security / ownership

Ran all 5 existing ownership/IDOR test files (63 tests) — green, confirming no regression from the
pricing refactor series (none of B2c.3/B2c.4/B2c.5 touched an ownership-check code path). Spot-checked
`update_auction_fields`'s docstring/implementation: ownership control ("contrôle de propriété") is
present and unaffected — its gap (found in Phase 2) is a pricing-basis gap, not an authorization one.

## 27. Money precision

`CommercialPricingSnapshot` uses `Decimal` throughout (never `float`) for `commercial_price_amount`/
`normalized_unit_price`; rounding policy is `ROUND_HALF_UP` at 4 decimal places for normalized values,
2 for money (`quantize_money`/`quantize_normalized`), applied at snapshot-construction time. The
module's own docstring documents the exact hard case from this mission ("1 000 000 / 3 kg =
333333.3333") as a worked example. `TestDecimalPrecision` in `test_commercial_pricing_snapshot.py`
covers exact and non-exact division, and that the commercial total is never altered by normalization
rounding — confirmed passing (part of every full-suite run this session). Not independently
re-verified: actual float precision at the asyncpg/psycopg2 driver boundary (would need a real DB
connection to observe).

## 28. Full backend CI

**Was red** at the start of this phase (`test_all_manual_smoke_flows_complete` on PR #45's CI run) —
root-caused and fixed (commit `535b1f6`, propagated to #46/#47/#48's branches). **Found red a second
time** after propagating: `test_price_basis_conflict.py` had a test asserting the *old*
`reconcile_price_basis` contract for `PROCUREMENT_CREATE_REQUEST`, now superseded by the
`CommercialOffer` gate; and the `new_task_prompts.py` guardrail text pushed the interpreter's
system-prompt anti-regression budget over its threshold. Both fixed (commit `e15eb0e`) — see Phase 9
of the B2c.5-era report structure inline in that commit. Locally verified: full `tests/unit` +
`tests/architecture` + `tests/nodes` + `tests/services` suite, plus the specific previously-failing
tests. **Third CI run not yet observed** as of this report (push landed shortly before writing this) —
confirm PR #48's CI is green before merging.

## 29. Full frontend CI

Not run — no active work landed in `ladinifront` this series (B2c.4 confirmed no buyer-facing UI
exists there at all). `tsc`/`vitest`/`eslint`/`drizzle-kit check`/production build were not
re-executed in this pass; recommend running once as part of confirming ladinifront PRs #13-15's
actual state before pilot.

## 30. Deployment order

1. Merge PR #48 (lands B2c.4 + B2c.5 into `main`) — confirm CI green first.
2. Confirm/re-check `ladinifront` PR stack (#13→#14→#15) state fresh (per this report's own
   "don't trust prior reports" finding — verify, don't assume).
3. If the P0 finding (negotiation.py) is fixed first: merge that PR before continuing; otherwise
   restrict pilot scope to exclude the negotiation flow (Phase 32).
4. Apply/verify migrations on the target environment (no new migration expected from this series).
5. Deploy backend.
6. Smoke backend (`test_all_manual_smoke_flows_complete` equivalent, or a manual WhatsApp round-trip
   for each pilot product).
7. Deploy frontend (only if the negotiation/award web flows are in pilot scope).
8. Smoke web.
9. Run the two READONLY data-quality/taxonomy SQL audits against the real DB, review with a human.
10. Enable pilot for the explicitly scoped zones/products (Phase 32).

## 31. Rollback plan

No destructive migration in this series — every migration in the B1→B2c.5 chain is additive
(new nullable columns / new tables), so a backend rollback is a straightforward "redeploy the
previous image," no migration `down` step needed. If a future `Auction.pricing_snapshot` migration
is added later (Phase 30/33's residual-risk item), it should follow the same additive-only
discipline this series already established, and this rollback plan should be revisited then.
Frontend rollback: standard previous-build redeploy, no schema coupling identified.

## 32. Pilot scope (proposed, needs explicit sign-off)

- **Exclude the negotiation flow** (`negotiation.py`, `initiate_negotiation_session`,
  `update_negotiation_offer`) from pilot scope until the P0 fix lands — this is the one flow with a
  live financial-ambiguity bug.
- Pricing bases supported: PER_BASE_UNIT, TOTAL_LOT, PER_PACKAGE (catalog products via
  `pricing_tiers` only — MarketOffer/Auction fail closed on PER_PACKAGE by design).
  Product categories: whatever the taxonomy audit (Phase 7) confirms is configured — maïs, tomate,
  lait, bœuf confirmed covered; extend the regex in `taxonomy_pilot_audit_READONLY.sql` for any
  additional pilot product before go-live.
- Zones/coops/farms/buyers: not decided in this report — business decision, not a technical one.

## 33. Legacy policy

No backfill performed or recommended (matches every phase's explicit "never backfill automatically"
constraint). Legacy rows (`Product`/`MarketOffer` without `commercial_pricing`/`pricing_snapshot`,
`Bid` with `offered_price_basis IS NULL`) read as `LEGACY_PARTIAL`/`UNKNOWN_BASIS` everywhere,
confirmed by design across every `*_pricing_view` function — visible, honest, non-transactional-looking
but not technically blocked from being acted on by existing flows that predate this series. Decision
needed from the business: visible-but-flagged vs. excluded-from-pilot-search for legacy rows: this
report recommends **visible but flagged** (existing behavior), since blocking them outright would be
new scope.

## 34. Taxonomy / seed

No seed PR was needed in this pass — `taxonomy_pilot_audit_READONLY.sql` already covers the mission's
minimum pilot product list. If the audit (run by a human against the real DB) shows gaps, the
recommended next step is a small, explicit seed PR listing exactly which `sub_categories` rows get
`priority_unit`/`allowed_units` set — not prepared in this pass since the audit hasn't been run
against real data yet (no DB access).

## 35. Pilot checklist

See `docs/COMMERCIAL_PRICING_PILOT_CHECKLIST.md` (companion file to this report).

## 36. P0/P1/P2/P3 classification

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | `negotiation.py` ambiguous price writes (3 call sites) | **P0** | Found, not fixed (new scope, reported per mission instruction) |
| 2 | B2c.4/B2c.5 not actually in `main` despite "Merged" PRs | **P0**-adjacent (blocks everything) | Fixed — PR #48 open |
| 3 | CI smoke test red (missing price provenance in demo helper) | P1 (blocked CI, not a live bug) | Fixed |
| 4 | Stale test asserting the pre-B2c.5 procurement contract | P2 (test debt, not a runtime bug) | Fixed |
| 5 | Prompt token-budget regression | P3 (guard threshold, cosmetic) | Fixed |
| 6 | Missing `BUYER_PRICING_VIEW_*` observability (asked for in B2c.4, never added) | P2 | Fixed |
| 7 | Data-quality READONLY audit missing B2c.5's writers | P2 | Fixed |

**Pilot gate requires NO P0, NO P1. Finding #1 is an open P0. Verdict: NOT READY.**

## 37. New anomalies found — stopped, not auto-fixed

Finding #1 above (`negotiation.py`) is exactly this case: discovered during the final writer audit,
clearly real and clearly P0-shaped, but out of this pass's original scope and comparable in size to
a full phase of prior work. Per the mission's explicit instruction, this was reported rather than
fixed. Recommended next step: a scoped "B2c.6" phase mirroring B2c.5's procurement fix exactly.

## 38. Exact unresolved blockers before "PILOT READY"

1. **Fix `negotiation.py`'s three uncertified `Auction.max_price_per_unit` writers** (P0).
2. **Merge PR #48** (lands B2c.4+B2c.5 into `main`) after confirming its CI is green.
3. Confirm `ladinifront` PR stack state fresh (not re-verified this pass).
4. Run the real-LLM difficult-phrasing spot check (Phase 9) at least once before enabling the pilot.
5. Run the two READONLY SQL audits against the real target DB and review with a human (Phase 6/7).
6. Get explicit business sign-off on pilot scope (Phase 32) and legacy policy (Phase 33).
