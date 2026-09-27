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

## Production deployment status (updated 2026-09-27, second pass)

| Item | Status |
|---|---|
| Backend PRs #18, #20, #21, #22, #23, #24 | merged; production release `sha-ba90cea` deployed (`deploy.yml`): migrations 0006-0008 applied, smoke OK, worker/beat/Caddy healthy |
| `/internal/analytics/*` reachable from the public domain | **DONE** — Caddy now proxies exactly this sub-path (see below); everything else under `/internal/*`, all of `/admin/*`, `/api/market/*`, `/metrics` still 404 |
| FastAPI auth on that route | fail-closed, confirmed live: no token -> 401, wrong token -> 401, real deployed token -> 200 (never printed; read from the deployed `.env` by a workflow step and used only in an HTTP header) |
| First recompute / drift / backfill / data quality | **DONE**, live production data (below) |
| 6-way KPI cross-check (dashboard vs a fresh independent query on the transactional tables) | **DONE, all 6 match**, on real production data |
| 10-endpoint smoke test with the real token | **DONE, all 200**, clean JSON, real taxonomy/mixed-units/PARTIAL/NO_TARGET behavior confirmed live |
| Frontend PR #6 (cockpit + Next.js adapter) | **still open, deliberately NOT merged** — blocked on Vercel access (below) |
| `INTERNAL_API_TOKEN` | CONFIGURED on the backend (GitHub `production` environment secret, read into the app env) and **MATCH CONFIRMED** (the live 200 above proves it). Vercel-side value: **not verifiable, no Vercel access** |
| `LADINI_BACKEND_URL` (Vercel, server-only) | now correctly `https://api.ladini.tech` (the blocker below is resolved) — **cannot verify or set it in Vercel** |

### Blocker resolved: Caddy now routes `/internal/analytics/*`
`infra/reverse-proxy/Caddyfile` gained one precise `@analytics_internal path /internal/analytics/*` block, proxying to the API exactly
like the existing public paths — nothing else under `/internal/*` (there is nothing else there today) or under `/admin/*`, `/api/market/*`,
`/metrics` is opened. FastAPI's own router-level `Depends(require_internal_token)` remains the real gate.

**A second, real bug was found and fixed while rolling this out**: Caddy bind-mounts `Caddyfile` as a single *file*, which Docker pins to
the inode present at mount time. The deploy's `git checkout -- infra/` replaces that file via rename (a new inode), so the
already-running container's mount kept pointing at the old, orphaned inode — `docker exec caddy reload` faithfully reloaded the STALE
content and reported success (a "green" deploy that silently changed nothing). Proven live: the file on disk already had the new route
while `docker exec caddy cat /etc/caddy/Caddyfile` still showed the old one. Fixed in `node_deploy.sh`: the Caddy service is now
`--force-recreate`d on every deploy instead of reloaded, which re-establishes the bind mount on the current inode. Verified on the very
next real deploy (`sha-ba90cea`): the route survived without any manual recovery step.

### Vercel blocker — still open, human action required
I have no Vercel CLI/token/dashboard access from this environment (no `vercel` binary, no `VERCEL_*`/Vercel-related secret anywhere in
either GitHub repo). Per the closing mission's own instruction, this stops the frontend rollout here rather than guessing. A human needs to,
in the Vercel project settings for `ladinifront` (Production environment, and Preview if the cockpit should be testable pre-merge):
1. Set `LADINI_BACKEND_URL=https://api.ladini.tech` (server-only — **never** `NEXT_PUBLIC_LADINI_BACKEND_URL`).
2. Set `INTERNAL_API_TOKEN` to the exact same value as the backend's (the GitHub `production` environment secret of the same name) —
   server-only, never `NEXT_PUBLIC_*`.
3. Merge PR #6, let it deploy, then re-run the checklist below through the real `/admin/analytics/buyers` page (browser + admin session)
   to get genuine browser-level proof — everything server-to-server has already been proven from the backend side (this document).

### Live production evidence (window 2026-09-14 → 2026-09-27, real pilot data)
- **Recompute**: `recompute_recent` over 14 days recomputed 15 calendar days; the 3 most recent days carry real rows
  (`direct_daily_metrics`, `recurring_daily_metrics`, `buyer_daily_metrics`); older days in the window are legitimately empty (pilot start).
- **`quantity_delivered` drift**: 0 occurrences diverging from the RECEIVED-order source of truth. **Backfill**: 0 rows updated (consistent
  with 0 drift) — there was no historical drift to correct in this pilot's data, so `backfill_quantity_delivered` was a confirmed no-op, not
  skipped out of caution.
