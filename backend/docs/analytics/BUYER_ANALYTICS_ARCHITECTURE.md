# Buyer Analytics Architecture

Status: **Phase C — DONE (merged, CI green on real PostgreSQL). Phase D (daily aggregates, idempotent refresh, metric layer / `AnalyticsService`) — implemented on `analytics/phase-d-metric-layer`, see [METRIC_LAYER.md](METRIC_LAYER.md).** Phase E (admin API + dashboard) has not started. Phases A/B: [ANALYTICS_PHASE1_AUDIT_CARTOGRAPHY_2026-09-27.md](../ANALYTICS_PHASE1_AUDIT_CARTOGRAPHY_2026-09-27.md), [ANALYTICS_PHASE1_PR_CLEANUP_GATE_2026-09-27.md](../ANALYTICS_PHASE1_PR_CLEANUP_GATE_2026-09-27.md). Event list: [BUSINESS_EVENT_CATALOG.md](BUSINESS_EVENT_CATALOG.md).

Code lives in `backend/src/ladini/domain/analytics/`:
- `units.py` — canonical unit / measurement-family classification.
- `aggregation.py` — weighted-rate and compatible-quantity-summing primitives.
- `metric_dictionary.py` — the buyer metric dictionary (single source of truth, 30+ metrics).
- `metric_targets.py` — the `analytics.metric_targets` row-shape contract (design only, no table yet).
- `business_events.py` — the `analytics.business_events` row-shape contract + initial event catalog (design only, no table yet).

Tests: `backend/tests/unit/test_analytics_{units,aggregation,metric_dictionary,metric_targets,business_events}.py`.

---

## 1. Taxonomy

Source of truth stays exactly `Category → SubCategory`, administered by the admin — no second taxonomy was created. `Product.sub_category_id` is the anchor point (not `Product.category_label`, which is free text and can drift from the FK — a real, pre-existing inconsistency flagged in the Phase A audit and left untouched, since fixing it is a live-catalog change, not an analytics-foundations one).

## 2. Canonical unit — source of truth decision

**`SubCategory.priority_unit` IS `canonical_unit`. No new column was added.**

This was a real audit question (mission section 2: "vérifie si `priority_unit` peut déjà jouer le rôle... si oui, NE PAS dupliquer"), not assumed:
- `priority_unit`/`allowed_units` already exist on `governance.sub_categories`, in both the backend ORM and the frontend Drizzle schema, **since the very baseline migration** (not migration 0004 as one comment in the ORM file claims — that comment is stale, harmless, not fixed here since it's not this phase's scope).
- `resolve_product_unit` (`domain/quantity_unit.py`) already treats `priority_unit` as exactly "the unit to standardize toward" for a subcategory — semantically identical to what `canonical_unit` needs to mean for analytics.
- `services/database/base.py::get_product_category_unit_config` already reads these columns defensively and is already wired into the live conversational flow — as soon as an admin sets `priority_unit` on a row, both the conversational agent and analytics automatically see it, no code change needed on either side.

`domain/analytics/units.py::resolve_subcategory_canonical_unit(sub_category)` is the one function analytics code calls to read it — deliberately **not** a fallback chain (no "guess KG", no "most common historical unit"): a SubCategory with no `priority_unit` configured returns `None`, and that must stay visible to callers, not get papered over.

## 3. Measurement families

`domain/analytics/units.py::measurement_family_of(unit) -> "MASS"|"VOLUME"|"COUNT"|"PACKAGE"|"OTHER"`. Not a stored column — pure function of the unit string, since the family is fully determined by the unit (storing it separately would just be a second place it could drift from the first).

| Family | Units (from `VALID_UNITS`) | Why |
|---|---|---|
| MASS | KG, TONNE | Universally convertible (fixed kg-equivalent) |
| VOLUME | LITRE | Only one volume unit exists in the registry today |
| COUNT | TETE, UNITE | Atomic, fixed-size counts — 1 head = 1 head, always |
| PACKAGE | SAC, PANIER | Content size is admin/product-specific, not universal — see mission: "ne force pas SAC/PANIER dans COUNT si leur taille varie" |
| OTHER | anything unrecognized | Never a silent guess — blocks aggregation explicitly |

