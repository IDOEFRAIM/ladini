# Analytics — Phase 1 Audit Cartography (2026-09-27)

Read-only audit across both repos (`AgriConnect` backend + `frontag` frontend) commissioned before any analytics implementation. No code was written in this pass. Structure follows the mission's Phase A checklist.

---

## 0. Executive summary

The transactional core is solid and mostly reusable as a source of truth: `Order`/`OrderItem`/`Auction`/`Bid`/`RecurringNeed`/`RecurringNeedOccurrence`/`NeedAllocation` already track the numerators/denominators we need (e.g. `quantity_matched` vs `requested_quantity` as independent columns — no "average of percentages" trap). A transactional outbox pattern already exists and is provably safe to imitate. A per-turn telemetry table (`intelligence.agent_turns` + children) already exists and already has a Next.js admin cockpit reading it — **but that cockpit lives only on an unmerged branch** (`feat/monitoring-cockpit-clean`, pushed to origin, not merged to `master`).

Three structural gaps stand out before we can build Phase 1 of the mission:

1. **No business-fact event table exists anywhere.** `agent_turns` is request/performance telemetry (technical), `notification_outbox` is a delivery-channel queue (transport), `AuditLog`/`solicitations` are unused/adjacent. None is a stable, append-only "an order was paid" / "a tender was won" fact stream. This is the real gap `analytics.business_events` should fill — it does not duplicate anything.
2. **No `canonical_unit`/`measurement_family` exists at the DB level.** The concept exists three times in code (`quantity_unit.py::UNIT_SYNONYMS`, `pricing_tiers.py::_unit_family` → literally returns `"MASS"`/`"VOLUME"`, and a third smaller alias map in `market_coach/utils.py`), and there's exactly one half-wired DB anchor point (`SubCategory.allowed_units`/`priority_unit`, added in migration 0004) whose own code comment says the admin-configured taxonomy "n'existe pas encore en base." This is precisely the gap Phase B should close, and the seed data (MASS/VOLUME split) already exists in `pricing_tiers.py`.
3. **Two unmerged/divergent codebases risk**: `frontag`'s `master` branch looks stale/abandoned relative to `feat/monitoring-cockpit-clean` (843-file diff). Before adding new analytics endpoints/tables to the frontend, we need the user to confirm which branch is the real integration target.

Decisions the user needs to make before Phase B implementation (see §7).

---

## 1. BUYERS / USERS

Source: `backend/src/ladini/domain/identity/models.py`.

- `User` (`auth.users`, `:33-96`): `role` is free-text (not an enum), `zone_id` FK → `governance.zones` (hierarchical: `parent_id`/`path`/`depth`), `declared_location` (raw text, never geocoded — deliberate), `coverage_status` (COVERED/NEARBY/OUT_OF_COVERAGE/WAITLIST), GPS fields, `onboarding_completed`, `account_status`/`blocked_*`.
- **Dual-role is structural, not a flag**: a `User` row can have both a `Producer` (`marketplace.producers`, its own `zone_id`/region/province/commune — a second, redundant location surface) and a `BuyerProfile` (`marketplace.buyer_profiles`, `:247-281`) row simultaneously.
- `BuyerProfile` carries `buyer_type_id` → `BuyerType` lookup, `is_verified`/`trust_badge`/`rating`.
- **`marketplace.clients`** is a producer-owned CRM contact record (name/phone/total_orders/total_spent) with **no FK to `auth.users`** — a second, unlinked "buyer" concept. Must not be unioned into a buyer dimension without care.
- `TrustScore` (`intelligence`, 1:1 on `user_id`) — generic reputation, not buyer-specific.

**(a) Reuse**: `User.zone_id` → `governance.zones` as the ready-made hierarchical geo dimension; `BuyerProfile`/`Producer` split for role-based joins.
**(b) Gaps**: no single canonical "actor" abstraction (a user can be producer + buyer + delivery_agent at once) — analytics event rows must record which identity role acted, not assume one.
**(c) Don't duplicate**: `marketplace.clients` must not silently become a second buyer source; `Order.buyer_id` (via `BuyerProfile`) is the one to anchor on.

---

## 2. CATALOG / TAXONOMY / UNITS

Sources: `governance/models.py` (Category/SubCategory), `catalog/models.py` (Product/MarketOffer/Stock), `domain/quantity_unit.py`, `domain/pricing_tiers.py`.