- **Data quality** (last 30 days): 1 benign `WARNING` — `stale_refresh` on `tender_daily_metrics`, because there has been **zero** tender
  activity ever in this pilot (the table has no rows to refresh, not a broken job). No `ERROR`-severity issue.
- **6-way KPI cross-check** (`verify-kpis`, dashboard value vs. a query against `marketplace.*`/`analytics.business_events` computed fresh
  in the same run, never against the stored aggregate): **all 6 matched** — `active_buyers` (2 = 2), `needs_created` (7 = 7),
  `direct_search_success_rate` (no data either side), `tender_response_rate` (no data either side), `recurring_coverage_rate` (4 groups,
  0 mismatches, after a first run found and fixed a real bug — see next paragraph), `unmatched_demand` for the largest sub-category
  (250 KG = 250 KG).
- **A real verification bug found and fixed in the same closing pass**: the first `verify-kpis` run flagged `recurring_coverage_rate` as a
  mismatch (dashboard denominator 487 vs. a same-unit-only source query's 425) — production genuinely has a G→KG occurrence, and the
  check's *own* source query had excluded it by only summing occurrences already in their sub-category's `priority_unit`. Fixed by
  re-aggregating raw occurrence rows through the same pure, unit-tested `resolve_canonical`/`to_canonical_quantity` helper and comparing
  group-by-group (`sub_category_id` × `canonical_unit`) instead of one loose total; re-run confirmed 0 mismatches. Not a Metric Layer bug —
  a bug in the independent check that the check itself caught before being trusted.
- **Mixed units confirmed live** (not just in tests): `recurring_requested_quantity` for the whole window returns `status: MIXED_UNITS`,
  `value: null`, and a breakdown of `{"KG": 425.0}` / `{"TETE": 62.0}` — no combined number anywhere.
- **North Star live**: `successful_procurement_rate` returns `status: PARTIAL`, a `breakdown` with DIRECT/TENDER/RECURRING plus a
  `"DIRECT+TENDER (reliable_scope)"` entry, and RECURRING explicitly marked `"reliability": "PARTIAL"` while the others are `"RELIABLE"`.
- **Targets**: every metric in this window returned `target: null`, `target_status: "NO_TARGET"` — none seeded, as intended.
- **Health**: `"status": "Healthy"`, last refresh ~33 minutes old at check time, 0 quality issues surfaced through the health endpoint
  (the tender `stale_refresh` warning above is filtered out of `health` by design — a table with zero rows ever is not "unhealthy").
- **10/10 internal endpoints** (`overview`, `direct`, `tenders`, `recurring`, `unmatched-demand`, `filters`, `health`,
  `metrics/.../timeseries`, `metrics/.../breakdown`, `compare`) returned HTTP 200 with clean JSON — no `NaN`, no `Infinity`, no 500.
- **`/filters`** returned the real taxonomy: 5 categories, 9 sub-categories, 4 Burkina Faso zones (Bobo-Dioulasso, Ouagadougou, Pabré,
  Saaba) — confirming this is genuinely live data, not a fixture.

### Required env vars (unchanged)
Frontend server (Vercel): `LADINI_BACKEND_URL`, `INTERNAL_API_TOKEN` (server-only, never `NEXT_PUBLIC_*`).
Backend: `INTERNAL_API_TOKEN` (already used by `/api/market/*`), optional `ANALYTICS_CACHE_TTL_SECONDS`, `ANALYTICS_DAILY_LOOKBACK_DAYS`.

### Operational tooling added while closing this out
`.github/workflows/analytics_ops.yml` (manual approval, same self-hosted runner/environment as `deploy.yml`) exposes: `recompute`,
`drift`, `backfill`, `quality`, `verify_kpis`, `smoke_endpoints`, `verify_public_route`, plus the one-off `debug_caddy`/`debug_repo`/
`force_recreate_caddy` actions used to find and fix the Caddy bug above (kept as a recovery lever). Every action prints only aggregate
JSON/counts — no secret, no PII, ever.

### Remaining checklist (blocked on the human Vercel steps above)
1. Set the two Vercel env vars, merge #6, confirm the Vercel deploy.
2. Open `/admin/analytics/buyers` as an admin session; confirm the same 200/data already proven server-to-server now render correctly
   through the browser, and that a non-admin gets 403 / an unauthenticated visitor gets redirected — this is the one check that
   genuinely needs a browser and could not be done from here.
3. Re-run `analytics_ops.yml -> smoke_endpoints` occasionally as real traffic accumulates (tender/direct-search data is currently sparse).