Classification is built on `pricing_tiers.py::unit_family`/`unit_factor` (now exposed as public names, previously private `_unit_family`/`_unit_factor`) rather than a new table — see §4.

## 4. Unit-logic audit — what survives, what's reused, what's explicitly NOT touched

| Source | Role | Decision |
|---|---|---|
| `domain/quantity_unit.py::UNIT_SYNONYMS`/`VALID_UNITS` | Repo-wide alias table + the enumerable set an admin picks a `priority_unit` from | **Survives untouched.** Still the single source of truth for "what units exist". Analytics imports `VALID_UNITS`, never re-lists it. |
| `domain/quantity_unit.py::convert_quantity`/`_UNIT_TO_KG` (KG/TONNE only) | Conversational-parser quantity math | **Survives untouched.** Analytics does NOT call this — see next row for why. |
| `domain/pricing_tiers.py::_unit_family`/`_unit_factor` (now public `unit_family`/`unit_factor`, includes G/GRAMME) | Pricing-tier unit-family validation | **Reused as-is, exposed publicly.** This is the more complete of the two mass/volume tables in the repo (has G, which `quantity_unit.py` doesn't) — analytics' `units_are_aggregation_compatible`/`convert_to_canonical` are built directly on it, so "1000 G + 2 KG → 3 KG" (mission example) works without inventing a 3rd conversion table. |
| `market_coach/utils.py::_CANONICAL_UNIT_MAP`/`canonical_unit_label` | WhatsApp-agent display-label normalizer (collapses G into "KG" for LABEL purposes only, not conversion) | **Left untouched, NOT reused.** It's a display concern for the conversational agent, a different scope than analytics' aggregation-compatibility concern. Flagged as a real, pre-existing drift risk (a 3rd unit-alias table) — out of scope to fix in this phase; recommended for a future consolidation pass, see "Known limitations". |
| `market_coach/nodes/memory.py::_PRIMARY_CANONICAL_UNITS` | Slot-answer validator's "is this a known canonical unit" set — **missing `LITRE`**, added to the repo 2026-08-29 | **Left untouched, flagged as a live bug**, not fixed here (it's a conversational-agent correctness issue, not an analytics one — fixing it would be scope creep into "ne transforme pas le moteur métier"). Recommend a follow-up ticket. |

