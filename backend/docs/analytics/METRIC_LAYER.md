# Buyer Metric Layer (Phase D)

From reliable facts (Phase C) to reliable buyer KPIs: daily aggregates, an idempotent refresh, and a
deterministic read layer (`AnalyticsService`). No dashboard, API, chart or cloud service here (Phase E).

## Source-of-truth rule

| Kind of fact | Source | Why |
|---|---|---|
| Current business state (orders, auctions, occurrences, allocations) | **Transactional tables** | Authoritative and fully historical (`created_at`, `occurrence_date`). |
| Behaviour that leaves no state (searches, digests queued/accepted) | **`analytics.business_events`** | Nothing else records them; no history before Phase C. |

Aggregates use both. Nothing is rebuilt from events when a transactional table already holds the truth
(e.g. recurring coverage comes from `requested_quantity` / `quantity_matched`).

## Tables (schema `analytics`, migration `0007`, Drizzle = source of truth, SQLAlchemy mirror)

All tables store **numerators and denominators, never rates**; are derived/rebuildable; have no FKs; use the nil
UUID (`00000000-…`, "not attributable") instead of NULL for dimensions so the UNIQUE grain index is real; carry
`computed_at`; enforce non-negativity with CHECK constraints.

| Table | Grain (unique index) | Cohort date | Main columns |
|---|---|---|---|
| `buyer_daily_metrics` | `(metric_date, buyer_id)` | need creation date (recurring: demand date) | `needs_{direct,tender,recurring}`, `satisfied_*`, `potential_gmv_*`, `confirmed_gmv_{tender,recurring}`, `delivered_gmv_*`, `digests_queued/accepted` (event-day basis) |
| `direct_daily_metrics` | `(metric_date, zone, category, sub_category)` | order `created_at` | `searches`, `successful_searches`, `orders_created`, `orders_delivered`, `created_value`, `delivered_value` |
| `tender_daily_metrics` | `(metric_date, zone, category, sub_category)` | auction `created_at` | `tenders_created`, `tenders_with_bid`, `bids_received`, `tenders_with_winner`, `tender_orders_created/delivered`, `first_bid_latency_seconds_sum/count`, `potential/committed/delivered_value` |
| `recurring_daily_metrics` | `(metric_date, zone, category, sub_category, canonical_unit)` | occurrence `occurrence_date` | `occurrences_{total,active,fully_covered,notified,accepted,skipped,with_orders,all_received}`, `needs_with_occurrence`, `requested/matched/confirmed/unmatched_quantity`, `potential/confirmed/received_value` |

Grain choices: `buyer_daily_metrics` is per buyer-day so `COUNT(DISTINCT buyer_id)` and repeat buyers stay exact over any
window (one row per *active* buyer-day — bounded by pilot volume). Journey tables stop at sub-category (no buyer, no
permutation explosion); category is stored with sub-category so category filters need no join. Only the recurring
table has a unit dimension because it is the only one aggregating physical quantities. Direct/tender order values are
money, not quantities; a multi-sub-category cart lands on the nil sub-category.

## Units

Recurring quantities are converted to the sub-category `priority_unit` when the occurrence unit converts to it
(`G → KG`); otherwise the occurrence keeps its own unit (`SAC`, `TETE`, unknown units are never converted). The
unit is part of the grain, so KG + L + TETE can never be summed. The service returns a `MIXED_UNITS` status with a
per-unit breakdown and **no** global physical numerator/denominator when a window holds several units.

## Refresh / recompute

`DailyMetricsRefresher.recompute_day(D)` rebuilds the four tables for UTC day D (facts fetched with light per-day
queries, aggregated by the pure functions of `domain/analytics/daily_aggregation.py`, `DELETE`+`INSERT` in one
transaction under a per-day advisory lock). Replays are byte-identical. Future days are refused.
- Any historical day: `recompute_day(date(...))` / `recompute_range(worker_session, start, end)`.
- Automatic: Celery task `workers.analytics_daily_metrics`, every 6 h, `recompute_recent` = today + previous 14 days
  (`ANALYTICS_DAILY_LOOKBACK_DAYS`).

## Late-arriving facts

Rows are **cohorts**: needs created that day + their *current* state. A delivery/receipt recorded today for an order
created last week updates last week's row at the next recompute of that day; today's row is unaffected. The rolling
window covers ordinary lateness; older corrections are a targeted recompute. Rates over young cohorts read low —
the service flags windows younger than 7 days (`MATURITY_DAYS`).

