# Producer Analytics Dashboard (Phase D)

Admin-only cockpit that lets the Ladini team answer producer questions without SQL. **No KPI is computed in
TypeScript**: every number, delta, status and target comes from `ProducerAnalyticsService` (Phase C's Metric Layer).
Mirrors `BUYER_DASHBOARD.md` (Phase E) exactly in architecture, contract and conventions — this document only
describes what differs on the producer side.

## Micro-gate (pre-flight, before any Phase D code)

Before touching the admin API, a real-Postgres regression test was added proving `available_supply` never sums
incompatible units (a scope holding both a KG product and a TETE product): `tests/schema/test_producer_metric_layer_pg.py
::TestSupplySnapshot::test_available_supply_never_sums_incompatible_units`. It passed on the first run (5363 passed,
0 failed) — `_available_supply`'s existing `MIXED_UNITS` handling (from Phase C) was already correct. Per the
mission's own branching instruction, no production code was touched for this step, and Phase D proceeded directly.

## Route and access
- Page: `/admin/analytics/producers` (frontend repo, `app/admin/analytics/producers`), linked from the admin navbar
  **alongside**, not instead of, `/admin/analytics/buyers`.
- Access is ADMIN/SUPERADMIN only, enforced twice: `app/admin/layout.tsx` (redirect) and every endpoint (`requireAdmin`).
  Unauthenticated -> 401, non-admin -> 403, admin -> 200 (tested, same pattern as the buyer cockpit).

## Architecture (identical server-to-server adapter, different namespace)
```
browser -> Next.js /api/admin/analytics/producers/*   (requireAdmin, parameter whitelist, no-store)
        -> FastAPI /internal/analytics/producers/*    (X-Internal-Token, fail-closed, read-only)
        -> producer_admin_api.py -> ProducerAnalyticsService -> analytics.producer_*_metrics / producer_supply_daily_snapshot (+ metric_targets)
```
`features/analytics/server/proxy.ts::analyticsProxy` gained one optional `namespace: 'buyers' | 'producers' = 'buyers'`
parameter (backward compatible — every existing buyer route call is unchanged) instead of a second, parallel adapter
module. The browser never sees the backend URL or token (same `LADINI_BACKEND_URL`/`INTERNAL_API_TOKEN`, server-side
env only, shared with the buyer router). `services/analytics/producer_admin_api.py` reuses `admin_api.py`'s generic,
non-buyer-specific pieces directly rather than re-implementing them: `ApiError`, day/uuid parsing, `parse_paging`,
`filter_options`, `_labels`/`_label`, and the zone-hierarchy `resolve_filters` (duck-typed on `zone_id`/`category_id`/
`sub_category_id` — it never reads `journey`, so it works unchanged on the producer side's `Params`, which has no
`journey` field at all: no producer table carries a journey dimension).

## Endpoints (Next path -> backend path, all GET)
| Endpoint | Content |
|---|---|
| `overview` | 7 KPI cards (`active_producers`, `producer_order_fulfillment_rate`, `producer_quantity_fulfillment_rate`, `producer_delivered_gmv`, `delivered_gmv_per_active_producer`, `repeat_producer_rate`, `time_to_first_sale`), `not_available` list, freshness |
| `supply` | `available_supply` — a **live gauge**, never a period flow; `from`/`to` are accepted for a uniform request shape but ignored (the service always reads "today") |
| `fulfillment` | `producer_order_fulfillment_rate`, `producer_quantity_fulfillment_rate` |
| `gmv` | `producer_delivered_gmv`, `delivered_gmv_per_active_producer` |
| `retention` | `repeat_producer_rate` (window PROVISIONAL) |
| `metrics/{metric}/timeseries` | day/week/month buckets — only `active_producers` + the 3 `BINDINGS_PRODUCER` metrics have a series |
| `metrics/{metric}/breakdown` | dimension must match the metric's own table: `producer_order_fulfillment_rate`/`producer_delivered_gmv` by `zone_id` only (their table has no category/sub-category dimension); `producer_quantity_fulfillment_rate` by `canonical_unit` only (its table has no zone dimension either) |
| `compare` | current vs previous period (default: the equal-length window right before) |
| `filters` | real taxonomy for the controls (same reader as the buyer side: categories, sub-categories, zones) |
| `health` | `Healthy` / `Warning` / `Stale`, freshness across the 3 producer daily tables, data-quality issues (`run_producer_metric_quality_checks`) |

Common filters: `from`, `to` (UTC calendar days, both inclusive, max 400 days), `zone_id` (expands to the zone and all
descendants), `category_id`, `sub_category_id`. **No `journey` filter** — producer metrics are already journey-combined
(`producer_order_fulfillment_rate` sums DIRECT+TENDER+RECURRING) or journey-restricted by construction
(`producer_quantity_fulfillment_rate` is DIRECT+RECURRING only, TENDER structurally absent — no `OrderItem` exists for
a tender order). A filter a metric's table cannot honour (e.g. `zone_id` on `producer_quantity_daily_metrics`, whose
only dimension is `canonical_unit`) reports `UNAVAILABLE` with the reason — never a silent 0, never a 500.