Net result: **zero new unit-vocabulary tables**. Analytics reuses exactly one existing table (`pricing_tiers.py`'s), classifies it into 5 business-facing families, and reads the canonical unit straight off `SubCategory.priority_unit`.

## 5. Aggregation rules

`domain/analytics/aggregation.py`:
- `weighted_rate(parts)` — sums numerators and denominators separately, then divides once. `weighted_rate([(800,1000),(20,100)]).value == 820/1100`, never `(0.8+0.2)/2`. Every ratio-shaped metric in the dictionary is computed this way; `.value` is `None` (not `0.0`) when the denominator is zero.
- `units_are_aggregation_compatible(a, b)` — identical unit, always; a genuinely convertible pair (same MASS or VOLUME family), yes; anything else (different families, or two different COUNT/PACKAGE units), no.
- `aggregate_compatible_quantities(pairs, canonical_unit=...)` — sums everything compatible, converting as needed, and returns incompatible pairs in `.rejected` rather than silently dropping or coercing them.
- `assert_compatible_or_raise` — for call sites that must fail loudly (`IncompatibleUnitsError`).

## 6. No hardcoded product

`MetricDefinition` has no `product`/`product_name` field at all — structurally impossible to hardcode a product into a metric's shape. `supported_dimensions` is restricted to `{date, zone, category, sub_category, journey, buyer}` — enforced by `tests/unit/test_analytics_metric_dictionary.py::TestNoHardcodedProduct`, which also greps every metric's text fields for a fixed list of French product names and fails if any appear.

## 7. Buyer metric dictionary

`domain/analytics/metric_dictionary.py::METRICS` — 32 entries (see the file for the authoritative, fully-commented definitions; this section documents the judgment calls behind the trickiest ones).

### "Need instance" — the unit that makes `needs_created`/`successful_procurement_rate` homogeneous

Mission section 11 explicitly asks for a decision here, not a list of names. The chosen definition — **one "need instance" per journey**:

| Journey | Need instance | Why |
|---|---|---|
| DIRECT | one `Order` (`order_type` IN `STANDARD`,`DIRECT_SALE`; `PREORDER` excluded) | A raw search is NOT counted — it's not logged anywhere historically (Phase A finding) and is too weak a signal of real intent on its own. An order is the buyer's first *committed* ask. |
| TENDER | one `Auction` | Already the natural "one need = one tender" unit. |
| RECURRING | one `RecurringNeedOccurrence` — **not** the parent `RecurringNeed` | The parent is a standing template/subscription (see `active_recurring_needs`, a separate gauge metric), not itself an instance of expressed demand. Using the occurrence keeps the unit "a dated, quantified instance of demand" consistent across all three journeys. |

`preorder` (the separate future-production-preorder subsystem, `MarketOffer`/`preorder_draft.py`) is **not yet mapped to a journey** — it doesn't fit DIRECT (no committed stock exists yet), TENDER, or RECURRING cleanly. Left as an explicit open item, not force-fit.

### Successful Procurement Rate (North Star)

`successful_procurement_rate = weighted_rate(satisfied need instances, needs_created)`. "Satisfied" is deliberately **delivered, not merely matched**:

| Journey | "Satisfied" |
|---|---|
| DIRECT/TENDER | `Order.delivery_status IN ('DELIVERED','FULFILLED')` |
| RECURRING | `occurrence.quantity_delivered >= occurrence.requested_quantity` (full delivery only — a partial delivery feeds `recurring_coverage_rate`, not the North Star) |

**Known limitation, flagged not fixed**: whether `DELIVERED` and `FULFILLED` are true synonyms on `Order.delivery_status` or represent different sub-flows needs a one-time verification against the live code before Phase C wires this metric for real — the Phase A audit found both literal values in use but did not resolve which is authoritative.

`fulfillment_rate` is the narrower sibling: `weighted_rate(delivered, confirmed)` — the same delivered definition, but denominator is need instances that reached a firm commitment (CONFIRMED/winner-selected/quantity_confirmed>0), not all created needs. This separates "did we capture the need at all" (North Star) from "once committed, did we execute" (fulfillment_rate) — two different funnel stages, not duplicates.

### GMV — three meanings, never one

- `potential_gmv` — value at need-creation time, before any confirmation (the buyer's stated ask: order price, `auction.max_price_per_unit`, or RECURRING's `governance.standard_prices`/`RecurringNeed.max_price_per_unit` fallback chain).
- `confirmed_gmv` — value of a real, priced commitment (order CONFIRMED/PAID, winning bid selected, allocation ACCEPTED).
- `delivered_gmv` — `confirmed_gmv`, additionally gated/weighted by actual delivered quantity.

`direct_gmv`/`tender_gmv`/`recurring_gmv` are **not a 4th kind of GMV** — they're `confirmed_gmv` filtered to one journey, kept as their own catalogue entries (mission listed them by name) but implemented via `alias_of="confirmed_gmv"`, never re-derived.

### `repeat_buyer_rate`

`weighted_rate(buyers with >=2 satisfied instances, buyers with >=1 satisfied instance)`, reusing the North Star's exact "satisfied" definition (a repeat of a *failed* need shouldn't count as loyalty). Window: **90 days, PROVISIONAL** — the mission explicitly asked to audit real usage data before fixing this, and no live database access was available in this pass. Revisit with real distributions before this feeds a dashboard target.

## 8. Dimensions

Every metric declares `supported_dimensions` from a fixed vocabulary: `date, zone, category, sub_category, journey, buyer`. Rolling up a dimension (e.g. subcategory → category) must always re-apply `weighted_rate` on the raw numerator/denominator sums for that coarser grouping — never average the finer level's already-computed rates (see §5, and the worked test in `test_analytics_aggregation.py`).

## 9. Metric targets (design only)

`domain/analytics/metric_targets.py::MetricTarget` — the exact row shape a future `analytics.metric_targets` table (Phase C) will have: `metric_name` (validated against the dictionary at construction time — a target for an unknown metric fails immediately, never silently accepted), `scope_type` (GLOBAL/JOURNEY/CATEGORY/SUBCATEGORY/ZONE), `scope_id`, `target_value`, `warning_threshold`, `critical_threshold`, `valid_from`/`valid_until`. **No migration was added** — targets need at least one real aggregate to compare against before they're worth persisting; building the table now would just invite unvalidated hardcoded numbers (mission: "les targets ne doivent pas être hardcodées dans React" — the deeper point is they shouldn't be invented anywhere yet).