- `Category` (`governance.categories`): just id/name/description — no taxonomy metadata.
- `SubCategory` (`governance.sub_categories`): `category_id` FK, `blocked_zone_ids`, `minimum_order_quantity/unit`, and — added in migration 0004 — **`priority_unit`/`allowed_units`** (free-text, still no `measurement_family`). This is the intended, half-built anchor point per the code's own docstring in `resolve_product_unit`.
- **Three parallel unit-bearing product-like entities**: `Product.unit` (default KG), `MarketOffer.unit` (default KG, its own `sub_category_id` FK, duplicates category/unit concerns independently), `Stock.unit` (default KG, **no FK to Product/SubCategory at all**). Plus `Product.category_label` (free text) vs `Product.sub_category_id` (FK) — two taxonomy signals that can disagree.
- No `Unit` table anywhere in the 43-table baseline schema.
- **Measurement family already exists in code**: `pricing_tiers.py::_unit_family()` returns literally `"MASS"` / `"VOLUME"`, or the raw unit as its own singleton family (SAC/PANIER/TETE/UNITE — no `"COUNT"` grouping exists yet).
- **Three parallel unit-vocabulary sources** that are not guaranteed to agree: `domain/quantity_unit.py::UNIT_SYNONYMS` (repo-wide "single source of truth" per its own docstring), `market_coach/utils.py`'s smaller display-only alias map, and the free-text DB columns themselves.

**(a) Reuse**: `SubCategory.allowed_units`/`priority_unit` as the DB anchor to extend with `canonical_unit`/`measurement_family`; `pricing_tiers.py::_unit_family` as the seed logic for a `measurement_family` enum; `quantity_unit.py::UNIT_SYNONYMS` as the seed alias table.
**(b) Gaps**: no DB enum for measurement_family; SAC/PANIER/TETE/UNITE need a deliberate decision (stay singleton families, or group into COUNT/PACKAGE).
**(c) Don't duplicate**: do not invent a fourth unit vocabulary. Reconcile `quantity_unit.py` (business logic) with the new DB `canonical_unit` column rather than adding a new independent mapping. Anchor taxonomy on `Product.sub_category_id`, not `category_label`, and treat `MarketOffer`/`Stock` units as legacy surfaces to reconcile later, not new sources of truth.

---

## 3. DIRECT FLOW

Source: `services/database/buyer.py`, `domain/orders/models.py`, `core/geofencing.py`.

- **Search**: `BuyerMixin.search_products` (fuzzy trigram, zone-priority scored). **No search-log table exists anywhere** — queries/results are not persisted today; this is pure forward-only instrumentation territory.
- **Order** (`marketplace.orders`): `status`/`delivery_status`/`payment_status` are free-text Text columns (no DB enum/CHECK) — conventions, not constraints. `order_type` (STANDARD/PREORDER/DIRECT_SALE + auction-linked), `source` (APP/WHATSAPP/AGENT), `checkout_group_id` groups a multi-producer checkout, `auction_id`/`winning_bid_id` link TENDER-originated orders.
- **Checkout**: `finalize_multi_order` (cart → one order per producer) and `create_preorder_draft` (DRAFT status) are the two entry points; `record_sale` is a distinct agent-recorded walk-in sale.
- **Delivery**: dual-tracked, only one side wired. `Order.delivery_status` (a string) is what production code actually mutates. A full `Delivery` model exists (GPS origin/destination, `assigned_at`/`picked_up_at`/`delivered_at`/`failed_at`) but **has zero write sites anywhere in the codebase** — schema-ready, fully un-instrumented.
- **Geofencing** (`is_within_burkina_faso`) is enforced (raises `BusinessRuleException`) but never logged — no telemetry on geofence pass/fail.

**(a) Reuse**: `Order`/`OrderItem`/`Payment` as state source of truth; `OrderStatusHistory` (see §5) as a partially-populated transition log.
**(b) Gaps — instrumentable only from now on**: search query/result logging (none exists); geofence pass/fail events; the `Delivery`/GPS lifecycle (would need either wiring real writers first, or treating it as future work).
**(c) Don't duplicate**: `Order.status`/`.delivery_status`/`.payment_status` are the single source of truth (free-text conventions — new analytics code should read the existing literal vocabulary, not invent a parallel one).

---

## 4. TENDER FLOW

