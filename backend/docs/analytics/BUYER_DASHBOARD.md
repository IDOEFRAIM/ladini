# Buyer Analytics Dashboard (Phase E)

Admin-only cockpit that lets the Ladini team answer buyer questions without SQL. **No KPI is computed in
TypeScript**: every number, delta, status and target comes from `AnalyticsService` (Phase D).

## Route and access
- Page: `/admin/analytics/buyers` (frontend repo, `app/admin/analytics/buyers`), linked from the admin navbar.
- Access is ADMIN/SUPERADMIN only, enforced twice: `app/admin/layout.tsx` (redirect) and every endpoint (`requireAdmin`).
  Unauthenticated -> 401, non-admin -> 403, admin -> 200 (tested).

## Architecture (server-to-server adapter)
```
browser -> Next.js /api/admin/analytics/buyers/*   (requireAdmin, parameter whitelist, no-store)
        -> FastAPI /internal/analytics/buyers/*    (X-Internal-Token, fail-closed, read-only)
        -> admin_api.py -> AnalyticsService -> analytics.*_daily_metrics (+ metric_targets)
```
The browser never sees the backend URL or token (`LADINI_BACKEND_URL`, `INTERNAL_API_TOKEN`, server-side env only). The
router carries `require_internal_token` at router level, so a new route inherits the protection (architecture test
`test_http_entrypoints_are_authenticated`). Internal errors return a generic 500/502; nothing from the database or the
network leaks. No phone numbers, tokens or WhatsApp payloads are returned by any analytics endpoint.

## Endpoints (Next path -> backend path, all GET)
| Endpoint | Content |
|---|---|
| `overview` | 7 KPI cards, journey mix (needs / satisfied per journey), `not_available` list, freshness |
| `direct`, `tenders`, `recurring` | journey metric sets (recurring also lists its unavailable metrics) |
| `unmatched-demand` | UNMATCHED demand (requested − matched) and, separately, confirmed-but-not-received, per sub-category x zone x unit; totals per unit; paged (`limit` <= 200, `offset`) |
| `metrics/{metric}/timeseries` | day/week/month buckets (auto by window, or `granularity`) |
| `metrics/{metric}/breakdown` | by `zone_id`, `category_id` or `sub_category_id`, with labels (+ parent category), paged |
| `compare` | current vs previous period (default: the equal-length window right before) |
| `filters` | real taxonomy for the controls: categories, sub-categories (with `category_id`), zones (with `parent_id`, `depth`) |
| `health` | `Healthy` / `Warning` / `Stale`, freshness per table, data-quality issues |

Common filters: `from`, `to` (UTC calendar days `YYYY-MM-DD`, both inclusive; a time component is rejected, max 400 days),
`zone_id` (expands to the zone and all descendants), `category_id`, `sub_category_id`, `journey` (validated; rejects a
metric of another journey). Errors: 400 invalid parameter, 404 unknown metric, 400 unavailable metric on series endpoints.
Overview/direct/... under a category filter mark buyer-level metrics (which the buyer table cannot split by category)
`UNAVAILABLE` with the reason, never a silent 0.

## Response contract (one shape for every metric)
`metric_name, label, value, numerator, denominator, unit, status, reliability, period, target, target_status,
previous_value, delta, delta_kind, delta_points | delta_pct, breakdown, notes`.
- `status` (data on this window): `OK`, `PARTIAL`, `UNAVAILABLE`, `NO_DATA`, `MIXED_UNITS`.
- `reliability` (the metric itself): `RELIABLE`, `PARTIAL`, `UNAVAILABLE`.
- Rates: `delta_kind = percentage_points` (`delta_points`). Amounts/counts: `relative` (`delta_pct`).
- `null` is never rendered as 0: it shows `—`.

## Page sections and metric mapping
1. **Filters**: 7/30/90 days (default 30) or custom, zone (hierarchy), category, sub-category (depends on category). State lives in the URL
   (`period, from, to, zone, category, sub, tab`); invalid values are ignored.
2. **KPI cards**: active buyers, needs created, successful procurement (North Star, PARTIAL), repeat buyers, potential / confirmed / delivered GMV.
   The global `fulfillment_rate` is UNAVAILABLE and therefore not shown as a card.
3. **Journey mix**: needs and satisfied per journey.
4. **Tabs DIRECT | TENDER | RECURRING** (see below).
5. **Trends** (7 charts): active buyers, needs, successful procurement, confirmed GMV, recurring coverage, tender response rate, direct search success rate.
6. **Demand analysis**: recurring coverage by category, expandable to sub-categories (global zone filter still applies), with unit.
7. **Unmatched demand** and **Confirmed but not received** (two separate tables).
8. **Analytics health** badge with last refresh.

Tabs: DIRECT (searches, successful searches, success rate, orders created, orders per search, confirmed, delivered, fulfillment, GMV);
TENDER (created, with bid, response rate, average bids, time to first bid, winner rate, orders, fulfillment, GMV);
RECURRING (occurrences, requested / matched / confirmed / delivered quantity, coverage, full coverage, acceptance, skip, received-occurrence rate,
fulfillment, unmatched). `active_recurring_needs` and `recurring_modification_rate` appear only under "Metrics not yet available".
Secondary metrics live in tabs/breakdowns: 1 card is not 1 dictionary metric.

## Reliability, PARTIAL and UNAVAILABLE
- PARTIAL: number shown + visible `PARTIAL` badge + an info button revealing the notes and journey breakdown (e.g. North Star:
  "Reliable scope: DIRECT + TENDER. Recurring depends on buyer-confirmed RECEIVED orders").