## 10. Business events contract (preparation only)

`domain/analytics/business_events.py` — the frozen shape of a future `analytics.business_events` row, plus the initial 20-event catalog (DIRECT/TENDER/RECURRING, mission section 16, unchanged) and two data-quality guards (`EVENTS_REQUIRING_QUANTITY`/`EVENTS_REQUIRING_AMOUNT`). **No table, no migration, no emission wiring in this phase** (mission: "PRÉPARATION SEULEMENT").

Deliberately: `buyer_id`/`producer_id` are the marketplace-identity FKs (BuyerProfile/Producer), never `auth.users.id` directly — a user can hold both roles (Phase A finding), so an event must say *which role* acted. `canonical_quantity`/`canonical_unit`/`measurement_family` are meant to be computed **once**, at emission time, via `domain/analytics/units.py` — never recomputed downstream, so two readers of the same row can never disagree about what it means.

**Emission mechanism, for Phase C**: reuse the transactional-outbox *pattern* proven by `workers/outbox/dispatcher.py` (same-session enqueue, `FOR UPDATE SKIP LOCKED` claim, `dedupe_key`/idempotency-key discipline, Celery Beat drain) — **not** the `notification_outbox` table itself, which is shaped for delivery channels (WhatsApp/Email templates), not typed facts (Phase A finding, unchanged).

## 11. Historical reconstructibility

Recorded per-metric in the dictionary (`reconstructible_historically` + `reconstructible_note`), not asserted globally. Summary:

- **NO**: `direct_searches`, `direct_search_success_rate`, `direct_search_to_order_rate` — no search-log table exists anywhere in the codebase; these have zero history before Phase C instruments `DIRECT_SEARCH_PERFORMED`.
- **PARTIAL**: `successful_procurement_rate`/`fulfillment_rate` (pending the DELIVERED-vs-FULFILLED verification, §7), `potential_gmv` (RECURRING side depends on `standard_prices` coverage, not guaranteed historically), `tender_fulfillment_rate` (no `OrderStatusHistory` write from `select_winning_bid`, a pre-existing gap, not fixed here), `recurring_modification_rate` (no single "buyer_response" log correlating a modify action to the digest that prompted it — approximated by timestamp proximity, not exact, for historical data).
- **YES**: everything else — in particular all four `recurring_*_quantity` metrics and `recurring_coverage_rate`, since `requested_quantity`/`quantity_matched`/`quantity_confirmed`/`quantity_delivered` are four independent, historically-populated columns per occurrence (no averaging-of-percentages trap to begin with).

No historical value is fabricated for a NO/PARTIAL metric anywhere in this codebase — that's a Phase C data-quality invariant, not just a phase B talking point.

## 11.5 Phase C decisions (2026-09-27)

**1.A — DELIVERED vs FULFILLED, resolved by tracing every writer of `Order.delivery_status`:**

They are **not synonyms**. Each belongs to a distinct flow:

| Value | Written by | Meaning |
|---|---|---|
| `FULFILLED` | `record_sale` only, at Order creation (`order_type=DIRECT_SALE`) | Buyer physically present at an instant walk-in exchange — no separate delivery step exists for this flow, so it's terminal by construction. |
| `DELIVERED` | `EscrowMixin.verify_delivery_otp` (buyer-supplied OTP), `ProducerMgmtMixin.confirm_delivery_and_payment` (cash-on-delivery, one producer gesture) | Terminal for STANDARD/PREORDER/tender-linked orders. |
| `DELIVERED` (same literal, different flow) | `RecurringSupplyMixin.mark_order_delivery_status`, `order_type=RECURRING_SUPPLY` only | **Intermediate**, producer-declared, NOT yet buyer-confirmed. |
| `RECEIVED` | `RecurringSupplyMixin.record_order_reception`, buyer-only | The real "buyer confirms successful receipt" state for RECURRING_SUPPLY orders — sets `Order.status='COMPLETED'`. |
| `RECEIVED_WITH_ISSUE` | Same method | Explicitly excluded from "success" — leaves `Order.status='CONFIRMED'`, resolution stays human (mandate: never an automatic refund/penalty). |