Source: `services/database/auction.py`, `domain/orders/models.py` (Auction/Bid).

- **Auction**: `status` (OPEN/CLOSED/EXPIRED/CANCELLED), `escrow_status`, `winner_bid_id`, `deadline`/`delivery_deadline`, `awarded_at`, `cancelled_at`. No `published_at` (use `created_at`), no `first_bid_at` (derivable via `MIN(bids.created_at) GROUP BY auction_id` — not a hard gap).
- **Bid**: `status` (PENDING/WINNING/LOST/WITHDRAWN), `is_winner` bool with a partial unique index (exactly one winner per auction), `notified_at`, `valid_until`. Bid status transitions overwrite in place — **no historical trail** for WITHDRAWN/LOST (unlike orders).
- **`select_winning_bid`** (canonical impl in `auction.py`) has exactly **two call sites** (`order_tracking.py::_execute_winner_selection` and `negotiation.py::_handle_viewing_offers`), both idempotency-keyed. It row-locks bid+auction, flips winner atomically, bulk-flips losers, and **creates the resulting Order in the same transaction** — but **does not write `OrderStatusHistory`**, so tender-originated orders have an incomplete transition trail from that point on.

**(a) Reuse**: `Auction`/`Bid` state as source of truth; `created_at` as a good-enough `published_at` proxy.
**(b) Gaps**: bid placement/withdrawal has no history trail; `select_winning_bid` should ideally also write `OrderStatusHistory` (a real, pre-existing bug independent of analytics — flagged for awareness, not in scope to fix here).
**(c) Don't duplicate**: `Auction.status`/`Bid.status` are the vocabulary to reference by FK, not reinvent.

---

## 5. RECURRING FLOW

Source: `domain/recurring_supply/models.py`, `workers/automation/need_matching_service.py`, `workers/automation/recurring_supply_digest_service.py`, `graphs/agents/market_coach/flows/buyer/recurring_need.py`.

Confirmed **separate subsystem** from "future production preorder" (`MarketOffer`/`preorder_draft.py`) — `NeedAllocation` deliberately has no `market_offer_id` yet ("ajoutable plus tard"). Treat as two independent event streams.

- **RecurringNeed**: one row = one sub_category + recurrence template (`DAILY`/`WEEKLY_DAYS`/`WEEKLY`/`ONE_OFF`/`MONTHLY`, closed enum), `status` (ACTIVE/PAUSED/CANCELLED). No inline zone column — resolved via `buyer_profiles.user_id` → `auth.users.zone_id`.
- **Occurrence**: materialized ahead of time (J→J+7 window, daily-replenished cron, idempotent). **Exactly the shape wanted for coverage_rate**: `requested_quantity`, `quantity_matched`, `quantity_confirmed`, `quantity_delivered` are four independent numeric columns — `coverage_rate = Σmatched/Σrequested` is directly computable without averaging percentages. Status enum has 12 values but only `OPEN→MATCHED→{ACCEPTED,PARTIALLY_ACCEPTED,REJECTED}` or `→{SKIPPED,CANCELLED}` are actually reachable in code today (PROPOSED/FULFILLED/PARTIALLY_FULFILLED/UNFULFILLED/EXPIRED are declared but unreachable — don't build metrics assuming they populate).
- **Matching**: event-driven + polling backstop (`rematch_occurrence`), single-supplier priority allocation, never touches stock until ACCEPT.
- **Digest**: WhatsApp via existing `notification_outbox`, one row per (buyer, date), deduped via a hash of (occurrence_id, version) pairs. `occurrence.notified_at` is the best "digest_sent_at" proxy (occurrence-level, not message-level — no digest entity/table exists).
- **Response recording**: ACCEPT/REJECT go through `accept_match_proposal` (sets `accepted_at`); MODIFY/SKIP go through a *completely different* path (`update_recurring_need`). No unified "buyer_response" event — must be synthesized from two call sites plus status deltas. An architecture test (`test_match_response_action_contract.py`) asserts exactly these two call sites exist.
- `expires_at` column exists but nothing writes it — dead column, don't assume populated.

**(a) Reuse**: the four independent quantity columns (huge win — directly gives correct weighted aggregation); `version`+`updated_at` as a change-detection primitive; `notified_at` as digest-sent proxy; the existing structured log lines (`recurring_supply.match_response_mapped`) as an instrumentation point.
**(b) Gaps**: no `matched_at`/`digest_message_id`/`delivered_at` on occurrence (delivery lives on the derived Order, joined loosely via `order_group_id`, no FK); no unified response event across ACCEPT/REJECT vs MODIFY/SKIP.
**(c) Don't duplicate**: don't merge `MarketOffer`/preorder state machine into recurring-need occurrences; don't confuse with one-off `Auction`/tender (explicitly called out in the model's own docstring as a different concept).