## Response contract (identical shape, reused types)
Same as the buyer side: `metric_name, label, value, numerator, denominator, unit, status, reliability, period, target,
target_status, previous_value, delta, delta_kind, delta_points | delta_pct, breakdown, notes`. The frontend reuses the
buyer's `MetricPayload`/`JourneyResponse`/`TimeseriesResponse`/`BreakdownResponse`/`FilterOptions`/`HealthResponse`
types verbatim (`features/analytics/types.ts`) — only two producer-specific shapes were added
(`features/analytics/types.producer.ts`): `ProducerOverviewResponse` (no `journey_mix`, producers have no journey
filter) and `SupplyResponse` (`{ ...Meta, metric: MetricPayload }`, the single live gauge).

## Page sections and metric mapping
1. **Filters**: 7/30/90 days (default 30) or custom, zone (hierarchy), category, sub-category — same `Filters`
   component reused unchanged from the buyer cockpit. No journey tabs (producer metrics have no journey dimension).
2. **KPI cards** (7): the pilot metrics above.
3. **Supply gauge**: `available_supply`, rendered with the same generic `MetricCardFromPayload` the buyer cockpit
   already uses for every other metric — it already handles `OK`/`MIXED_UNITS`/`UNAVAILABLE`/`NO_DATA` with zero
   producer-specific code.
4. **Exécution** (fulfillment), **GMV**, **Fidélisation** (retention): three small sections, each a metric grid over
   its own endpoint.
5. **Trends** (4 charts): active producers, order fulfillment rate, quantity fulfillment rate, delivered GMV.
6. **Analytics health** badge with last refresh (reuses `HealthBadge` from `BuyerAnalyticsPage.tsx` unchanged).
7. **Not-yet-available list**: `producer_sell_through_rate`, `producer_paid_gmv`, `demand_exposure_rate`,
   `historical_unsold_supply` — always shown with their exact reason, never a fabricated number.

## Sell-Through and Demand Exposure — deliberately UNAVAILABLE, never faked
Both remain exactly as `PRODUCER_ANALYTICS_ARCHITECTURE.md` §4/§7/§42/§51 concluded in Phases A-C:
- `producer_sell_through_rate`: **UNAVAILABLE**. A positive `PRODUCT_SELLABLE_QUANTITY_CHANGED` delta cannot
  distinguish new production from a correction/reconciliation — no reliable restock signal exists yet.
- `demand_exposure_rate`: **UNAVAILABLE** as a single number (mixed per journey: DIRECT/TENDER have no exposure
  instrumentation at all; RECURRING has a real signal, `RECURRING_MATCH_FOUND`, but it is not wired into this phase's
  aggregates). No combined figure is proposed or displayed — this phase does not build one just to fill a card.

Neither metric is hidden either: both appear in the overview's `not_available` list with their exact reason, per the
same convention the buyer side already uses for `recurring_modification_rate`/`active_recurring_needs`.