**Decision**: "satisfied/delivered" for `successful_procurement_rate`/`fulfillment_rate`/`delivered_gmv` is **order_type-conditional**, not a flat literal set:
- `order_type != 'RECURRING_SUPPLY'` → `delivery_status IN ('DELIVERED', 'FULFILLED')`.
- `order_type == 'RECURRING_SUPPLY'` → `delivery_status == 'RECEIVED'` only.

Locked in by `tests/unit/test_analytics_metric_dictionary.py::TestDeliveredVsFulfilledContract`.

**Real bug found while tracing this** (not a unit-model issue — a schema/instrumentation gap): `RecurringNeedOccurrence.quantity_delivered` has **zero writers anywhere in the codebase**. Phase B's dictionary wrongly assumed it was historically populated (`Reconstructibility.YES`); corrected to `NO` for `recurring_delivered_quantity`, and `PARTIAL` for `delivered_gmv`/`recurring_fulfillment_rate`/`successful_procurement_rate`'s RECURRING contribution (the only real signal is the linked RECURRING_SUPPLY Order's `delivery_status`, joined via `occurrence.order_group_id` — nullable, no FK, per the Phase A finding). This column being dead is itself a Phase D/E candidate (either start writing it from the RECEIVED transition, or retire the metric in favor of the order-join approach).

**1.B — LITRE fixed, minimally, with tests:**

Two real, live bugs (not touched in Phase B, since fixing them then would have been scope creep into "ne transforme pas le moteur métier" before this session's explicit go-ahead):
- `market_coach/utils.py::_CANONICAL_UNIT_MAP` had no entry for `L`/`LITRE`/`LITRES` at all (only worked for the exact string "LITRE" by accidental fallthrough — "L" and "LITRES" came back unfolded).
- `market_coach/nodes/memory.py::_PRIMARY_CANONICAL_UNITS` was missing `"LITRE"` since the unit's addition to the registry (2026-08-29), so `_resolve_unit_value` rejected a valid "litre" answer to "quelle unité ?" outright.

Fixed both with the minimal diff (new dict entries only, no restructuring), tested in `tests/unit/test_canonical_unit_label.py::TestLitreIsARealCanonicalEntryNotAnAccidentalFallthrough` and the new `tests/unit/test_resolve_unit_value_litre.py`. Confirmed via the full `test_conversation_characterization.py`/`test_state_machine_transition_matrix.py`/`test_new_task_micro.py`/`test_services_and_tools.py` suites that nothing else regressed.

## 12. Known limitations (carried forward, not fixed in this phase)

1. ~~`market_coach/utils.py::_CANONICAL_UNIT_MAP`/`_PRIMARY_CANONICAL_UNITS` missing LITRE~~ — **fixed in Phase C**, see §11.5.1.B. The two tables still exist as separate surfaces from `quantity_unit.py::UNIT_SYNONYMS` (a 3rd/4th unit-vocabulary risk in general) — not unified, only the specific LITRE gap was closed; a full consolidation remains a future candidate, not done here.
2. `Product.category_label` (free text) vs `Product.sub_category_id` (FK) can disagree — pre-existing, not fixed here.
3. ~~`Order.delivery_status`'s `DELIVERED` vs `FULFILLED` semantic needs verification~~ — **resolved in Phase C**, see §11.5.1.A.
4. `select_winning_bid` doesn't write `OrderStatusHistory` — a real, pre-existing gap (Phase A finding) that limits `tender_fulfillment_rate`'s historical fidelity; not fixed here.
5. The future-production-preorder subsystem (`MarketOffer`/`preorder_draft.py`) has no journey mapping yet.
6. **New in Phase C**: `RecurringNeedOccurrence.quantity_delivered` has zero writers anywhere in the codebase (see §11.5.1.A) — `recurring_delivered_quantity` has no real data source until this is fixed or replaced.
7. **New in Phase C**: the RECURRING North Star/GMV/fulfillment signal depends on `occurrence.order_group_id`, which has no FK — a real but imperfect join, not a hard guarantee (could silently miss/misattribute an order if the correlation value is ever wrong).

## 13. Phase C pipeline (implemented)

- **Tables** (schema `analytics`, Drizzle migration `0006`, mirrored in SQLAlchemy): `business_events`, `event_outbox`, `metric_targets`.
- **Emit**: `BusinessEventEmitter.emit()` inserts one row into `analytics.event_outbox` (`ON CONFLICT (dedupe_key) DO NOTHING`) **in the caller's own transaction** — the business fact and its event intent commit or roll back together. No network call, no extra lookup; a broken emit rolls the business transaction back (a bug to fix, not to swallow).
- **Drain**: `AnalyticsEventDispatcher` (Celery Beat) — phase 1 claims a batch `FOR UPDATE SKIP LOCKED` (to `SENDING`, committed); phase 2 inserts into `business_events` (`ON CONFLICT (idempotency_key) DO NOTHING`) and marks `SENT`, one transaction per row; failures back off (1/5/15/60/180 min) and become `DEAD` after 5 attempts. A Phase C review found and fixed a bug here: the payload key `metadata` resolved to Declarative's `MetaData` instead of the `metadata_` column, which would have failed every event.
- **Idempotency**: keys identify the business fact (`{EVENT}:{entity_id}`), never a message id, so WhatsApp/Celery replays and cron overlaps collapse to one event.
- **Read-only trap**: methods routed through `AgriDatabaseService._READ_ONLY_METHODS` never commit; `search_products` was removed from that set because it now writes an outbox row (guarded by `tests/architecture/test_event_emitting_methods_are_transactional.py`).
- **Atomicity map**: DIRECT/TENDER/RECURRING mixin call-sites use the same session as the fact (`@transactional(write=True)`). `NeedMatchingService` and `RecurringSupplyDigestService` emit in the same session and a single `commit()` covers fact and outbox row. The only asynchronous gap is outbox to `business_events` (by design). Matching wraps its work in a best-effort try/except: an emit failure there rolls that occurrence's match back and is retried by the next cron pass.
- **Performance**: the critical path adds one indexed same-database INSERT per event and zero external calls. Recurring replenishment adds one query per cron run (buyer zone + sub-category canonical unit for all active needs), not per need. Matching emits only when the allocation set changed; the digest emits only for newly queued digests. `search_products` now commits a (tiny) write transaction instead of a read-only one.

## 14. Phase D (implemented) and what remains

Implemented: four daily aggregate tables (migration 0007), idempotent per-day refresh with a rolling 14-day window,
`AnalyticsService`, target resolution, data-quality checks — details, grains, sources and limits in
[METRIC_LAYER.md](METRIC_LAYER.md). North Star is exposed as **PARTIAL** (RECURRING receipts are a buyer-confirmed lower
bound); recurring quantity fulfillment and DIRECT confirmation metrics stay UNAVAILABLE.

Phase E candidates: admin API on the Next.js side calling the service contract, dashboard/charts (library decision still
open), seeding real targets, resolving `quantity_delivered` (write it from the RECEIVED transition or retire it), and
instrumenting `DIRECT_ORDER_CONFIRMED`.

## 15. Phase D.5 — gaps closed before the dashboard

- `RecurringNeedOccurrence.quantity_delivered`: **RELIABLE (lower bound)** — exact mapping proven, idempotent writer added in the RECEIVED
  transaction (see METRIC_LAYER.md). `recurring_delivered_quantity` / `recurring_fulfillment_rate` are now bound (PARTIAL).
- `DIRECT_ORDER_CONFIRMED`: **INSTRUMENTED** on the two canonical `-> CONFIRMED` transitions; `direct_fulfillment_rate` and `direct_gmv` are bound.
  Audit finding fixed on the way: `DIRECT_ORDER_CREATED` had been wired to a dead path and the DIRECT aggregates filtered `STANDARD` only,
  missing the real PREORDER checkout cohort.
- `direct_search_to_order_rate` renamed `direct_orders_per_search` (window ratio, not attributed).
- North Star stays PARTIAL. Targets stay unseeded.
- Still open: global `fulfillment_rate` (needs per-journey confirmed counts at buyer level), `recurring_modification_rate`,
  `active_recurring_needs` history, session-level search->order attribution.