---

## 6. OBSERVABILITY, OUTBOX, INFRA, ADMIN

### Outbox pattern
`intelligence.notification_outbox` + `OutboxDispatcher` (Celery Beat, 30s, `FOR UPDATE SKIP LOCKED` claim, dedupe_key unique constraint). **Already transactional with business writes** (enqueued in the caller's own session). **Not directly reusable as a business-event bus** — it's shaped for delivery channels (WhatsApp/Email/Push templates), not typed facts, and rows are meant to be short-lived (no retention policy, unlike telemetry). **Reuse the mechanism** (same-session enqueue + SKIP LOCKED claim + dedupe_key + Beat drain), build a separate table for analytics facts.

### Existing telemetry
`intelligence.agent_turns` / `agent_tool_calls` / `agent_llm_calls` (schema owned by Drizzle in `frontag`, mirrored read/write in the Python backend) is exactly the "TurnTrace" referenced in prior memory at the DB level — per-turn technical telemetry (intent, workflow, timing, error category), 30-day retention cron. A separate **non-persisted** `TurnTrace` class exists in `market_coach/core/turn_trace.py` — deliberately log-only, not a table.
**No `analytics` schema/package exists anywhere in the backend.** `AuditLog`/`Solicitation`/`ModerationEvent`/`DemandSignal` exist in `intelligence` but are unused-or-adjacent, not business-fact streams.
**Collision verdict**: `agent_turns` fully covers request/performance telemetry — do not duplicate it. It does **not** cover business-fact events (order paid, tender won, digest accepted) at stable record-level — that gap is real and justifies `analytics.business_events`.

### Infra
Celery app + Beat schedule (`workers/beat_schedule.py`, plain dict, ~14 entries) — a new analytics cron is a one-entry addition. Valkey/Redis used as Celery broker + RedisSearch fallback, no existing pub/sub pattern beyond Celery. `boto3`/AWS is used **only for Bedrock LLM inference** today, plus an unrelated "S3 ingestion hygiene" config block — no Kinesis/Redshift/SNS/SQS anywhere. Confirms Phase O (prepare, don't build) is the right call — there's truly nothing to build on yet.

### Admin API (Python backend)
A single, minimal router (`api/routes/admin.py`) with **one endpoint** (`GET /admin/llm/health`) protected by a **shared-secret header** (`X-Admin-Token`), not role/permission-based. No RBAC precedent exists in the Python backend at all. The "7-tab monitoring cockpit" is **not implemented here** — it lives entirely in the Next.js frontend reading Postgres directly (consistent with Drizzle owning the telemetry schema). **New admin analytics endpoints need an explicit auth decision** — there is no existing pattern to just "reuse" on the Python side.

---

## 7. FRONTEND (frontag)

- **Drizzle schema**: `0001_agent_telemetry.sql` creates the `intelligence.agent_turns/agent_tool_calls/agent_llm_calls` tables (TS defs in `src/db/schema/telemetry.ts`); `0002`-`0004` cover `marketplace.recurring_needs/recurring_need_occurrences/need_allocations/recurring_need_drafts`. New analytics tables belong in this same `src/db/schema/` barrel, next migration number **0005+**.
- **An admin KPI dashboard already exists** (`app/admin/page.tsx` → `features/admin/components/AdminDashboardClient.tsx`, backed by `features/admin/services/admin-stats.ts` — raw SQL + Drizzle aggregates for users/products/orders/revenue/top-zones) guarded by `assertAdmin()`. This is a **direct precedent to extend**, not a green field.
- **The monitoring cockpit (7-tab UI, SSE, `/admin/monitoring`) is real, fully built, and pushed to `origin/feat/monitoring-cockpit-clean`** — but **not merged to `master`**, and `master` itself looks like a stale/divergent lineage (843-file diff, different `services/`/`src/db` layout). **This needs a user decision** (§8) before any new analytics work lands in this repo.
- **Three auth/guard mechanisms coexist** for admin routes: `middleware.ts` route-prefix RBAC, `lib/api-guard.ts::requireAdmin()` (REST, used by the monitoring cockpit's `adminEndpoint()` wrapper), and `lib/action-guard.ts::secureAction()` (server actions, used by the admin dashboard). Pick one deliberately for new analytics endpoints rather than adding a fourth.
- **No chart library exists at all** (`package.json` has neither recharts, chart.js, d3, visx, nor nivo) — the monitoring cockpit renders everything as styled numeric cards/tables, not charts. Adding a chart library is a real net-new dependency decision.
- **No SWR/react-query** — all data fetching is hand-rolled `fetch`/`axios` + `useState`/`useEffect`. `useCockpitData` (polling fetch hook) is the closest ready-made primitive to mirror.

---

## 8. Decisions needed before Phase B implementation

1. **Frontend integration branch**: build new analytics work on top of `feat/monitoring-cockpit-clean` (which is where the real telemetry schema + admin dashboard precedent live), or reconcile it into `master` first? Given `master` looks abandoned, recommendation is to treat `feat/monitoring-cockpit-clean` as the real trunk — but this is the user's call, not an architectural inference.
2. **Chart library**: none exists; needs to be picked once (recommend a lightweight one — e.g. Recharts — consistent with the existing card-based, no-dependency-heavy admin UI, but open to the user's preference).
3. **New admin API auth pattern (frontend)**: reuse `adminEndpoint()`/`requireAdmin()` (REST, matches the monitoring cockpit precedent) vs `secureAction()` (server actions, matches the admin-dashboard precedent) — recommend REST + `adminEndpoint()` since new analytics endpoints are naturally REST-shaped (`/api/admin/analytics/...` per the mission spec) and this is what the cockpit already does.
4. **New admin API auth pattern (Python backend)**: no RBAC precedent exists; needs an explicit decision (extend `X-Admin-Token` shared secret, or introduce real role-based auth) if any analytics endpoint is added on the Python side at all — note the mission's endpoint list (`/api/admin/analytics/...`) maps naturally to the **Next.js** admin API (which already owns Postgres access for telemetry/dashboard), so the Python backend may not need new admin endpoints for Phase 1.
5. **`measurement_family` grouping for SAC/PANIER/TETE/UNITE**: group into a shared `COUNT`/`PACKAGE` family, or keep each as its own singleton family (current code behavior)? Affects whether "20 têtes + 5 sacs" can ever be summed under one label.

---

## 9. Reusable inventory (quick reference)

| Concern | Reuse this | Don't build a second one |
|---|---|---|
| Outbox mechanism | `outbox_repo.enqueue`/`claim_due` pattern (SKIP LOCKED, dedupe_key, same-txn enqueue) | `notification_outbox` table itself (wrong shape for facts) |
| Per-turn technical telemetry | `intelligence.agent_turns/agent_tool_calls/agent_llm_calls` | a new request-level telemetry table |
| Order/payment/delivery state | `Order.status/.payment_status/.delivery_status`, `OrderStatusHistory` | a parallel status vocabulary |
| Auction/bid state | `Auction.status`, `Bid.status`, `is_winner` | a parallel tender status vocabulary |
| Recurring coverage math | `requested_quantity`/`quantity_matched`/`quantity_confirmed`/`quantity_delivered` (already independent columns) | recomputing coverage from an averaged rate |
| Unit vocabulary | `quantity_unit.py::UNIT_SYNONYMS`, `pricing_tiers.py::_unit_family` (MASS/VOLUME) | a 4th unit-alias table |
| Taxonomy anchor | `Category` → `SubCategory.sub_category_id` (extend with `canonical_unit`/`measurement_family`) | `Product.category_label` (legacy, drifts from FK) |
| Admin dashboard UI precedent | `features/admin/components/dashboard/*` cards, `useCockpitData` polling hook | a third dashboard visual language |
| Celery scheduling | `workers/beat_schedule.py` dict + `workers/crons/` | a second scheduler |
| Frontend schema location | `src/db/schema/*.ts` barrel, next migration 0005+ | a new schema directory |

---

*Audited by 5 parallel read-only research passes across `AgriConnect` (backend) and `frontag` (frontend), 2026-09-27. No code, migrations, or files outside this document were modified in this pass.*