## Historical coverage
- `available_supply`: **live snapshot only** (`producer_supply_daily_snapshot`, forward from Phase C's first run) —
  never a historical series, never additive across days. The gauge's own note says so explicitly on every response.
- `time_to_first_sale`: **PARTIAL** — only activations observable via `PRODUCT_PUBLISHED_FOR_SALE` (Phase B forward)
  are covered; a producer's very first product published before that phase's deploy is invisible to this metric.
- `producer_quantity_fulfillment_rate`: **PARTIAL** — reliable for DIRECT+RECURRING, structurally `UNAVAILABLE` for
  TENDER (no `OrderItem` exists for a tender order, so there is no per-unit ledger to build a quantity rate from —
  this is a structural absence, not a degraded-to-0 value; see `producer_admin_api.fulfillment`'s explicit note).
- Everything else in the pilot 8 (`active_producers`, `producer_order_fulfillment_rate`, `producer_delivered_gmv`,
  `delivered_gmv_per_active_producer`, `repeat_producer_rate`) is `Reconstructibility.YES` — built from transactional
  tables, recomputable for any past day via `ProducerMetricsRefresher.recompute_range`.

## Targets
Reused from `analytics.metric_targets` (same table, same `resolve_target`/`evaluate_target` engine as the buyer side).
None are seeded for producer metrics. `target = null` shows "Aucun objectif configuré" in a neutral style (no
red/orange) — identical convention to the buyer cockpit.

## 2 integration bugs found and fixed wiring this layer (zero KPI/formula change)
1. **`available_supply`'s `MIXED_UNITS` breakdown missing `numerator`**: the shared `MixedUnits` frontend component
   (already used for every other physical metric) reads `breakdown[].numerator` to render a quantity; the producer
   service's breakdown only carried `value`/`product_count`. Fixed by adding `numerator` alongside `value` (same
   number under both keys) in `ProducerAnalyticsService._available_supply` — purely additive, no existing consumer
   inspected the breakdown dict's exact key set.
2. **`_where` didn't ignore `zone_scope`**: `admin_api.resolve_filters` (buyer, reused as-is for producers) always
   injects a `zone_scope` key alongside the expanded `zone_id` list (so target resolution can still see the originally
   -requested zone after expansion to descendants). The buyer's own `AnalyticsService._where` already special-cases
   this key away; `ProducerAnalyticsService._where` (Phase C) never needed to until this phase started calling
   `resolve_filters` — every producer metric request carrying a zone filter raised `Filter 'zone_scope' is not a
   dimension of ...` (caught in CI: `test_producer_admin_api_pg.py`'s zone-hierarchy/breakdown/timeseries tests all
   failed on the first push). Fixed identically to the buyer side, in both `_where` and `_apply_target` (the latter
   also needed the `zone_scope`-first fallback for zone-scoped target resolution to work at all). A plain unit test
   on the static `_where` method (no DB needed) now guards this without requiring a Postgres round-trip to catch a
   regression.

## Cache and performance
Same generic `cache.py::cached()` reused as-is (`ANALYTICS_CACHE_TTL_SECONDS`, default 180 s; filters 600 s; health
60 s) under a `producers:` key prefix so buyer and producer entries never collide in the same Valkey/Redis instance.
Reads hit only the 3 producer daily aggregates (never orders/bids/products at render time). A latency guard
(`test_overview_and_breakdown_latency_over_thirty_days`, < 5 s for a 30-day/50-producer window) mirrors the buyer
side's own gate.

## Limitations
- No Market Balance (Demand vs Supply) view — explicitly out of scope for this phase, per instruction. §29/§53 of
  `PRODUCER_ANALYTICS_ARCHITECTURE.md` already checked what a future Phase E would need on both sides.
- `producer_quantity_fulfillment_rate` cannot be filtered or broken down by zone (its table has no zone dimension);
  `producer_order_fulfillment_rate`/`producer_delivered_gmv` cannot be filtered by category/sub-category (their table
  has no such dimension either). Both report `UNAVAILABLE` with the reason rather than silently ignoring the filter.
- `repeat_producer_rate`'s window remains PROVISIONAL (not calibrated against real cadence data — a `DAILY` vegetable
  producer and a `MONTHLY` livestock producer have structurally different natural cycles).
- `time_to_first_sale` has no dedicated real-Postgres end-to-end test yet (disclosed gap carried over from Phase C,
  §52's "Known, disclosed gap" — the aggregation/service logic exists and is documented, including the RECURRING
  `Order.updated_at` imprecision, but was not proven against real data in this pass either).
