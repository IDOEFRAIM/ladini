# Buyer Analytics Architecture

Status: **Phase B (taxonomy, units, metric dictionary) — DONE.** Phase A (audit) and the pre-Phase-B repository cleanup gate are done; see [ANALYTICS_PHASE1_AUDIT_CARTOGRAPHY_2026-09-27.md](../ANALYTICS_PHASE1_AUDIT_CARTOGRAPHY_2026-09-27.md) and [ANALYTICS_PHASE1_PR_CLEANUP_GATE_2026-09-27.md](../ANALYTICS_PHASE1_PR_CLEANUP_GATE_2026-09-27.md). Phase C (business_events table + emission, aggregates, AnalyticsService, admin endpoints, dashboard) has not started.

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

## 12. Known limitations (carried forward, not fixed in this phase)

1. `market_coach/utils.py::_CANONICAL_UNIT_MAP` and `market_coach/nodes/memory.py::_PRIMARY_CANONICAL_UNITS` are a 3rd/4th unit-vocabulary surface, one of them (`_PRIMARY_CANONICAL_UNITS`) demonstrably missing `LITRE` since 2026-08-29 — a live conversational-agent bug, out of scope here (touching it risks the "ne transforme pas le moteur métier" boundary this whole engagement has respected since Phase A).
2. `Product.category_label` (free text) vs `Product.sub_category_id` (FK) can disagree — pre-existing, not fixed here.
3. `Order.delivery_status`'s `DELIVERED` vs `FULFILLED` semantic needs a one-time verification before Phase C computes the North Star for real.
4. `select_winning_bid` doesn't write `OrderStatusHistory` — a real, pre-existing gap (Phase A finding) that limits `tender_fulfillment_rate`'s historical fidelity; not fixed here.
5. The future-production-preorder subsystem (`MarketOffer`/`preorder_draft.py`) has no journey mapping yet.

## Phase C plan (recommendation, not started)

1. Verify DELIVERED-vs-FULFILLED semantics (limitation 3) before wiring the North Star.
2. Stand up `analytics.business_events` (real migration, both repos in sync from day one this time) + the outbox-pattern emitter, starting with the DIRECT/TENDER/RECURRING events already catalogued here.
3. First daily aggregates (`analytics.{buyer,direct,tender,recurring}_daily_metrics`), numerators/denominators stored separately per §5.
4. `analytics.metric_targets` migration, seeded from real observed rates, not guesses.
5. `AnalyticsService` (backend, deterministic — reads the dictionary, never recomputes a metric's meaning) + first `/api/admin/analytics/*` endpoints on the **Next.js** side (it already owns Postgres access for telemetry/dashboard, per the Phase A audit) using `adminEndpoint()`/`requireAdmin()`, matching the existing monitoring-cockpit convention.
6. Frontend: a chart library decision is still open (none exists in `frontag` — Phase A finding); TS types for the metric dictionary/filters can be generated from this file's shape.