## Metrics

Implemented (`metric_layer.BINDINGS`): `needs_created`, `successful_procurement_rate` (PARTIAL), `potential_gmv` (PARTIAL),
`confirmed_gmv` (PARTIAL, excludes DIRECT), `delivered_gmv` (PARTIAL), `active_buyers`, `repeat_buyer_rate` (PARTIAL);
DIRECT `direct_searches`, `direct_search_success_rate`, `direct_search_to_order_rate` (PARTIAL), `direct_orders_created`,
`direct_order_delivery_rate`; TENDER `tenders_created`, `tender_response_rate`, `average_bids_per_tender`,
`time_to_first_bid`, `tender_winner_rate`, `tender_fulfillment_rate` (PARTIAL), `tender_gmv`; RECURRING requested/matched/
confirmed/unmatched quantity, `recurring_coverage_rate`, `recurring_full_coverage_rate`, `recurring_acceptance_rate`,
`recurring_skip_rate` (denominator = all occurrences), `recurring_received_occurrence_rate` (PARTIAL), `recurring_gmv`.

**UNAVAILABLE (value `null` + reason, never a proxy)**: `fulfillment_rate`, `direct_fulfillment_rate`, `direct_gmv`
(DIRECT confirmation not instrumented); `recurring_delivered_quantity` and `recurring_fulfillment_rate`
(`quantity_delivered` has no writer); `recurring_modification_rate` (not instrumented); `active_recurring_needs`
(gauge history not stored).

## North Star — Successful Procurement Rate

`SUM(satisfied) / SUM(needs)` with per-journey components and a `reliable_scope` (DIRECT+TENDER).
- DIRECT/TENDER satisfied = order delivered (`DELIVERED`/`FULFILLED`) — reliable.
- RECURRING satisfied = occurrence whose orders are **all** `RECEIVED`. The relation is exact by construction:
  `accept_match_proposal` writes `occurrence.order_group_id` and the orders' `checkout_group_id` in the same
  transaction. It does **not** depend on `quantity_delivered`. It is still a lower bound (a buyer who never confirms is
  counted unsatisfied) and the join has no FK, hence the global metric is flagged **PARTIAL**, never presented as a
  complete delivery rate. Cancelled needs stay in the denominator; skipped/cancelled occurrences are not needs.

## Unfulfilled demand

`get_unfulfilled_demand` = **UNMATCHED demand** (`requested − matched`, per occurrence, per canonical unit). It measures
the matching engine, not delivery; undelivered demand is reported as unavailable.

## Targets

`analytics.metric_targets` is read, never seeded. Resolution for a query: only scopes the query is filtered on apply
(a ZONE target never applies to an all-zones query); precedence `SUBCATEGORY > CATEGORY > ZONE > JOURNEY > GLOBAL`;
several rows of one scope → latest `valid_from` valid on the window's end date. Status bands: `ON_TARGET`, then
`BELOW_TARGET` / `WARNING` / `CRITICAL` using the warning/critical thresholds, mirrored for lower-is-better metrics
(`time_to_first_bid`, skip rate, unmatched quantity).

## AnalyticsService (`services/analytics/analytics_service.py`)

`get_buyer_overview`, `get_direct_metrics`, `get_tender_metrics`, `get_recurring_metrics`, `get_unfulfilled_demand`,
`get_metric_timeseries` (day/week/month; parts are summed per bucket), `get_metric_breakdown`, `compare_periods`, and
`get_metric` (previous equal-length period + delta by default). Response contract: `metric_name, value, numerator,
denominator, unit, status, period, target, target_status, previous_value, delta, breakdown, notes`.
Rates are always `SUM/SUM`; a zero denominator gives `value = null` (`NO_DATA`); an unsupported filter raises instead
of being ignored.

## Data quality (`services/analytics/data_quality.py`)

matched > requested, confirmed > matched, unknown canonical units, incompatible aggregation (a sub-category with several
measurement families), days with source facts but no aggregate, duplicate grain (also blocked by the unique index),
stale refresh (> 36 h). Negative values are impossible (CHECK constraints).

## Known limits

- Search/digest metrics have no history before the Phase C deploy.
- `direct_search_to_order_rate` is a window ratio, not a per-session funnel.
- `quantity_matched` is the *current* matching state (rematching overwrites it) — recurring coverage of past days
  reflects the latest state, not a snapshot.
- Refresh is batch (6-hourly), not streaming.
