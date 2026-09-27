# Market Balance (Phase E) — Buyer Demand ↔ Producer Supply

**Status: AUDIT (§1–16) then IMPLEMENTATION (§17 onward), same discipline as every prior analytics
phase — every verdict below is either a fact proven by reading the code (cited file:line) or a
design decision explicitly flagged as a decision, never invented to fill a gap.** Companion to
`BUYER_ANALYTICS_ARCHITECTURE.md`, `PRODUCER_ANALYTICS_ARCHITECTURE.md`, `METRIC_LAYER.md`,
`PRODUCER_METRIC_LAYER.md` — this phase does not change any of those, only combines their outputs.

## 1. Objective

Put buyer demand and producer supply side by side — by zone, category, sub-category, canonical
unit — to answer: where does supply fall short of demand, where does supply sit unused, which
sub-categories are under-covered, how much demand is genuinely unmet right now, how much supply is
available right now, where should the team recruit producers, where should it recruit buyers.

## 2. The trap this phase exists to avoid

`unmatched_demand` (Buyer Analytics) can be summed over a **period** (a flow/cohort: "demand that
arrived in the last 30 days and wasn't matched"). `available_supply` (Producer Analytics) is a
**live snapshot** ("what's sellable right now"). `unmatched_demand − available_supply` mixes a
flow with a stock — structurally wrong regardless of how clean either side's own number is. This
phase starts from a from-scratch definition of **current, still-actionable** demand, not a reuse of
the historical KPI.

## 3. Demand semantics per journey — audited, not assumed

### 3.1 DIRECT — no persisted quantified "open need" exists

- No `Cart`/`Wishlist`-style table exists anywhere in the schema (`domain/catalog`, `domain/orders`
  grepped) — a shopping cart is ephemeral/session state, never a DB row.
- A search (`DIRECT_SEARCH_PERFORMED`/`SUCCEEDED`) carries no quantity, no sub-category-quantified
  need — "tomate" is not "200 KG de tomate". Turning a search into a KG figure would be invented,
  exactly the mission's own example.
- The only place a *quantity* appears for DIRECT is inside an `Order`/`OrderItem` — by which point
  it is already a transaction in progress (already deducted from `quantity_for_sale`, §7 below), not
  an *open* need waiting for supply.

**Verdict: `CURRENT_OPEN_DEMAND` for DIRECT = UNAVAILABLE, quantitatively.** Search volume/success
rate remain valid **qualitative signals**, tracked separately (§30), never folded into a KG figure.

### 3.2 TENDER — exact, but binary (no partial-quantity concept exists)

- `Auction` (`domain/orders/models.py:341`) carries exactly one `quantity`/`unit` for the whole
  auction (the buyer's ask). `Bid` (`:411`) has **no quantity column at all** — only `offered_price`.
  A producer bids a *price*, never a *volume*: there is no schema support for a partial-quantity
  bid, so the mission's own warning ("ne suppose pas qu'un winning bid couvre nécessairement toute
  la quantité") resolves cleanly — **a winning bid always implies the full `Auction.quantity`**,
  because nothing else is representable.
- `Auction.status` (checked directly, not inferred): `OPEN` (default, `auction.py:371`) → `CLOSED`
  (winner selected, `auction.py:1604`) or `CANCELLED` (`auction.py:1810`, `buyer.py:2745`) or
  `EXPIRED` (a real cron, `check_and_expire_auctions`, `auction.py:1910-1929`, `WHERE status='OPEN'
  AND deadline<=now`). **No code path ever re-opens a `CLOSED` auction** (grepped every
  `status = "OPEN"` write site) — if the winning order is later cancelled, the auction stays
  `CLOSED` and its quantity is not resurfaced as open demand. A real, disclosed gap in the domain,
  not fixed here (out of scope: it's an order-cancellation-recovery feature, not an analytics one).
- Unit: `Auction.unit` defaults `'TONNE'`, a genuine `MASS`-family unit already convertible to KG
  via the existing, already-tested `resolve_canonical`/`to_canonical_quantity`
  (`domain/analytics/daily_aggregation.py:171-190`) — the same mechanism the buyer/producer layers
  already use, joined through `Auction.sub_category_id → SubCategory.priority_unit`. **New
  territory** in the sense that no existing buyer TENDER metric (`tenders_created`,
  `tender_response_rate`, `tender_gmv`, …) is quantity-based today — this phase is the first to
  canonicalize `Auction.quantity`, so it gets its own dedicated tests (§39).

**Verdict: `CURRENT_OPEN_DEMAND` for TENDER = `SUM(Auction.quantity)` WHERE `status='OPEN' AND
deadline > now`, converted to canonical unit. RELIABLE for the quantity itself (exact, binary,
no partial-bid ambiguity); PARTIAL as a demand-recency signal** (a cancelled-after-award auction's
demand silently disappears rather than reappearing — disclosed, not fixed).

### 3.3 RECURRING — the only source with a real "still actionable" state machine

- `RecurringNeedOccurrence.status` (`domain/recurring_supply/models.py:110-176`) has 12 values;
  the actual matching worker names the ONLY two that are still actionable, verbatim in its own
  code comment (`workers/automation/need_matching_service.py:50-53`):
  > "Occurrences encore éligibles à un (re)matching — jamais une occurrence qui a quitté ce
  > couple: SKIPPED/ACCEPTED/PARTIALLY_ACCEPTED/REJECTED/EXPIRED/FULFILLED/PARTIALLY_FULFILLED/
  > UNFULFILLED/CANCELLED ne sont JAMAIS modifiées par ce service."
  > `_MATCHABLE_OCCURRENCE_STATUSES = ("OPEN", "MATCHED")`
- Confirmed by the acceptance path itself (`services/database/recurring_supply.py:751-865`): the
  query that finds an occurrence to accept/reject explicitly requires
  `status.in_(("OPEN", "MATCHED"))`; accepting sets `occurrence.quantity_confirmed = <converted
  total>` (an overwrite, not an increment — an occurrence is accepted at most once) and
  `status = "ACCEPTED"` (fully covered) or `"PARTIALLY_ACCEPTED"` (partially covered) — **both
  terminal**. A `PARTIALLY_ACCEPTED` occurrence's remaining gap (`requested_quantity −
  quantity_confirmed`) can **never** receive another allocation — it is not "still open", even
  though the raw subtraction is positive. Reusing the historical `UNMATCHED_DEMAND` KPI's cohort
  (any occurrence created in a window, regardless of current status) would silently include this
  dead gap — exactly the trap ÉTAPE 4 warned about.
- **Real, disclosed gap found in this audit**: no code anywhere transitions an occurrence to
  `EXPIRED` (grepped `RecurringNeedOccurrence` + `EXPIRED` across `src/`) — the value exists in the
  `CHECK` constraint and in the matching worker's own docstring, but nothing ever sets it. A
  months-old `OPEN` occurrence that was never matched stays `OPEN` forever. Left as a **query-level
  operational-recency window** (below), not silently included as "current".

**Verdict: `CURRENT_OPEN_DEMAND` for RECURRING = `SUM(requested_quantity − quantity_confirmed)` WHERE
`status IN ('OPEN','MATCHED')` AND `occurrence_date` inside an operational-recency window (§6).
RELIABLE for the quantity itself** (exact, live, and the only journey whose state machine already
distinguishes "still actionable" from "closed" without extra invention) **— the most reliable of
the three, matching the mission's own hypothesis.**

## 4. Canonical `current_open_demand` contract

> `current_open_demand` = the quantity of a still-actionable need, right now, that no allocation
> has yet closed off — **not** "requested minus matched over the last 30 days", **not** "searches
> without a result", **not** "orders not yet delivered". A quantity stops being "open" the moment
> the domain's own state machine says it can no longer receive more allocation (§3), independent of
> whether the underlying need was ever actually fulfilled.

## 5. `current_available_supply` — reused, verified, not touched

Reuses `available_supply` (Producer Analytics Phase C/D) exactly as-is:
`SUM(Product.quantity_for_sale)` where `is_available=TRUE`, snapshotted daily
(`producer_supply_daily_snapshot`), never a historical series, MIXED_UNITS-safe. No new supply
concept is introduced. **Verified still correct for this phase's needs**: it groups by
`(producer_id, zone_id, category_id, sub_category_id, canonical_unit)` — exactly the dimensions
Market Balance needs on the supply side (`domain/analytics/producer_metric_layer.py:29-33`).

## 6. Reserved vs. free supply — the domain does not have a "reserved" bucket

Audited explicitly per the mission's own ÉTAPE 7 instruction ("ne fabrique pas la notion si le
domaine ne la supporte pas"):
- DIRECT debits `quantity_for_sale` directly at buyer-confirmation/escrow-payment — **no separate
  reserved state** (`PRODUCER_ANALYTICS_ARCHITECTURE.md` §3.2.G, re-confirmed here).
- TENDER never touches `quantity_for_sale` at all (§3.2.I of the same document).
- RECURRING: `need_matching_service.py`'s own module docstring states it explicitly (`:19-22`):
  > "Stock jamais consommé (mandat §7): Ce module ne fait AUCUN `UPDATE` sur
  > `products.quantity_for_sale`. Une allocation est une PROPOSITION, jamais une réservation."
  The debit only happens at `accept_match_proposal` (`recurring_supply.py:834`, PROPOSED→CONVERTED),
  i.e. exactly when the quantity actually leaves `current_open_demand` (§3.3) on the demand side.

**Verdict: `GROSS_AVAILABLE_SUPPLY == FREE_AVAILABLE_SUPPLY`** in this codebase today — there is
no reservation layer to net out. `available_supply` already represents what a producer can sell
right now. Re-open this finding only if a `MarketOffer`-style reservation
(`available_quantity`/`reserved_quantity`, catalog/models.py — the separate future-preorder
subsystem) is ever folded into the DIRECT/TENDER/RECURRING supply this phase measures — it is not,
today.

## 7. Operational-recency window for RECURRING occurrences (a decision, not a fact)

Because nothing expires a stale `OPEN`/`MATCHED` occurrence (§3.3), `current_open_demand` scopes to
`occurrence_date BETWEEN (today − LOOKBACK_DAYS) AND (today + LOOKAHEAD_DAYS)`.
**PROVISIONAL, exactly like `repeat_producer_rate`'s window** — not calibrated against real
cadence data in this pass. Chosen defaults: `LOOKBACK_DAYS = 3` (a short grace period for
occurrences the matching cron or the buyer haven't yet acted on), `LOOKAHEAD_DAYS = 7` (near-future
scheduled occurrences a producer could still plan against). Configurable
(`MARKET_BALANCE_LOOKBACK_DAYS`/`MARKET_BALANCE_LOOKAHEAD_DAYS`), not hardcoded silently.

## 8. Units

Reuses `resolve_canonical`/`to_canonical_quantity`/`measurement_family_of` exactly as-is — no new
conversion logic. `G → KG` is allowed (existing MASS-family conversion); `SAC → KG` is not
(no defined conversion — `resolve_canonical` returns `SAC` unconverted, keeping it a distinct row);
`KG + TETE` are never summed (different `measurement_family`). A `zone/category/sub_category` scope
holding several canonical units returns `MIXED_UNITS` — a per-unit breakdown, never a fabricated
total — identical convention to every existing Metric Layer table.

## 9. Sub-category alignment

Both sides key on `sub_category_id` (never `category_label`, the free-text field already flagged as
drifting from `sub_category_id` on both the buyer and producer audits). Demand: `Auction.sub_category_id`
/ `RecurringNeed.sub_category_id` (via occurrence → need). Supply: `Product.sub_category_id` (via
`producer_supply_daily_snapshot`, already grouped this way).

## 10. Geographic alignment — a real finding, not the assumed risk

- `Product` has **no `zone_id` and no `farm_id`** at all (`domain/catalog/models.py:222-278`
  grepped in full) — a product's only geography is its producer's own `Producer.zone_id`
  (single-valued, nullable). **A product's supply belongs to exactly one zone, never several** —
  the double-counting risk ÉTAPE 11 warned about (one producer's stock counted in 3 zones at once)
  **does not exist in this domain's data model.** `producer_supply_daily_snapshot` already groups
  by this single zone (`producer_metrics_refresh.py`'s `snapshot_producer_supply`, joined
  `LEFT JOIN marketplace.producers pr ON pr.id = p.producer_id`) — no fan-out, confirmed by
  reading the query.
- Demand-side zone: RECURRING uses the buyer's own `auth.users.zone_id` (`buyer_profiles` has none
  of its own — `need_matching_service.py:12-17`'s own comment confirms this, and that it's already
  the established convention elsewhere, e.g. `auction.py::create_auction`'s
  `target_zone_id = user_obj.zone_id`). TENDER has its own **explicit** `Auction.target_zone_id`
  (the buyer's chosen delivery zone for that specific auction — more precise than the buyer's
  registered zone, used as-is).
- **A second real finding**: the matching engine's own candidate query
  (`need_matching_service.py:114-121`) selects producers by `sub_category_id` **only** — no zone
  filter at all. `same_zone` (`:211`) is a low-priority *tie-breaker* inside
  `allocate_single_source`'s ordering ("fournisseur unique > fiabilité > prix > **zone** > partiel
  individuel"), not a hard eligibility rule. **Cross-zone matching already happens in production
  today.** A zone-scoped Market Balance breakdown is therefore a **descriptive sourcing/recruiting
  lens** ("where does LOCAL supply meet LOCAL demand"), not a proxy for "what could actually be
  matched" — the dashboard states this explicitly (§13/§28) rather than implying zone-local balance
  equals real matchability.

## 11. Scope decision for Market Balance V1 (ÉTAPE 12)

| Journey | current_open_demand reliability | Included in V1 quantitative balance |
|---|---|---|
| RECURRING | RELIABLE (exact, live, real state machine) | **Yes — primary/reliable_scope** |
| TENDER | RELIABLE for the quantity, PARTIAL for demand-recency (cancelled-after-award gap) | **Yes, labeled PARTIAL** |
| DIRECT | UNAVAILABLE (no persisted quantified need) | **No** — search signals shown separately (§30), never summed into a KG figure |

`reliable_scope = "RECURRING"` when only RECURRING contributes to a cell;
`"RECURRING+TENDER"` when a TENDER auction also matches that zone/sub-category/unit. Never a single
undifferentiated "all journeys" figure — exactly the discipline `successful_procurement_rate`
(buyer North Star) already established for a mixed-reliability metric.

## 12. Formulas (facts only — no arbitrary score)

For each `(zone_scope, category_id, sub_category_id, canonical_unit)` cell, `as_of` = snapshot time:

```
open_demand_quantity        = SUM(current_open_demand) across included journeys (§11)
available_supply_quantity   = current_available_supply (§5), same cell
potential_coverable_quantity = MIN(open_demand_quantity, available_supply_quantity)
demand_gap_quantity          = MAX(open_demand_quantity - available_supply_quantity, 0)
excess_supply_quantity       = MAX(available_supply_quantity - open_demand_quantity, 0)
potential_coverage_rate      = potential_coverable_quantity / open_demand_quantity
                               (NULL, never 0/0, when open_demand_quantity == 0)
```

Called **POTENTIAL** coverage throughout (UI and API) — no allocation has actually happened between
these two numbers; this is not a fulfillment rate. Edge cases (§15 mission ÉTAPE): demand>0/supply=0
→ full gap; demand=0/supply>0 → full excess; both 0 → "no observable activity" (a distinct row
state, not computed as 0/0).

## 13. Snapshot model

`analytics.market_balance_daily_snapshot` — grain `(snapshot_day, zone_scope, category_id,
sub_category_id, canonical_unit, demand_scope)` (`demand_scope` = which journeys contributed,
e.g. `"RECURRING"` / `"RECURRING+TENDER"` — part of the grain because the same cell computed with a
different journey mix is not the same fact). Columns store only the raw quantities
(`open_demand_quantity`, `available_supply_quantity`, `potential_coverable_quantity`,
`demand_gap_quantity`, `excess_supply_quantity`) — never `potential_coverage_rate` (reconstructible
from the components, same discipline as every other Metric Layer table: numerators/denominators
only, never a stored rate). Idempotent DELETE+INSERT per `snapshot_day`, one snapshot per day (or
after each analytics refresh if that proves more useful operationally — daily is enough for the
pilot). **No historical backfill**: history is `FROM_PHASE_E_ONLY` — reconstructing a past day's
`available_supply` from today's live catalog would be fabricated, exactly the mission's own ÉTAPE 18
warning.

## 14. API and dashboard

See `PRODUCER_DASHBOARD.md`/`BUYER_DASHBOARD.md` for the (unchanged) surrounding architecture this
reuses. Endpoints and page sections are documented together with their implementation below,
updated as each is built (§20 onward in the mission — tracked in this same file's later sections
once implemented, to avoid two documents drifting apart).

## 15. Data quality

See `run_market_balance_quality_checks` (implemented alongside the service): unit incompatible
comparison, orphan sub-category, missing zone, negative demand/supply, duplicate snapshot grain,
stale supply snapshot, stale demand refresh, stale market-balance snapshot itself.

## 16. Historical coverage

`available_supply`: `FROM_PHASE_E_ONLY`, gauge-only, exactly like the producer dashboard's own
supply gauge. `current_open_demand`: reconstructible only from this phase's snapshot forward for
the same reason — RECURRING/TENDER's *current* state (which occurrences are still `OPEN`/`MATCHED`,
which auctions are still `OPEN`) is not preserved historically anywhere upstream; only the
snapshot's own history is a real time series.

## 17. Limitations (carried into the dashboard, not hidden)

- DIRECT is not part of the quantitative balance — a real gap, not an oversight (§3.1).
- TENDER's demand can silently vanish if a post-award order is cancelled (§3.2) — a domain gap, not
  fixed by this phase.
- RECURRING's operational-recency window (§7) is PROVISIONAL, not calibrated.
- Zone-scoped balance is a sourcing/recruiting lens, not a matchability guarantee (§10) — matching
  already crosses zones today.
- No Market Health Score, no arbitrary target — facts only (§12), per explicit instruction.
