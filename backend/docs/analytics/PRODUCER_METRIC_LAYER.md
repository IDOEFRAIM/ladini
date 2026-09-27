# Producer Metric Layer (Phase C)

Companion to `METRIC_LAYER.md` (buyer side) and `PRODUCER_ANALYTICS_ARCHITECTURE.md` (Phase A audit + Phase B instrumentation). Same discipline throughout: every rate is `SUM(numerator)/SUM(denominator)` over the requested window, never an average of rates; a zero denominator yields `value=None`, never `0`; physical quantities are never mixed across units.

## Source-of-truth rule

`producer_daily_metrics` and `producer_quantity_daily_metrics` are **FLOWS**: cohort (order creation day) + current state, fully rebuildable for any past day from `marketplace.orders`/`order_items`/`products`/`bids` — never from `business_events` for the bulk of the numbers (same reasoning as the buyer layer: full historical reconstructibility, not just what Phase B's event-enrichment happens to cover). `producer_supply_daily_snapshot` is the one exception: a **SNAPSHOT** of `marketplace.products`' live state, generated once per day, for **today only** — never reconstructible for a past day (see "Refresh / recompute" below).

## Tables (schema `analytics`, migration `0010`, Drizzle = source of truth, SQLAlchemy mirror)

| Table | Grain | Kind |
|---|---|---|
| `producer_daily_metrics` | `(metric_date, producer_id)` | FLOW |
| `producer_quantity_daily_metrics` | `(metric_date, producer_id, canonical_unit)` | FLOW |
| `producer_supply_daily_snapshot` | `(metric_date, producer_id, zone_id, category_id, sub_category_id, canonical_unit)` | SNAPSHOT |

`zone_id` on all three is the **producer's own zone** (`Producer.zone_id`), never the buyer/delivery zone — see mission Étape 19. No foreign keys (derived + rebuildable, same convention as the buyer tables).

### `producer_daily_metrics`

A row only exists for a `(day, producer)` with **>= 1 qualifying activity fact**: `products_published`, `quantity_changes`, `bids_received` (all sourced from `analytics.business_events`, all producer_id-carrying since Producer Analytics Phase B) or an order confirmed/delivered that day. **This is the entire mechanism behind `active_producers`** — no separate boolean column, `COUNT(DISTINCT producer_id)` over the window is exact.

Order counts and delivered GMV are split per journey: `orders_confirmed_{direct,tender,recurring}`, `orders_delivered_{direct,tender,recurring}`, `delivered_gmv_{direct,tender,recurring}`. "Confirmed" is journey-specific:
- **DIRECT**: `Order.status IN ('CONFIRMED', 'COMPLETED')` (or the `DIRECT_ORDER_CONFIRMED` event) — same predicate the buyer layer already uses, **with the same safety net**: a delivered order counts as confirmed even if this predicate somehow missed it (`order_is_confirmed` in `producer_daily_aggregation.py`), so `delivered <= confirmed` always holds.
- **TENDER**: the order's own creation (`select_winning_bid`) — there is no separate confirmation step, so `confirmed` is always `True` once the order exists.
- **RECURRING**: the order's own creation (`accept_match_proposal`) — same reasoning, always `True`.

"Delivered" is also journey-specific: DIRECT/TENDER = `delivery_status IN ('DELIVERED', 'FULFILLED')`; RECURRING = `delivery_status = 'RECEIVED'` (buyer-confirmed reception, same rule the buyer layer already applies to this journey — a producer's own delivery claim alone is not enough).

**GMV attribution**: each DIRECT/TENDER/RECURRING order maps to **exactly one producer** — cart splitting ensures one Order per producer for DIRECT, the winning bid identifies the producer for TENDER, and `accept_match_proposal` creates one order per producer for RECURRING (all three facts proven in `PRODUCER_ANALYTICS_ARCHITECTURE.md` §17-19). `Order.total_amount` is therefore already the correct producer attribution — no item-level split was needed.

### `producer_quantity_daily_metrics`

Same cohort/confirmed/delivered rules as above, but grained by `canonical_unit` (never KG + L + TETE in one sum — same `resolve_canonical`/`to_canonical_quantity` machinery the buyer's RECURRING table already uses, reused directly, not duplicated). **TENDER has no columns here at all** — not `confirmed_quantity_tender = 0`, simply absent — because a TENDER order has no `OrderItem` (Phase A finding, `PRODUCER_ANALYTICS_ARCHITECTURE.md` §3.2 finding I): there is no reliable per-unit quantity to report, and a column that's always zero would misrepresent that as "measured empty" rather than "not measurable."

### `producer_supply_daily_snapshot`

Each row is **"the known sellable supply of this producer, at the end of this day"** — never a quantity added that day, never additive across days (summing several days' snapshots would double-count standing inventory). Built from a live `GROUP BY producer_id, zone_id, category_id, sub_category_id, unit` over `marketplace.products WHERE is_available = TRUE AND quantity_for_sale > 0`, converted into the sub-category's `priority_unit` when compatible (same canonicalization as everywhere else).

## Units

Identical machinery to the buyer layer (`domain/analytics/units.py`, `daily_aggregation.py::resolve_canonical`/`to_canonical_quantity`, reused by import, not copied): `1500 G` → `1.5 KG`; `TETE`/`SAC` keep their own unit, never converted; a physical metric spanning several canonical units in its window returns `status=MIXED_UNITS`, `value=None`, and a per-unit `breakdown` — never a summed, meaningless number.

## Refresh / recompute

`ProducerMetricsRefresher.recompute_day(day)` (`services/analytics/producer_metrics_refresh.py`) rebuilds the two FLOW tables for UTC day `day`: a per-day advisory lock (`pg_advisory_xact_lock`, keyed `producer_daily_metrics:{day}`), then `DELETE` + bulk `INSERT` in the caller's own transaction — byte-identical to the buyer layer's `DailyMetricsRefresher.recompute_day` in mechanism. Replaying it is idempotent; recomputing an arbitrary historical day is the same call.

`snapshot_producer_supply(session, day)` is **not** `recompute_day` — it **refuses any `day` other than today (UTC)**, raising `ValueError`. No historical ledger of `is_available`/`quantity_for_sale` existed before Producer Analytics Phase B (and even `PRODUCT_SELLABLE_QUANTITY_CHANGED` only started accumulating forward from that phase, as a change-event stream, not a point-in-time gauge) — fabricating a past day's snapshot from today's live state would misrepresent history. It is idempotent for today: re-running it replaces today's rows with the same live query.

`recompute_range`/`recompute_recent` mirror the buyer layer's batch helpers exactly (one committing transaction per day; `recompute_recent` also snapshots today's supply as its last step).

## Late-arriving facts

Same cohort rule as the buyer layer: a producer's order-facts row for day D describes orders **created** on D and their **current** state — a delivery confirmed 3 days later changes day D's row the next time D is recomputed, it does not create a new fact on the delivery day. Proven by `TestRecomputeIdempotencyAndLateEvents::test_a_late_delivery_updates_the_original_cohort_row` in the real-Postgres test file.

## Metrics

The 8 pilot KPIs from `PRODUCER_ANALYTICS_ARCHITECTURE.md` §48, now live (`domain/analytics/producer_metric_layer.py::BINDINGS_PRODUCER` for the 3 that are plain single-table SUM/SUM ratios; `ProducerAnalyticsService`'s dedicated methods for the other 5, which each need something a single `Binding` cannot express):

| Metric | Mechanism | Reliability |
|---|---|---|
| `active_producers` | `COUNT(DISTINCT producer_id)` over `producer_daily_metrics` in the window | RELIABLE |
| `available_supply` | Live read of the most recent `producer_supply_daily_snapshot` day, grouped by `canonical_unit` (MIXED_UNITS if >1) | RELIABLE (snapshot only, never historical) |
| `time_to_first_sale` | `percentile_cont(0.5)` of (first delivered sale - first `PRODUCT_PUBLISHED_FOR_SALE`), per producer activated in the window | PARTIAL (Phase B forward only; RECURRING uses `Order.updated_at` as an imperfect proxy — no dedicated "received at" column exists) |
| `producer_order_fulfillment_rate` | `Binding`, `producer_daily_metrics` | RELIABLE |
| `producer_quantity_fulfillment_rate` | `Binding`, `producer_quantity_daily_metrics`, `physical=True` | PARTIAL (DIRECT+RECURRING only; TENDER structurally absent) |
| `producer_delivered_gmv` | `Binding`, `producer_daily_metrics` | RELIABLE |
| `delivered_gmv_per_active_producer` | `SUM(producer_delivered_gmv) / active_producers`, cross-metric (never 0 on an empty denominator — always `None`) | RELIABLE |
| `repeat_producer_rate` | Per-producer CTE over `producer_daily_metrics` (producers with >= 2 delivered orders / >= 1), recomputed at the window level — never an average of daily rates | RELIABLE, window **PROVISIONAL** |

### Explicitly UNAVAILABLE (registered with their exact reason, never silently absent, never proxied)

- **`producer_sell_through_rate`** — a positive `PRODUCT_SELLABLE_QUANTITY_CHANGED` delta does not distinguish new production from a correction, a reconciliation, a return or an adjustment. Building a ratio off summed positive deltas would be misleading, not a lower bound. **Still UNAVAILABLE even with the quantity-change history Phase B added** — that history is the raw material a future design would need, not itself sufficient.
- **`producer_paid_gmv`** — `PAID_OUT` (escrow) and `PAID` (cash) are set atomically with `DELIVERED` in this codebase today (no delayed-payout path exists) — identical to `producer_delivered_gmv`, not a distinct signal. Not renamed/duplicated; `alias_of="producer_delivered_gmv"` in the dictionary makes this explicit.
- **`demand_exposure_rate`** — mixed, not one number: DIRECT UNAVAILABLE (no search-impression/visibility instrumentation), TENDER UNAVAILABLE (no tender-notification instrumentation), RECURRING PARTIAL (`RECURRING_MATCH_FOUND` is a real per-allocation signal, but wiring it into this phase's aggregates is explicitly out of Phase C scope). No combined figure is proposed.
- **`historical_unsold_supply`** — only a live snapshot exists (`producer_supply_daily_snapshot`, forward from Phase C's own first run); a PERIOD figure (newly listed minus sold, over a past window) is not reconstructible.

## Targets

Reuses `analytics.metric_targets` and the generic `resolve_target`/`evaluate_target`/`TARGET_PRECEDENCE` engine from `domain/analytics/metric_layer.py` — no new table, no new scope type. `ProducerAnalyticsService._apply_target` mirrors `AnalyticsService._apply_target` exactly. No pilot target values are seeded (same instruction as the buyer layer: never hard-code a target).

## ProducerAnalyticsService (`services/analytics/producer_analytics_service.py`)

Mirrors `AnalyticsService` in shape (`get_metric`, `get_metric_timeseries`, `get_metric_breakdown`, `compare_periods`) plus the curated views the mission asked for: `get_producer_overview`, `get_producer_fulfillment`, `get_producer_gmv`, `get_repeat_producers`, `get_available_supply` (no date-range params — deliberately live-only), `get_time_to_first_sale`. Not inherited from a shared base class — none existed before this phase, and forcing one now would be a premature abstraction (mission Étape 25's own instruction); the generic, domain-agnostic pieces (`Binding`, `DataStatus`, `MetricResult`, `TargetRow`, `compute_value`, `resolve_target`, `evaluate_target`) are imported and reused directly from `metric_layer.py`, not copied.

No public API / dashboard on top of this service in this phase — Phase D's endpoints will call it, not before.

## Data quality (`services/analytics/data_quality.py`)

`run_producer_metric_quality_checks(session, start, end)`, a new function alongside the buyer-side `run_data_quality_checks` and the Phase B `run_supply_data_quality_checks` (each scoped to its own layer, not merged): `delivered_gt_confirmed_orders_{direct,tender,recurring}` (ERROR), `delivered_gt_confirmed_quantity_{direct,recurring}` (ERROR), `unknown_canonical_unit` (WARNING, on the quantity and snapshot tables), `duplicate_grain` (ERROR, all 3 tables), `stale_refresh` (WARNING, all 3 tables, 36h threshold — same as the buyer layer), `missing_snapshot` (WARNING — no supply snapshot exists for today although sellable products do). `run_supply_data_quality_checks` (Phase B) also gained a `tender_order_event_missing_producer_id` check, extending its existing `direct_order_event_missing_producer_id` pattern to TENDER.

## Known limits

1. `time_to_first_sale`'s RECURRING leg uses `Order.updated_at` as a proxy for "received at" — no dedicated timestamp exists for `record_order_reception`. A future `OrderStatusHistory`-backed timestamp would remove this imprecision.
2. `repeat_producer_rate`'s window is PROVISIONAL — not calibrated against real producer cadence data (same caution as the buyer layer's `repeat_buyer_rate`, and the same RECURRING `recurrence_type` diversity — DAILY/WEEKLY/MONTHLY — argues for segmenting by category before fixing a number).
3. `producer_quantity_fulfillment_rate` cannot include TENDER by construction — not a gap to close, a structural fact (no `OrderItem` exists for a tender order).
4. `producer_supply_daily_snapshot` has zero history before its first run — `available_supply` is a live gauge only, by design, not a temporary limitation.
5. No `producer_daily_metrics`-based dashboard or Admin API exists yet — Phase D, deliberately not started here.