- UNAVAILABLE: `—` + "Donnée indisponible" + reason. NO_DATA: `—` + "Aucune donnée sur la période". These are different states, and both differ from 0.
- Section states: loading, empty, error (with retry), stale-while-refreshing; per-metric states above.

## Units
Physical quantities always carry their canonical unit. When a window holds several incompatible units the API returns `MIXED_UNITS`
with a per-unit breakdown and no global numerator; the UI lists one line per unit (`4 200 KG`, `850 L`, `73 TETE`), never a bare total.
Unmatched-demand tables and totals are per unit. `direct_orders_per_search` is shown as "x cmd/recherche" (never `%`, never colored as an
error above 1) with the note "Window-level ratio; searches and orders are not session-attributed".

## Targets
None are seeded. `target = null` shows "Aucun objectif configuré" in a neutral style (no red/orange). With a target: value, target,
gap (points for rates), status (`ON_TARGET`, `BELOW_TARGET`, `WARNING`, `CRITICAL`; text + color).

## Freshness and health
`freshness.last_refresh` = latest `computed_at` across the four daily tables; stale after 36 h (same threshold as the data-quality check)
-> the badge reads "PÉRIMÉES" with an explicit warning. `Warning` = data-quality issues in the last 30 days (duplicate grain, unknown units,
incompatible aggregation, missing aggregate days, delivered > confirmed, quantity_delivered drift). A per-table `stale_refresh` alone
(a table with no facts) is not a warning.

## Cache and performance
Reads hit only the daily aggregates (never orders/bids/occurrences/events at render time). Optional short Valkey/Redis cache on the backend
(`ANALYTICS_CACHE_TTL_SECONDS`, default 180 s, 0 disables; filters 600 s, health 60 s). Valkey is never a source of truth; any cache failure
falls back to computing (tested). Tables are paged (breakdown <= 500, unmatched <= 200).

## Charts
Recharts (single library, React 19 compatible, small API, SVG). One line per unit for physical series; `null` stays a gap.

## Limitations
- Global `fulfillment_rate`, `recurring_modification_rate`, `active_recurring_needs` UNAVAILABLE; search -> order attribution not built.
- North Star, recurring fulfillment and delivered quantities are lower bounds (need buyer-confirmed RECEIVED).
- Buyer-level cards cannot be filtered by category/sub-category (the buyer table has no such dimension) and say so.
- Day boundaries are UTC; recompute lag (6-hourly refresh) is surfaced through freshness.

## Production deployment status (2026-09-27)

| Item | Status |
|---|---|
| Backend PR #18 (`/internal/analytics/buyers/*`) | merged, release `sha-adcaf64` deployed (`deploy.yml`, production): migrations 0006, 0007, 0008 applied by the official runner, smoke tests OK, worker + beat recreated healthy |
| Frontend PR #6 (cockpit + Next.js adapter) | **open, deliberately NOT merged** (see blocker) |
| `INTERNAL_API_TOKEN` | GitHub `production` environment secret present; the backend reads it from the app env. Match with the frontend server value NOT verified (no access to Vercel env) |
| `LADINI_BACKEND_URL` (Vercel, server-only) | not verifiable from CI tooling; **cannot be `https://api.ladini.tech`** (see blocker) |
| Refresh / jobs, first recompute, `backfill_quantity_delivered`, KPI-vs-SQL checks, cockpit E2E | **not executed**: no server (SSH/DB) or Vercel access in the closing session |

### Blocker found: the public API does not route `/internal/*`
`infra/reverse-proxy/Caddyfile` proxies only `/api/webhook*`, `/api/webchat/*`, `/health*`, `/version`; everything else is a 404
(same rule that keeps `/api/market/*`, `/admin/*`, `/metrics` private). Verified on production after the deploy:
`https://api.ladini.tech/internal/analytics/buyers/health` -> 404 with or without a token. A Vercel server therefore cannot reach the
analytics API through `api.ladini.tech`. Decide one path before merging the frontend:
1. private connectivity (tunnel / private network / fixed egress) from the Next.js server to the API container, with `LADINI_BACKEND_URL` pointing at it;
2. explicitly whitelist `/internal/analytics/*` in the Caddyfile (a policy change: the surface stays token-protected and fail-closed, but becomes internet-reachable);
3. host the adapter where it can reach the internal network.
Do not add a `NEXT_PUBLIC_*` variable in any case.

### Required env vars
Frontend server (Vercel, all environments that use the cockpit): `LADINI_BACKEND_URL`, `INTERNAL_API_TOKEN` (same value as the backend's).
Backend: `INTERNAL_API_TOKEN` (already used by `/api/market/*`), optional `ANALYTICS_CACHE_TTL_SECONDS`, `ANALYTICS_DAILY_LOOKBACK_DAYS`.

### Operational checklist after the blocker is resolved
1. Confirm the token match with a 200 on `GET <backend>/internal/analytics/buyers/health` (token in the header, never in logs/URLs).
2. Trigger a first recompute instead of waiting for the 6-hourly beat: `recompute_recent(worker_session, 14)` (or `recompute_range` for a chosen window) in the worker container.
3. `backfill_quantity_delivered(session)` is one idempotent `UPDATE ... FROM (SELECT ... RECEIVED orders of CONVERTED allocations)` touching only occurrences whose stored value differs from the source of truth; it has no dry-run, so count first with the data-quality check `quantity_delivered_drift`, then run once and expect 0 on the second run.
4. Compare at least five KPIs to independent SQL (active_buyers, needs_created, direct_search_success_rate, tender_response_rate, recurring_coverage_rate with SUM(matched)/SUM(requested) on the same window and unit, one unmatched-demand sub-category).
5. Check `health` (Healthy / Warning / Stale) and freshness, then merge the frontend PR and repeat the checks through `/admin/analytics/buyers`.
