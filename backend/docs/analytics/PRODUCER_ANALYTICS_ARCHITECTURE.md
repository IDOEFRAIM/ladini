# Producer Analytics Architecture — Phase A (Audit + Design) + Phase B (Instrumentation)

**Phase A status: AUDIT ONLY.** Everything in §1–34 below is either a fact proven by reading the code (cited file:line) or a design decision explicitly flagged as a decision, never invented to fill a gap. Companion to `BUYER_ANALYTICS_ARCHITECTURE.md`, `BUSINESS_EVENT_CATALOG.md`, `METRIC_LAYER.md` — the buyer-side infrastructure this reuses without change.

**Phase B status: IMPLEMENTED (merged)** — see §35-49. The bug fix, the two new SUPPLY events, `producer_id` enrichment, and the exhaustive writer instrumentation are real, tested code, merged via [ladini#27](https://github.com/IDOEFRAIM/ladini/pull/27) + [ladinifront#7](https://github.com/IDOEFRAIM/ladinifront/pull/7).

**Phase C status: IMPLEMENTED** — see §50 onward. The 8 pilot KPIs from §48 are now live and queryable (`ProducerAnalyticsService`), backed by 3 new daily-aggregate tables (migration `0010`). Full design record in `docs/analytics/PRODUCER_METRIC_LAYER.md` — this section only summarizes what changed and points there for detail, to avoid duplicating (and drifting from) that document.

**Phase D status: IMPLEMENTED (merged)** — a mandatory MIXED_UNITS micro-gate on `available_supply` passed with zero production-code change, then the Admin API (`/internal/analytics/producers/*`) and the `/admin/analytics/producers` dashboard were built, mirroring the Buyer Analytics Phase E architecture exactly (same `requireAdmin` -> internal-token router -> service layering, same response contract, same reused UI components). Sell-Through and Demand Exposure remain UNAVAILABLE, exactly as §4/§7/§42/§51 concluded — no formula invented. Full design record in `docs/analytics/PRODUCER_DASHBOARD.md` — this section only summarizes what changed and points there for detail.

**Phase E (Market Balance) status: IN PROGRESS (PR open, not yet merged)** — the producer-side `available_supply` this document scoped as RELIABLE (snapshot-only) is now one half of the cross-cockpit Market Balance view (buyer demand ↔ producer supply). See [MARKET_BALANCE.md](MARKET_BALANCE.md) for the full semantic audit and implementation record — not duplicated here.

---

## 1. Objective

Measure the producer side of the same three journeys already instrumented for buyers (DIRECT, TENDER, RECURRING): supply → demand exposure → sale → delivery → revenue → repeat/retention. The central risk this phase exists to prevent: building a "Sell-Through Rate" on a denominator that doesn't actually mean what it claims to mean. That risk is real in this codebase — see §3.

## 2. Producer identity

**Canonical producer identity = `Producer.id`** (`domain/identity/models.py:145-193`, table `marketplace.producers`), never `auth.users.id` directly (a `User` can hold a `Producer` row and a `BuyerProfile` row simultaneously — proven dual-role, same as the buyer-side finding) and never `marketplace.clients` (a producer-owned CRM contact record with no FK to `auth.users` at all).

Fields: `user_id` (FK, **unique** — true 1:1 with `User`), `organization_id` (FK `governance.organizations`, nullable), `zone_id` (FK `governance.zones`, nullable), `business_name`, `status` (default `"PENDING"`, **no CHECK constraint, no enum**), `is_certified` (bool, default False), `region`/`province`/`commune` (free text, independent of `zone_id`'s hierarchy — same redundant-location pattern already flagged on `User` in the buyer audit), `rating`/`reviews_count`, `company_registration_number`, `created_at`/`updated_at`. No `verified_at`/`activated_at` (contrast: `BuyerProfile` has `verified_at`).

**Real finding, not a guess**: `Producer.status` is set to `"PENDING"` at every creation site and **never reassigned anywhere in the codebase** (grepped every `producer.status =` / `Producer.status` write across `src/`; the only other hit is a read for a display dict). There is no admin-approval workflow, no PENDING→ACTIVE transition. **`Producer.status` cannot be used for "active producer" — it would read 100% PENDING.**

Producer creation is immediate and self-service, 3 sites: onboarding (`services/database/auth.py:74-81`), `create_user_profile` (`services/database/base.py:227-234`), and auto-vivification the first time a producer-flow tool runs (`services/database/producer.py:384-389`, `:448-454` — note: this path sets `Producer.id = user_obj.id`, i.e. the PK is deliberately the same UUID as the User's, an inconsistent pattern versus the FK-based creation sites, worth flagging but not fixing here).

**Cooperative**: `Producer.organization_id` exists structurally but **no code path ever writes it** (no onboarding flow sets it). The separate `UserOrganization` join table (`governance/models.py:49-73`, field-agent/zone-manager governance construct) has **zero readers or writers anywhere in `src/`** — dead code, not a cooperative mechanism. **Conclusion: there is no populated cooperative dimension today.** A `cooperative` analytics dimension is not usable until (a) something actually writes `Producer.organization_id`, or (b) it's deliberately built later.

**Trust/reputation**: `TrustScore` (`identity/models.py:315-331`, schema `intelligence`) is schema-generic (1:1 per `User`) but **the only writer in the whole codebase is `buyer.py:760-808::rate_delivery`, keyed on the delivery AGENT's user_id** — not a producer trust signal. `Producer.is_certified` has zero writers anywhere (and shares its name with an unrelated conversational-agent state flag in `market_coach/core/state.py` — a real naming collision to watch for, not the same thing).

**Certification/KYC**: `identity_verified`/`cnib_number` are User-level, not Producer-specific.

## 3. Supply / Stock lifecycle — THE critical section

Two **structurally separate** "stock" systems exist in this codebase. Confusing them was the exact trap this phase was commissioned to avoid.

### 3.1 `marketplace.stocks` / `stock_movements` — a real ledger, for the wrong thing

`Stock` (`catalog/models.py:137-172`) belongs to a `Farm` (`farm_id` FK), not a `Product` — `item_name` is free text with **no FK to `Product` or `SubCategory` at all**. `StockMovement` (`:175-194`) is a genuine append-only ledger: `type` (`IN`/`OUT`), `quantity`, `reason`, `created_at`, written exactly once, by `add_stock_movement` (`services/database/producer.py:2470-2520`, the `ADD_STOCK_MOVEMENT` intent from the mission brief — confirmed live/wired into the conversational agent, not dead code: reachable from `graphs/agents/market_coach/actions/stock.py` and exposed via MCP). It correctly does IN/OUT with a reason, on a real ledger. **It has no relationship to the marketplace catalog buyers actually purchase from.** Two other `StockMovement` writers exist in `marketplace.py:177,245` — same Farm/Stock concept, unrelated to Product.

### 3.2 `Product.quantity_for_sale` — the real sellable inventory, with NO ledger at all

This is the counter buyers actually debit at checkout. It is a **pure mutable snapshot**, decremented/incremented directly, with **zero append-only history**. Every writer, classified:

| Site | Direction | Meaning |
|---|---|---|
| `producer.py:595-653` (`create_product`) | sets initial value | First "new supply" moment — **dated** (`Product.created_at`), reconstructible. |
| `product.py:107` (`update_product`) | **absolute overwrite** | Producer sends a new number; **no record of the prior value, the delta, or the direction** (restock vs. correction vs. typo fix — all indistinguishable). |
| `product.py:279` (`toggle_product_availability`) | **destructive** | **Real bug, not a design choice**: fakes on/off by setting `quantity_for_sale` to `1.0`/`0.0` — a "resume" after a pause **permanently loses the real quantity** (500 KG paused → resumed becomes 1 KG, forever). Never touches the actual `is_available` boolean that exists for exactly this purpose. |
| `product.py:355` (`delete_product`) | sets to 0 | Soft-delete when order history exists; combined with `is_available=False`. |
| `buyer.py:2360` (`confirm_preorder_draft`, cash), `escrow.py:297` (`mark_escrow_paid`, escrow) | decrease | DIRECT sale debit — see §3.3. |
| `buyer.py:1049`, `producer.py:1953` | increase | Cancellation recredit (buyer- and producer-initiated respectively) — exact inverse of the sale debit. |
| `recurring_supply.py:834` | decrease | RECURRING allocation-acceptance debit. |
| `marketplace.py:480` | sets to 0 at creation | `record_sale`'s phantom product (walk-in sale bookkeeping artifact, not real supply). |

**Answering the mission's audit questions (A–J) directly:**
- **A/B.** "Current stock" = the live value of `quantity_for_sale`, read now. A "movement" only exists, formally, for the unrelated Farm/`Stock` system (§3.1).
- **C/D.** No append-only ledger exists for Product-level supply — **only snapshots** (the current value; `create_product`'s initial value is the one historically dated snapshot we have).
- **E.** We **cannot** reconstruct the supply made available during a past period. We can reconstruct the *initial* listed quantity per product (`created_at` + `quantity_for_sale` at INSERT, via the migration/audit trail — not literally stored historically today either, but the row's current value is the only value, so no drift there for products never updated). Any subsequent restock/adjustment is invisible retroactively.
- **F.** A sale debits `quantity_for_sale` directly (no separate "confirmed" bucket), at the moment described in §3.3, and re-credits it exactly on cancellation.
- **G.** Functionally yes for DIRECT/RECURRING (see §3.3: the debit happens before producer confirmation in the cash path), but there is **no distinct "reserved" state/column** — it's a direct debit, indistinguishable from a completed sale until you also read `Order.status`.
- **H.** RECURRING debits `quantity_for_sale` at occurrence-acceptance (`recurring_supply.py:834`), one debit per `NeedAllocation`→`OrderItem`.
- **I.** **TENDER never touches `Product.quantity_for_sale` at all.** Confirmed by direct grep: `select_winning_bid` creates an `Order` with `auction_id`/`winning_bid_id` set, but **creates zero `OrderItem` rows** and references no `Product` row. A tender commitment is a pure promise (`Auction.quantity` × `Bid.offered_price`), with **no inventory linkage whatsoever**. This is a major structural finding: TENDER supply cannot be measured against "listed supply" the way DIRECT can, because no listing is debited.
- **J.** DIRECT debits at buyer-confirmation (cash, `buyer.py:2360`) or payment-confirmation (escrow, `escrow.py:297`) — **before** producer acceptance in the cash path.

A third, separate supply concept exists for completeness: `MarketOffer` (`catalog/models.py:78-`, future-production preorder) has its own, **richer** triad — `available_quantity`/`reserved_quantity`/`current_stock` — a genuine reservation concept, but this is the separate future-harvest-preorder subsystem (already noted in the buyer-side audit as not yet mapped to a journey), not the DIRECT/TENDER/RECURRING Product-based supply.

### 3.3 Can we reconstruct historical supply? — the mission's central question, answered directly

| Concept | Historically reconstructible? |
|---|---|
| **Quantity sold/debited** (DIRECT, RECURRING) | **YES, via `OrderItem`+`Order`** (exact ledger exists *implicitly* through orders — filter out `CANCELLED`). This does NOT require `business_events`; it's a plain SQL join over transactional tables, same principle as the buyer-side Metric Layer's "current state is authoritative" rule. |
| **Quantity sold/debited** (TENDER) | **NO** — no `OrderItem` exists; only `Auction.quantity` × the winning `Bid.offered_price` as a lump commitment, no per-unit ledger. |
| **Quantity newly listed/restocked** (any journey) | **NO**, beyond a product's very first `quantity_for_sale` at `create_product` time. Every `update_product` call is a value overwrite with no history. |
| **Current available supply** | Trivially available **as of now** (a live `SUM(quantity_for_sale)` query) — not a historical time series unless snapshotted going forward. |

## 4. Definitions

Following the mission's own vocabulary, precisely, with a verdict on each:

- **CURRENT_AVAILABLE_SUPPLY** = `SUM(Product.quantity_for_sale)` for a producer's `is_available = TRUE` products, evaluated **now**. **RELIABLE**, but a live gauge only — no historical series without new snapshotting.
- **NEW_SUPPLY_LISTED** (period) = quantity a producer newly made available in a window. **UNAVAILABLE historically** (no ledger); **PARTIAL, and only going forward**, if `PRODUCER_SUPPLY_PUBLISHED`/`PRODUCER_SUPPLY_ADJUSTED` are instrumented (§15) — and even then, "restock" vs. "correction" vs. the toggle-bug are indistinguishable without a product decision on `update_product`'s semantics.
- **SELLABLE_SUPPLY** = `Product` rows where `is_available = TRUE AND quantity_for_sale > 0`. Same reliability as CURRENT_AVAILABLE_SUPPLY (live only).
- **GROSS_SUPPLY_OFFERED**: for DIRECT this equals NEW_SUPPLY_LISTED (same UNAVAILABLE/PARTIAL verdict). For TENDER, there is **no producer-declared supply-offered quantity at all** — a bid is a price on the *buyer's* stated `Auction.quantity`, never the producer's own volume; "gross supply offered" is not a meaningful TENDER concept without a business decision to introduce one (e.g. asking a bidder to state their own available quantity, which the schema doesn't capture today).

### Producer Sell-Through Rate — verdict

**UNAVAILABLE today.** Not historically computable (the denominator, new supply listed, has no ledger), and not uniformly definable across journeys even prospectively (TENDER has no inventory linkage at all — a "sell-through" for TENDER would need an entirely different, not-yet-designed formula, likely bid-volume-based rather than stock-based). **No formula is proposed here** — inventing one now would be exactly the trap the mission asked to avoid. Phase B path to close this gap: instrument `PRODUCER_SUPPLY_PUBLISHED`/`PRODUCER_SUPPLY_ADJUSTED` (§15) going forward, get a product decision on restock-vs-correction semantics, then re-open this definition — DIRECT/RECURRING only; TENDER stays out of scope for sell-through by construction.

## 5. Active Producer

`Producer.status` is unusable (§2, always PENDING). Proposed definition, built entirely from signals that already exist and are already historically queryable:

> **Active Producer (period)** = a producer with at least one of: a `Product` created or updated (`created_at`/`updated_at` in window), a `Bid` placed (`created_at`), an order confirmed-by-producer or delivery-status advanced (`OrderStatusHistory.actor_id` = this producer's identity, in window).

**Reliability: RELIABLE for the presence/count of activity** (every underlying timestamp is real and historically populated) **with one known bias**: "Product updated" conflates genuine restocking with price changes, photo uploads, and the `toggle_product_availability` bug (§3.2) — a producer who only fixed a typo in their product name still counts as "active." This is a directional, not fatal, bias; tightening it (e.g. requiring a quantity-affecting event specifically) needs the same `PRODUCER_SUPPLY_ADJUSTED` instrumentation as §4.

## 6. Producer Activation

`User.onboarding_completed` is **User-level**, shared across a dual-role account, and (per the identity audit) gets set `True` immediately at signup by one creation path (`base.py:221`) regardless of role — it is not a meaningful producer-specific signal.

**Proposed canonical activation moment**: **first sellable product published** — the first `Product` row by this producer with `quantity_for_sale > 0 AND is_available = TRUE` at creation. Reconstructible (`Product.created_at` of that first qualifying row, filtered from `create_product`'s history). `Producer Activation Rate` and `Time to Activation` (signup → first sellable product) both become computable on top of this — **RELIABLE**, no new instrumentation needed.

## 7. Demand Exposure Rate

Audited per journey, not assumed:

- **DIRECT**: **UNAVAILABLE.** `search_products` (`buyer.py:228`) emits `DIRECT_SEARCH_PERFORMED`/`SUCCEEDED` with **zero product or producer dimension** — a producer whose product surfaced in N searches leaves no trace whatsoever.
- **TENDER**: **UNAVAILABLE.** `get_producer_auctions` (`auction.py:1281`) is a pure read query, never logged. A producer "seeing" a relevant open tender is not a fact anywhere in the system. (`TENDER_BID_RECEIVED` measures a *response*, not exposure — using it as an exposure proxy would be survivorship-biased and is explicitly rejected here, not proposed.)
- **RECURRING**: **the only journey with a real signal.** `RECURRING_MATCH_FOUND` already carries `producer_id` (one event per `NeedAllocation` row, `workers/automation/need_matching_service.py::_persist_allocations`). This is genuine "algorithmic exposure" — but the identity/RECURRING audit confirms **the producer is never actually notified of a PROPOSED allocation** (no outbox template references producer + recurring allocation) — the producer only learns of it indirectly once a buyer accepts and an Order appears in their queue. So this measures "the matching engine considered this producer," not "the producer perceived an opportunity." Label accordingly if ever surfaced.

**Overall verdict: PARTIAL** — computable for RECURRING only (`producers with ≥1 allocation proposed / producers with eligible sellable supply in that sub-category`), **UNAVAILABLE for DIRECT and TENDER** without new instrumentation of search-result and tender-notification pathways (a real technical lift, not a quick add — flagged for Phase B+, not decided here).

## 8. Time to First Opportunity

Only meaningful where an exposure signal exists (§7): **RECURRING only, PARTIAL** (first sellable product date → first `RECURRING_MATCH_FOUND` for this producer). **NO for DIRECT/TENDER** (no exposure event exists to measure from).

## 9. Time to First Sale — two distinct notions, kept separate as instructed

- **Time to First Order** = first sellable product date → first `Order` (DIRECT/RECURRING via `OrderItem.product.producer_id`, TENDER via `Bid.producer_id`) referencing this producer, **regardless of outcome**. **RELIABLE**, reconstructible today via plain SQL joins over `Order`/`OrderItem`/`Bid` — this does not depend on `business_events` at all.
- **Time to First Successful Sale** = same start → first order where money actually reached the producer (§12: `payment_status IN ('PAID_OUT', 'PAID')`). **RELIABLE**, same reasoning — these are real, historically-populated `Order.payment_status` transitions, joinable regardless of whether the event stream captures `producer_id` (it currently doesn't, for the DIRECT/TENDER delivery/creation events — see §14 — but that only affects the *event-driven* aggregation path, not whether the underlying data exists).

## 10. Producer Fulfillment Rate

The buyer-side mirror question ("commande confirmée mais 60/100 KG livrés") applies. Two grains proposed, both needed (they answer different questions):

- **Producer Order Fulfillment Rate** = delivered orders (this producer's) / confirmed orders (this producer's). Works for all three journeys, since every journey has an `Order`.
- **Producer Quantity Fulfillment Rate** = `SUM(delivered quantity)` / `SUM(confirmed quantity)`, unit-compatibility enforced via the existing `domain/analytics/units.py` (reused as-is, no new unit logic). Works for DIRECT (`OrderItem.quantity`) and RECURRING (already proven exact via `need_allocations`↔`order_item` 1:1, same mechanism the buyer-side `quantity_delivered` fix (Phase D.5) already established and reuses without change). **Not computable for TENDER** — no `OrderItem`, so only the order-grain rate applies there; `Auction.quantity` could serve as a per-order denominator/numerator (binary: delivered or not, no partial-delivery concept exists for TENDER at all).

Both **RELIABLE** for DIRECT/RECURRING, order-grain only (RELIABLE) for TENDER.

## 11. OTIF — audited, not implemented

`Order` has `delivery_deadline`(Auction)/`expected_fulfillment_date`(RecurringSupply order) and `OrderStatusHistory` timestamps for the DELIVERED transition — the raw timestamps needed for "on time" exist. "In full" reuses §10's quantity fulfillment. **Both halves of OTIF are technically present**; not implemented in this phase per instruction, flagged as a viable Phase B/C candidate once §10's fulfillment metrics exist.

## 12. Delivered GMV per producer

Reuses the buyer-side potential/confirmed/delivered GMV taxonomy exactly (`METRIC_LAYER.md`), re-aggregated by producer instead of buyer/zone. One genuinely new nuance, proven by code (not assumed):

- **Money reaching the producer** = `Order.payment_status IN ('PAID_OUT', 'PAID')` — `PAID_OUT` (escrow, set at `escrow.py:509` inside `verify_delivery_otp`) or `PAID` (cash-on-delivery, set at `producer.py:1720` inside `confirm_delivery_and_payment`, **atomically with `delivery_status = 'DELIVERED'`** in the same call). `ESCROWED` is money **committed by the buyer, not yet with the producer** — never count it as producer revenue.
- **Because both payment paths set the delivered and paid signals atomically in the same transaction, Delivered GMV and Paid GMV are the same thing in this codebase today** (no net-terms/delayed-payout path exists) — worth stating explicitly rather than silently assuming a distinction that isn't there. No separate `escrow_payout_status` column exists on `Order` (that name, from a prior brief, is a misnomer — the real column is `payment_status`; `escrow_payout_status` only exists on the unrelated `OrderDispute` table).

**RELIABLE** for DIRECT/RECURRING; RELIABLE at order-grain for TENDER (no partial payment concept there either).

## 13. Producteur payé

Fully covered by §12 — `payment_status` transitions ARE the "paid" signal, already historically populated, no new instrumentation needed for `Payment Completion Rate`/`Time to Payment` later.

## 14. Repeat Producer Rate

Mirrors the buyer-side `repeat_buyer_rate` exactly: `producers with ≥2 successful sales (§9's "Successful Sale" definition) / producers with ≥1`. **Window: PROVISIONAL**, same posture as the buyer side (no live database access to calibrate empirically in this pass) — **not fixed at 30/60/90 days here**; the mission's own caution about cattle-vs-market-garden cadence (confirmed real by the RECURRING audit's `recurrence_type` vocabulary: `DAILY`/`WEEKLY_DAYS`/`WEEKLY`/`ONE_OFF`/`MONTHLY`, materialized by a daily-replenishment cron) argues for segmenting by category/sub_category before fixing a number — deferred to Phase B, not decided now.

## 15. Producer Retention

Cohort framework proposed (W0/W1/W2/W4/W8), **not implemented**. The mission's own worry is proven correct by the RECURRING cadence data: a `MONTHLY` recurring producer and a `DAILY` one have structurally different natural repeat cycles, so a single global retention curve would average away the signal. **Recommendation for Phase B**: segment retention cohorts by category (at minimum) before computing anything, or present retention only within a single category/sub_category filter — never as one unqualified global number.

## 16. Unsold Supply

Mirrors buyer-side "unmatched demand," but the supply-ledger gap (§3) bites harder here:

- **CURRENT_UNSOLD_STOCK** = `SUM(quantity_for_sale)` where `is_available = TRUE` — **RELIABLE, live gauge only** (same caveat as §4: not a historical series).
- **PERIOD_UNSOLD_NEW_SUPPLY** (newly listed minus sold, over a window) — **UNAVAILABLE**, directly inherits the NEW_SUPPLY_LISTED gap (§4): can't subtract from a numerator that doesn't exist historically.
- **EXPIRED_UNSOLD_SUPPLY** — **UNAVAILABLE**: `Product.harvest_date` exists but nothing marks a product as "expired" distinct from "sold out" or "deactivated"; `quantity_for_sale = 0` is ambiguous between all three, made worse by the `toggle_product_availability` bug (§3.2) which can ALSO produce `quantity_for_sale = 1` for a reason unrelated to real inventory.
- **WITHDRAWN_SUPPLY** — **UNAVAILABLE** for the same ambiguity reason; `delete_product`'s `quantity_for_sale = 0 + is_available = False` (`product.py:355`) is the closest thing to an explicit "withdrawn" signal, but only for products with order history (no order history → hard delete, no trace at all).

**Recommendation**: use `CURRENT_UNSOLD_STOCK` as the only viable pilot-era proxy, explicitly labeled as a snapshot gauge, never presented as a period flow.

## 17–19. Journey mapping — DIRECT / TENDER / RECURRING (producer side)

### 17. DIRECT
`Product` published (`create_product`, is_available=True by default) → appears in `search_products` (no producer-level trace, §7) → buyer selects, cart splits **one Order per producer** (`checkout_group_id`) → `confirm_preorder_draft` (cash) or `mark_escrow_paid` (escrow) debits `quantity_for_sale` and fires `DIRECT_ORDER_CREATED` → producer confirms (`confirm_order_by_producer`) or escrow auto-confirms, firing `DIRECT_ORDER_CONFIRMED` → delivery (`confirm_delivery_and_payment` cash / `verify_delivery_otp` escrow) sets `payment_status`+`delivery_status` atomically and fires `DIRECT_ORDER_DELIVERED`.
**KPI candidates ready today**: Producer Activation (§6), Time to First Order/Sale (§9), Producer Order+Quantity Fulfillment (§10), Delivered/Paid GMV (§12). **Not ready**: Product Visibility, Direct Demand Exposure, Sell-Through (§4/§7).

### 18. TENDER
Buyer publishes `Auction` (`TENDER_CREATED`) → producer pulls `get_producer_auctions` (unlogged, §7) → `place_bid` (`TENDER_BID_RECEIVED`, has `producer_id`) → `select_winning_bid` sets winner + creates Order **with no `OrderItem`** (`TENDER_WINNER_SELECTED`+`TENDER_ORDER_CREATED`, both **already have `producer_id`** via `bid.producer_id`) → delivery via the same shared `emit_order_delivered` helper as DIRECT (`TENDER_DELIVERED` — **producer_id NOT populated**, gap, §14/§20).
**KPI candidates ready today**: Bid Participation/Win Rate (Bid rows are fully historical), Time to Bid (`Auction.created_at`→`Bid.created_at`), order-grain Fulfillment (§10), order-grain Delivered/Paid GMV. **Not ready**: Tender Opportunities Exposed (§7), any quantity-grain fulfillment (no OrderItem).

### 19. RECURRING
Occurrence materialized (`RECURRING_OCCURRENCE_CREATED`) → matching proposes `NeedAllocation` rows (`RECURRING_MATCH_FOUND`, **has `producer_id`**, producer never notified — §7) → buyer accepts digest, one Order per producer created (`RECURRING_DIGEST_ACCEPTED` — occurrence-grain, **no `producer_id`**, needs a join through `order_group_id`/`checkout_group_id` to attribute per producer) → producer declares delivery (`mark_order_delivery_status`, no BusinessEvent emitted) → buyer confirms reception (`record_order_reception`, no BusinessEvent emitted; this is what recomputes `quantity_delivered`, per the buyer-side Phase D.5 fix, reused here without change).
**KPI candidates ready today**: Allocation/Opportunity Rate (RECURRING_MATCH_FOUND already producer-attributed), Allocated Quantity, Recurring Order+Quantity Fulfillment (reuses the proven `quantity_delivered` mechanism). **Not ready**: producer-grain acceptance rate (needs the `order_group_id` join), Consecutive Successful Cycles / Retention (§15, needs cadence segmentation first).

## 20–24. Business events — reuse, gaps, and what NOT to build

### Already producer-attributed, reuse as-is (zero changes)
`TENDER_BID_RECEIVED`, `TENDER_WINNER_SELECTED`, `TENDER_ORDER_CREATED`, `RECURRING_MATCH_FOUND`. **Do not create a `PRODUCER_TENDER_WON`-style duplicate of any of these** — the fact already exists, already carries `producer_id`, per the mission's own rule (§22: "un fait métier ne doit pas être dupliqué juste parce qu'on change de dashboard").

### Existing events missing `producer_id` — an enrichment, not a new event
Proven by direct code inspection of the emitter helpers (`domain/analytics/emitter.py`):
- `emit_direct_order_created` (DIRECT_ORDER_CREATED) — `producer_id` is trivially resolvable (`order.items[0].product.producer_id`, already loaded in memory at the one call site that matters) but not passed. **Zero N+1 cost to add.**
- `emit_direct_order_confirmed` (DIRECT_ORDER_CONFIRMED) — same gap; one call site (`producer.py::confirm_order_by_producer`) already has the producer's own identity in hand and still doesn't pass it.
- `emit_order_delivered` (DIRECT_ORDER_DELIVERED **and** TENDER_DELIVERED, shared helper) — same gap for both; DIRECT resolves via `order.items[0].product.producer_id`, TENDER via `order.winning_bid_id` → `Bid.producer_id` (the exact resolution `producer.py::confirm_delivery_and_payment` already performs two frames away, as `owns_via_bid`, and simply isn't threaded through to the emit call).
- `RECURRING_DIGEST_ACCEPTED` — legitimately occurrence-grain (an occurrence can split across N producers/orders); adding a flat `producer_id` would misrepresent multi-producer occurrences. **Recommendation: leave this event as-is**, and resolve per-producer attribution via the `order_group_id`↔`Order.checkout_group_id` join at query time (the join is exact, per the buyer-side Phase D.5 proof already on file) — a query-layer concern, not an event-schema one.

**Risk flagged explicitly**: a Producer Analytics dashboard built naively off today's `business_events` would silently undercount DIRECT/TENDER order/delivery volume (3 of the 7 relevant events are producer-blind) while the 4 working events could create false confidence that "the whole catalog is producer-safe." Fix before building anything on top: thread `producer_id` through the three helpers above.

### New events that would be genuinely justified (not created in this phase)
- **`PRODUCER_SUPPLY_PUBLISHED`** — canonical source `create_product`; entity `PRODUCT`; actor `PRODUCER`; idempotency `PRODUCER_SUPPLY_PUBLISHED:{product_id}`; reconstructibility **NO** (forward-only). Justified: no current fact captures "a producer made X quantity available" with its quantity — `Product.created_at` alone doesn't carry the quantity as a discrete, queryable fact stream entry (it's still readable from the row itself today, so this event is a convenience/consistency addition, not the only way to know it happened).
- **`PRODUCER_SUPPLY_ADJUSTED`** — canonical source `update_product`, with the delta computed **server-side, before the overwrite** (read the old value, then write); entity `PRODUCT`; idempotency needs a sequence/timestamp component (`PRODUCER_SUPPLY_ADJUSTED:{product_id}:{updated_at_iso}`), since a product can be adjusted many times; reconstructibility **NO**. **Blocked on a product decision**: should this event (and the metric layer built on it) distinguish a genuine restock from a price/photo-only edit or a correction? The current `update_product` function doesn't carry that distinction and `toggle_product_availability`'s bug (§3.2) actively pollutes it. Do not build the event until that's resolved — instrumenting a known-corrupted signal would just encode the bug into analytics permanently.
- **`PRODUCER_OPPORTUNITY_EXPOSED`** — justified *only* for DIRECT/TENDER, where no exposure signal exists at all (§7); **not justified for RECURRING**, where `RECURRING_MATCH_FOUND` already is this fact. Building it for DIRECT/TENDER requires instrumenting the search-result and tender-notification code paths themselves (a real technical project, not a quick event add) — flagged as a Phase B+ recommendation, not decided or scoped here.

### Explicitly NOT to build
- **`PRODUCER_FIRST_SALE`** — the mission's own example of a derived metric, not a fact. Fully computable from existing `Order`/`OrderItem`/`Bid` via `MIN(created_at)` per producer. Do not create an event for it.
- **`PRODUCER_TENDER_WON`** (or any rename of `TENDER_WINNER_SELECTED`) — duplicate of an existing fact.

## 25–27. Metric dictionary proposal (conceptual, not implemented)

Following the exact `MetricDefinition` shape already in `domain/analytics/metric_dictionary.py` (`name, description, business_definition, journey, aggregation_type, numerator, denominator, unit_behavior, supported_dimensions, supported_time_windows, source_entities, reconstructible_historically, reconstructible_note`), extended with the two concepts this mission introduced: **grain** and **reliability** (`RELIABLE`/`PARTIAL`/`UNAVAILABLE` — note: today this vocabulary lives in `metric_layer.py::reliability_of`, not the dictionary itself; a producer metric dictionary should either import that function or the two concepts should be unified — flagged for Phase B, not resolved here) plus **known_bias**.

| # | metric_name | grain | reliability | one-line note |
|---|---|---|---|---|
| 1 | active_producers | producer-day | RELIABLE | §5, biased by noisy "product updated" signal |
| 2 | producer_activation_rate | producer | RELIABLE | §6 |
| 3 | available_supply | producer/product, live | RELIABLE (snapshot only) | §4, no historical series |
| 4 | demand_exposure_rate | producer, RECURRING only | PARTIAL | §7, UNAVAILABLE for DIRECT/TENDER |
| 5 | producer_sell_through_rate | producer | **UNAVAILABLE** | §4, do not implement without new instrumentation + a product decision |
| 6 | time_to_first_opportunity | producer, RECURRING only | PARTIAL | §8 |
| 7 | time_to_first_order / time_to_first_sale | producer | RELIABLE | §9, kept as two separate metrics |
| 8 | producer_order_fulfillment_rate | producer, order | RELIABLE | §10 |
| 9 | producer_quantity_fulfillment_rate | producer, quantity | RELIABLE (DIRECT/RECURRING), N/A (TENDER) | §10 |
| 10 | delivered_gmv / paid_gmv | producer | RELIABLE | §12, same value in this codebase today |
| 11 | delivered_gmv_per_active_producer | producer cohort | RELIABLE | derived from 1+10 |
| 12 | repeat_producer_rate | producer | RELIABLE, window PROVISIONAL | §14 |
| 13 | producer_retention | producer cohort | NOT IMPLEMENTED | §15, needs category segmentation first |
| 14 | unsold_supply | producer/product, live | RELIABLE (snapshot only) | §16 |

## 28. Top 8 KPI recommendation for the pilot

Revised from the mission's initial hypothesis, dropping what the audit shows is not yet reliable:

1. **Active Producers**
2. **Available Supply** (current snapshot)
3. **Time to First Sale**
4. **Producer Order Fulfillment Rate**
5. **Producer Quantity Fulfillment Rate** (DIRECT + RECURRING)
6. **Delivered / Paid GMV**
7. **Delivered GMV per Active Producer**
8. **Repeat Producer Rate** (window explicitly marked provisional)

**Dropped from the mission's initial hypothesis, with reason**: *Demand Exposure Rate* (UNAVAILABLE for 2 of 3 journeys — would misrepresent as a global number) and *Producer Sell-Through Rate* (UNAVAILABLE, §4 — the exact trap this phase exists to prevent) are held back from the top-8 pilot list until their instrumentation gaps close, per §7/§4.

## 29. Market Balance — preparation check only

Demand-side data (buyer needs) already exists in the buyer daily aggregates (`recurring_daily_metrics` has `requested_quantity` per sub_category/zone/canonical_unit — exactly what a future Market Balance needs on the demand side). Supply-side is the blocker: `available_supply` is a live snapshot only (§4), not a period aggregate, so a future `Demand vs Supply` view would need either (a) daily snapshotting of `SUM(quantity_for_sale)` going forward, or (b) the `PRODUCER_SUPPLY_PUBLISHED`/`ADJUSTED` events (§21) landing first. **Not built in this phase**, per instruction — this is only the readiness check requested.

## 30. Data quality risks (producer-specific)

- **`toggle_product_availability` bug** (§3.2): real, active, corrupts `quantity_for_sale` on every pause→resume cycle. The single most important data-quality risk found in this audit — any supply metric will be silently wrong for any producer who has used this toggle.
- **`quantity_for_sale = 0` is 4-ways ambiguous**: sold out, manually zeroed via `update_product`, the toggle bug's "paused" state, or `delete_product`'s soft-delete. No column disambiguates.
- **Negative stock**: `update_product`/`create_product` use `positive_float(..., allow_zero=True)` — structurally guarded against negative values at write time; not separately re-verified here at the DB constraint level (no CHECK found on `quantity_for_sale` itself — flag for a future data-quality check, not fixed here).
- **`Product.category_label` vs `sub_category_id`** — same pre-existing drift already flagged on the buyer side; affects producer-side category dimensions equally.
- **Duplicate `Producer.id == User.id`** creation pattern (`get_or_create_farm`/`create_farm`) vs. the FK-based creation sites — an inconsistency in how the PK is assigned, not verified to cause an actual bug, but worth a Phase B look before building a producer dimension that assumes PK provenance is uniform.
- **Missing producer_id on 3 emitted events** (§20) — a silent-undercount risk for any dashboard built before the enrichment lands.
- **`Producer.organization_id`/cooperative**: always NULL in practice (no writer) — a "cooperative" dimension would be 100% NULL if built today; don't build it as a filterable dimension until something populates it.

## 31–32. Bugs/gaps found (listed for Phase B, not fixed here)

1. `toggle_product_availability` destroys the real quantity on resume (§3.2, §30) — highest-priority fix candidate, arguably a bug worth fixing regardless of analytics.
2. `emit_direct_order_created`/`emit_direct_order_confirmed`/`emit_order_delivered` never populate `producer_id` despite it being cheaply resolvable (§20).
3. No historical ledger for Product-level supply changes (§3.2) — a structural gap, not a bug, but the reason Sell-Through is UNAVAILABLE.
4. `Producer.status` is dead (always PENDING, no transitions) — misleading if anyone assumes it reflects real approval state.
5. `Producer.organization_id`/`UserOrganization` cooperative linkage is entirely unwired.
6. `Producer.is_certified` has zero writers, and shares a name with an unrelated conversational-agent flag — a real collision risk for anyone querying by column name alone.
7. TENDER has zero inventory linkage (`OrderItem`) — a structural fact to design around, not a bug, but easy to forget when building quantity-based TENDER metrics.

## 33. Files created / commits

- `docs/analytics/PRODUCER_ANALYTICS_ARCHITECTURE.md` (this file). No other files touched — audit only, per instruction.

## 34. Verdict

**NOT READY FOR PRODUCER ANALYTICS PHASE B on the full original KPI list — READY for a scoped Phase B** on the 8 KPIs in §28, with these exact blockers called out for the rest, not glossed over:

- **Blocked, needs a product decision before any code**: `producer_sell_through_rate` (needs an `update_product` semantics decision: restock vs. correction vs. fixing the toggle bug first) and `demand_exposure_rate` for DIRECT/TENDER (needs a decision on whether to build search-impression/tender-notification instrumentation at all — a real project, not a quick add).
- **Ready to implement immediately, zero ambiguity**: the 8 KPIs in §28, once `producer_id` is threaded through the 3 emitter gaps in §20 (a small, mechanical change) — everything else needed for them is either already event-instrumented (TENDER_BID_RECEIVED/WINNER_SELECTED/ORDER_CREATED, RECURRING_MATCH_FOUND) or directly queryable from already-historical transactional tables (Order/OrderItem/Bid/OrderStatusHistory), independent of the event stream.
- **Recommended Phase B scope**: (1) enrich the 3 emitter helpers with `producer_id`; (2) build a `producer_daily_metrics`-style aggregate (no producer dimension exists on any current daily table) mirroring the buyer-side Metric Layer's numerator/denominator-never-rates discipline; (3) implement the 8 KPIs from §28 against it; (4) explicitly defer sell-through, exposure (DIRECT/TENDER), and retention-with-segmentation to a Phase C, pending the product decisions flagged above.

---

# Phase B — Instrumentation (implementation record)

**Scope actually delivered**: the bug fix (§36), the two new SUPPLY events + their writer instrumentation (§37–39), `producer_id` enrichment on the three DIRECT/TENDER emitter gaps identified in §20 (§40), and the KPI-readiness update this unlocks (§43). **Deliberately NOT delivered** (per the mission's own interdiction list, same as §34's Phase C recommendation): `producer_daily_metrics`, any Producer AnalyticsService/Admin API/Dashboard, Market Balance, and no invented Sell-Through/Demand-Exposure formula. Those remain exactly as scoped in §34, now formally the Phase C backlog (§48).

## 36. Bug fix: `toggle_product_availability`

**Cause** (proven in §3.2/§30/§31): the method faked ON/OFF by writing `product.quantity_for_sale = 0.0` (pause) / `1.0` (resume) — a resume after a pause **permanently destroyed** the real quantity (500 KG → paused → resumed → 1 KG, forever). It never touched `Product.is_available`, the boolean that already existed for exactly this purpose (already the column `BuyerMixin.search_products` filters on: `Product.is_available.is_(True)`).

**Fix** (`services/database/product.py::toggle_product_availability`, line 260): flips `product.is_available = not was_available` only. `quantity_for_sale` is never assigned anywhere in the method body (locked in by source-inspection tests, see §46). On the OFF→ON transition, calls `emit_product_published_for_sale` (§38) — the only place besides `create_product` where a product can newly become sellable.

**Migration: NONE.** `is_available` (`marketplace.products`) already existed and was already correctly used elsewhere (buyer search) — the bug was that this one method didn't use it. No schema change was needed for the bug fix itself; the only migration in this phase is the additive SUPPLY-journey/event-name CHECK-constraint change (§45), unrelated to the bug.

**Invariant restored and tested** (§46): pause → quantity unchanged; resume → quantity unchanged; a product at 0 KG still just toggles visibility; buyer search still excludes paused products (pre-existing filter, unaffected); no checkout/matching regression (neither reads this method's write side).

## 37. Source of truth: product availability vs. quantity

Two structurally separate booleans/counters, now correctly separated in every write path:

- **Commercial availability** = `Product.is_available` (bool). Mutated by: `toggle_product_availability` (explicit producer pause/resume), `delete_product` (soft-delete forces it False), `update_product_price_and_qty` (never directly — only the publication-transition side effect reads it, never writes it).
- **Physical/sellable quantity** = `Product.quantity_for_sale` (float). Mutated only by the writers exhaustively listed in §39 — never as a side effect of an availability toggle anymore.
- **SELLABLE** (used by both new events' trigger conditions) = `is_available = TRUE AND quantity_for_sale > 0` — the exact same predicate §4/§6 already defined in Phase A, now the operational trigger, not just a proposed definition.

## 38. New event: `PRODUCT_PUBLISHED_FOR_SALE`

Fires **only** on the genuine non-sellable → sellable transition (§37's SELLABLE predicate flipping False→True), never on every update — per the mission's explicit "pas à chaque update." Three call sites, all guarded by an explicit before/after comparison (never an unconditional emit):

| Call site | Guard |
|---|---|
| `producer.py::create_product` (line 564, after flush) | `product.is_available and quantity_for_sale > 0` at creation (no "before" state needed — creation is always a transition from nonexistence) |
| `product.py::update_product_price_and_qty` (line 64) | `was_sellable` captured before the mutation loop; emits only `if is_sellable and not was_sellable` after |
| `product.py::toggle_product_availability` (line 260) | emits only on the `not was_available and product.is_available` (OFF→ON) branch |

**Payload**: `producer_id`, `entity_type=PRODUCT`, `entity_id=product.id`, `sub_category_id`, `quantity` (the sellable quantity at the moment of publication), `unit`. **Journey**: `SUPPLY`.

**Idempotency**: keyed on `product_id` **alone** (`emitter.py::emit_product_published_for_sale`, line 285) — deliberate, not an oversight. This captures only the **first** such transition per product; a later re-publish after a pause is silently deduped. That is exactly what this event's one current consumer (Producer Activation, §6 — "first sellable product published") needs and nothing more. **If a future metric needs every republish as a distinct fact, this key must change first.**

## 39. New event: `PRODUCT_SELLABLE_QUANTITY_CHANGED`

A **raw fact about the column**, nothing more: "the declared sellable quantity changed from X to Y." Never an interpretation of intent — an increase is not asserted to be a restock, a decrease is not asserted to be a sale (sale debits already have their own DIRECT/TENDER/RECURRING order events; this event exists purely because `Product.quantity_for_sale` had **zero history** before Phase B — confirmed in §3.2/§3.3).

**Payload** (`emitter.py::emit_product_quantity_changed`, line 314): `producer_id`, `entity_type=PRODUCT`, `entity_id=product.id`, `sub_category_id`, `quantity` (the new value), `unit`, and inside `metadata` (the existing generic JSONB extension column, same pattern as every other event in this codebase — no new columns added): `previous_quantity`, `delta`, `source` (a short caller-supplied tag: `producer_adjustment`, `order_debit_direct`, `order_debit_escrow`, `order_debit_recurring`, `order_cancelled_recredit_buyer`, `order_cancelled_recredit_producer`, `soft_delete`). `source` is descriptive metadata only — it never gates whether the event fires. **No-op** (no DB call, returns `False`) when `previous_quantity == new_quantity`.

**Idempotency**: `Product` has no version/revision column, so there is no fully stable per-transition ID — documented explicitly, not glossed over (mission requirement). Keyed on `(product_id, previous_quantity, new_quantity, today's UTC date)`. This collapses a same-day retry of the **identical** transition (the real retry scenario: a replayed task/message reruns the exact same write against the exact same before-state) into one event, while a genuinely repeated transition on a **different** day still produces its own event. **Known, accepted limitation**: it would under-count only if the exact same (previous, new) pair legitimately recurs *twice on the same calendar day* for reasons other than a retry — judged preferable to a random-UUID key, which would miss real retries entirely.

## 40. Exhaustive `Product.quantity_for_sale` writer instrumentation

Every legitimate writer from the §3.2 Phase A audit table, now instrumented (function → reason → transactionality → event):

| Function | Reason | Same transaction as mutation? | Event |
|---|---|---|---|
| `producer.py::create_product` | initial sellable quantity at creation | yes (before `session.flush()` returns) | `PRODUCT_PUBLISHED_FOR_SALE` only (no quantity-changed event — there is no "previous" value to compare against at creation) |
| `product.py::update_product_price_and_qty` | producer-driven adjustment | yes | `PRODUCT_SELLABLE_QUANTITY_CHANGED` (source=`producer_adjustment`) + `PRODUCT_PUBLISHED_FOR_SALE` if this update is also the sellability transition |
| `product.py::toggle_product_availability` | pause/resume (bug fix, §36) | yes | `PRODUCT_PUBLISHED_FOR_SALE` only on resume — **never** a quantity-changed event, because (post-fix) this method never touches quantity |
| `product.py::delete_product` | soft-delete zeroing | yes | `PRODUCT_SELLABLE_QUANTITY_CHANGED` (source=`soft_delete`) |
| `buyer.py::confirm_preorder_draft` (line ~2360) | DIRECT cash-path sale debit | yes | `PRODUCT_SELLABLE_QUANTITY_CHANGED` (source=`order_debit_direct`) |
| `escrow.py::mark_escrow_paid` (line ~297) | DIRECT escrow-path sale debit | yes | `PRODUCT_SELLABLE_QUANTITY_CHANGED` (source=`order_debit_escrow`) |
| `recurring_supply.py::accept_match_proposal` (line ~834) | RECURRING allocation-acceptance debit | yes | `PRODUCT_SELLABLE_QUANTITY_CHANGED` (source=`order_debit_recurring`) |
| `buyer.py` cancellation recredit (line ~1049) | buyer-initiated order cancellation | yes | `PRODUCT_SELLABLE_QUANTITY_CHANGED` (source=`order_cancelled_recredit_buyer`) |
| `producer.py` cancellation recredit (line ~1953) | producer-initiated order cancellation | yes | `PRODUCT_SELLABLE_QUANTITY_CHANGED` (source=`order_cancelled_recredit_producer`) |

**Deliberately excluded**: `marketplace.py::record_sale`'s phantom-product zeroing (§3.2's last row) — a walk-in-sale bookkeeping artifact, not real catalog supply, out of scope by the same reasoning Phase A already gave it. `buyer.py::finalize_multi_order`'s own `quantity_for_sale` write is **dead code** — zero live callers (verified by grep: only referenced in comments/docs) — deliberately not instrumented; instrumenting dead code would be pure noise.

No central "quantity mutation primitive" was introduced — each call site already had its own before/after value in scope from its own business logic, so each just calls `BusinessEventEmitter(session).emit_product_quantity_changed(product, previous_quantity=..., source=...)` directly, in the same session/transaction as the mutation, before `flush()`/`commit()`. `BusinessEventEmitter` itself remains the single centralization point (mission's actual ask), not a second layer on top of it.

## 41. `producer_id` enrichment — DIRECT / TENDER / RECURRING

**Canonical resolution rule** (never the current conversational actor, per mission instruction): `emitter.py::_resolve_direct_producer_id` (line 159) reads the persisted `Order.items[].product.producer_id` relationship — already-loaded items cost zero extra queries (the common case at every real call site); when not loaded, exactly **one** bounded query (`SELECT Product.producer_id JOIN OrderItem ... LIMIT 1`, never a loop). `_resolve_tender_producer_id` (line 183) reads `Order.winning_bid_id → Bid.producer_id`, one bounded query (an Order has at most one winning Bid).

**Wired into** (closing exactly the 3 gaps §20 identified, no new events created):
- `emit_direct_order_created` (line 254) — DIRECT_ORDER_CREATED now carries `producer_id`.
- `emit_direct_order_confirmed` (line 369) — DIRECT_ORDER_CONFIRMED now carries `producer_id`.
- `emit_order_delivered` (line 195, shared DIRECT+TENDER helper) — branches on `is_tender` (`order.auction_id is not None`) to call the matching resolver; both DIRECT_ORDER_DELIVERED and TENDER_DELIVERED now carry `producer_id`.

**RECURRING**: unchanged, per mission instruction — `RECURRING_MATCH_FOUND` already carried `producer_id` per-allocation (Phase A §20 finding, confirmed still true, no gap to close). `RECURRING_DIGEST_ACCEPTED` deliberately **not** turned into a producer-grain event (§20's own recommendation, re-confirmed): it is legitimately occurrence/buyer-grain (an occurrence can split across N producers), and forcing a flat `producer_id` onto it would misrepresent multi-producer occurrences. Per-producer attribution for RECURRING stays a query-time join (`order_group_id` ↔ `Order.checkout_group_id`), exactly as §20 already recommended — no code change needed there.

**TENDER's other events** (`TENDER_BID_RECEIVED`, `TENDER_WINNER_SELECTED`, `TENDER_ORDER_CREATED`) were re-verified against the branch diff: all three already had `producer_id` before this phase (§20's Phase A finding), confirmed unchanged — no new gap, no duplicate event created.

**Tested** (§46): resolution from already-loaded relationships costs zero DB calls (`session.scalar.assert_not_awaited()`); the bounded-query fallback path is exercised separately; both DIRECT and TENDER branches of `emit_order_delivered` are tested to route to the correct resolver and event name.

## 42. Demand Exposure / Sell-Through — unchanged status, re-confirmed

Per explicit mission instruction, **not touched** in Phase B:
- **Sell-Through Rate**: still **UNAVAILABLE** (§4/§80's verdict). The new `PRODUCT_SELLABLE_QUANTITY_CHANGED` event only starts accumulating history **from Phase B forward** — restock-vs-correction semantics remain undecided (§39), and no formula is proposed here, exactly as §4 already concluded.
- **Demand Exposure Rate**: still **PARTIAL** (RECURRING only, via the pre-existing `RECURRING_MATCH_FOUND`) / **UNAVAILABLE** (DIRECT, TENDER) — §7's verdict stands unchanged; no search-impression or tender-notification instrumentation was added, per the mission's explicit "ne pas bloquer Phase B dessus."

## 43. KPI readiness — update from §28's list

| # | KPI | §28 status (Phase A) | Phase B status | What changed |
|---|---|---|---|---|
| 1 | active_producers | RELIABLE | RELIABLE | unchanged (§5's definition didn't need `producer_id` enrichment — it reads `Product`/`Bid`/`OrderStatusHistory` directly) |
| 2 | available_supply | RELIABLE (snapshot only) | RELIABLE (snapshot only) | unchanged, live-gauge only by design (§4/§29) |
| 3 | time_to_first_sale | RELIABLE | RELIABLE | unchanged — §9 already didn't depend on the event stream's `producer_id` (plain `Order`/`OrderItem`/`Bid` joins) |
| 4 | producer_order_fulfillment_rate | RELIABLE | RELIABLE | unchanged, same reasoning as #3 |
| 5 | producer_quantity_fulfillment_rate | RELIABLE (DIRECT/RECURRING), N/A (TENDER) | unchanged | TENDER still has no `OrderItem` — structural, not closed by this phase |
| 6 | delivered_gmv / paid_gmv | RELIABLE | RELIABLE | unchanged, still the same value in this codebase (§12) |
| 7 | delivered_gmv_per_active_producer | RELIABLE | RELIABLE | derived from #1+#6, unchanged |
| 8 | repeat_producer_rate | RELIABLE, window PROVISIONAL | RELIABLE, window still PROVISIONAL | unchanged — §14's window-calibration question is a Phase C data question, not a Phase B instrumentation gap |
| — | producer_activation_rate / time_to_activation | RELIABLE | RELIABLE, now **event-observable** | §6's definition ("first sellable product published") was already reconstructible from `Product.created_at`; `PRODUCT_PUBLISHED_FOR_SALE` (§38) now also makes it **event-stream-observable**, not just table-observable — a convenience, not a new capability |

**Net effect of Phase B on the §28 top-8**: none of them were blocked on the producer_id/quantity-history gaps in the first place — §28 was already careful to pick only KPIs reconstructible from plain transactional tables. What Phase B actually unlocks is (a) an event-driven path to the same 8 KPIs, as an alternative to table-joins, and (b) the raw material — `PRODUCT_SELLABLE_QUANTITY_CHANGED` history — a future Sell-Through design would need, without yet building that design.

## 44. Data quality checks added

`services/analytics/data_quality.py::run_supply_data_quality_checks` (new function, reuses the existing `QualityIssue` dataclass — no new abstraction): `sellable_product_negative_quantity` (ERROR — `is_available=TRUE AND quantity_for_sale < 0`), `available_product_not_actually_sellable` (WARNING — `is_available=TRUE` but zero/NULL quantity, the exact post-bug-fix sanity check for §36), `supply_event_missing_producer_id` (ERROR — any `journey=SUPPLY` event without `producer_id`), `direct_order_event_missing_producer_id` (WARNING — a DIRECT order event still missing `producer_id`, expected only for pre-Phase-B rows). **Deliberately narrow** — only the checks the mission explicitly asked for (§16), not a general-purpose linter; no unit-validity check was added (would require reusing the Python-side `measurement_family_of` registry inside a raw SQL count, judged premature/gassy for this phase).

## 45. Schema / migration

**Source of truth**: Drizzle (`frontag` repo), per repo convention. `src/db/schema/analytics.ts` extended: `EVENT_NAMES` gained `PRODUCT_PUBLISHED_FOR_SALE`/`PRODUCT_SELLABLE_QUANTITY_CHANGED`; a shared `JOURNEY_VALUES_SQL` constant (`'DIRECT','TENDER','RECURRING','SUPPLY'`) now backs both `event_outbox_journey_chk` and `business_events_journey_chk`. Generated migration `drizzle/0009_supply_journey_and_events.sql` — pure **EXPAND** (DROP+ADD CONSTRAINT pairs, strict superset of the previously-allowed values, zero data loss) — committed and pushed to `origin/analytics/producer-phase-b-schema` (`c757ba6`).

**Backend mirror**: `domain/analytics/models.py`'s `_JOURNEY_VALUES_SQL`/`_EVENT_NAMES_SQL` and both tables' `CheckConstraint`s updated to match; `schema_contract/migrations/0009_supply_journey_and_events.sql` + `drizzle_snapshot.json` + `_journal.json` synced via the repo's own `sync_contract.py` convention. `domain/analytics/business_events.py`'s `BusinessEventName` enum, `EVENT_JOURNEY` dict, and `EVENTS_REQUIRING_QUANTITY` frozenset all extended with the two new names; `metric_dictionary.py`'s `Journey` enum gained `SUPPLY`.

**event_outbox has no `event_name` CHECK constraint** (only `journey`) — confirmed by reading `EventOutboxRecord.__table_args__`; only `business_events` restricts event names. Both tables' `journey` constraints were updated identically.

## 46. Tests

- **`tests/unit/test_product_toggle_availability_bug_fix.py`** (new, 9 tests, all green): source-inspection lock that `quantity_for_sale` is never assigned in `toggle_product_availability`'s body; behavioral tests for pause/resume/zero-quantity preserving quantity; publish-event-fires-only-on-OFF→ON; buyer search still filters on `is_available`.
- **`tests/unit/test_analytics_emitter.py`** (extended, 21 new tests across `TestEmitProductPublishedForSale`, `TestEmitProductQuantityChanged`, `TestDirectProducerIdResolution`, `TestTenderProducerIdResolution`): idempotency-key stability/replay behavior for both new events; zero-DB-call assertion when Order items are already loaded; bounded-query fallback when not; DIRECT vs. TENDER routing correctness inside `emit_order_delivered`.
- **`tests/schema/test_analytics_business_events.py`** (extended, +4 tests) and **`tests/schema/test_analytics_event_outbox.py`** (extended, +1 test): real-PostgreSQL proof that the migrated CHECK constraints actually accept `SUPPLY`/the two new event names, that pre-existing journeys/events are unaffected (additive-migration regression lock), and (event_outbox specifically) that both new event names insert cleanly under the `SUPPLY` journey.
- **`tests/schema/test_analytics_supply_data_quality_pg.py`** (new, 8 tests): real-Postgres exercise of `run_supply_data_quality_checks` — clean catalog raises nothing, negative-quantity-while-available is ERROR, zero-quantity-while-available is WARNING, a paused product at 0 raises nothing (proving the check targets the bug's *symptom*, not the state itself), SUPPLY events with/without `producer_id`, DIRECT order events with/without `producer_id`.
- **Local run results**: `test_analytics_emitter.py` + `test_product_toggle_availability_bug_fix.py` → **30/30 passed**. All four `tests/schema/*` files (`test_analytics_business_events.py`, `test_analytics_event_outbox.py`, `test_analytics_supply_data_quality_pg.py`) collect cleanly (15/6/8 items respectively) but **self-skip locally** — no `SCHEMA_TEST_DSN` / local PostgreSQL available in this environment (consistent with this repo's established, documented CI-only gap for schema tests — same pattern as prior analytics phases). They run for real under `REQUIRE_SCHEMA_DB=1` in CI.
- **Buyer Analytics regression**: no Buyer Analytics test file was modified; all touched emitter methods (`emit_direct_order_created/confirmed`, `emit_order_delivered`) only gained an *additional* `producer_id` kwarg passed into the pre-existing `emit()` call — the buyer-facing payload shape (`buyer_id`, `event_name`, `journey`, `amount`, etc.) is unchanged. Full-suite run confirms no new failures (§47).

## 47. Gates

Ruff: `ruff check` on every modified/new file — clean. Full backend test suite and mypy diff-vs-main comparison run and reported in the final delivery message to the user, together with the exact commit list and PR link(s) — not duplicated here to avoid this document going stale the moment CI runs again; treat this doc as the design/implementation record, the chat delivery as the point-in-time gate proof.

## 48. Stabilized Producer Metric Dictionary (definitions only — no code registered)

Mirrors the exact field shape of `domain/analytics/metric_dictionary.py::MetricDefinition` (`name, description/definition, journey, numerator, denominator, unit_behavior, supported_dimensions, reconstructible_historically, reliability, notes`) — stabilizing §25–27's conceptual sketch into concrete per-field definitions for the §28 top-8. **Deliberately NOT `_register()`-ed into the live `METRICS` dict**: doing so would be the first brick of a Producer AnalyticsService (querying it requires either a `producer_daily_metrics` aggregate that doesn't exist yet, or direct transactional joins that would need their own service layer) — both explicitly out of scope for this phase (§22/§48). This section is the Phase C implementation spec, not runnable code.

| # | name | definition | numerator | denominator | unit | journeys | dimensions | reconstructibility | reliability | notes |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `active_producers` | Distinct producers with ≥1 qualifying action in the window (§5) | `COUNT(DISTINCT producer_id)` over Product created/updated, Bid placed, or order status advanced by this producer | — | count | DIRECT+TENDER+RECURRING (cross-journey) | date, zone, category | YES | RELIABLE | biased by noisy "product updated" signal (§5); tightens once a quantity-affecting-only variant is defined |
| 2 | `available_supply` | Live sellable supply per producer/product, evaluated now | `SUM(quantity_for_sale)` where `is_available=TRUE` | — | KG/L/TETE/... (per canonical unit, never summed across incompatible units) | SUPPLY | producer, sub_category, zone | NO (snapshot only) | RELIABLE (snapshot only) | never a historical series without forward snapshotting; §39's quantity-changed event is the raw material for a future series, not a series itself |
| 3 | `time_to_first_sale` | signup/first-sellable-product -> first successful (delivered) sale | `MIN(delivered_order.occurred_at) - product.first_sellable_at` | — (point value per producer, aggregated as AVG/P50) | days | DIRECT+TENDER+RECURRING | producer, category | YES | RELIABLE | two variants exist (Time to First Order vs. Time to First Successful/Delivered Sale, §9) — pilot KPI uses the delivered/successful notion |
| 4 | `producer_order_fulfillment_rate` | share of confirmed orders that reached delivery | `COUNT(orders delivered)` | `COUNT(orders confirmed)` | ratio | DIRECT+TENDER+RECURRING | producer, journey, zone, category | YES | RELIABLE | works for all 3 journeys (every journey has an Order) |
| 5 | `producer_quantity_fulfillment_rate` | share of confirmed quantity actually delivered | `SUM(quantity delivered)` | `SUM(quantity confirmed)` | canonical unit (mass/volume family) | DIRECT+RECURRING only | producer, sub_category | PARTIAL | RELIABLE (DIRECT/RECURRING), **UNAVAILABLE (TENDER — no OrderItem)** | do not blend TENDER into a global rate — would silently misrepresent it as quantity-based when it's order-binary only |
| 6 | `producer_delivered_gmv` / `producer_paid_gmv` | revenue that reached the producer | `SUM(amount)` where `payment_status IN ('PAID_OUT','PAID')` | — | XOF | DIRECT+TENDER+RECURRING | producer, journey, zone, category | YES | RELIABLE | **same value in this codebase today** (§12 — both payment paths set delivered+paid atomically); keep both names reserved in case a delayed-payout path is introduced later, but do not compute two different numbers now |
| 7 | `delivered_gmv_per_active_producer` | average revenue per active producer | `SUM(producer_delivered_gmv)` | `COUNT(active_producers)` (metric 1) | XOF | GLOBAL (cross-journey) | zone, category | YES | RELIABLE | pure derivation of 1+6, no new source needed |
| 8 | `repeat_producer_rate` | share of producers with ≥2 successful sales among those with ≥1 | `COUNT(producers with >=2 successful sales)` | `COUNT(producers with >=1 successful sale)` | ratio | DIRECT+TENDER+RECURRING | producer category (for window segmentation) | YES | RELIABLE, window **PROVISIONAL** | no dedicated event — computed from the same delivered-order facts as metric 3/4; window not fixed at 30/60/90d — segment by category before fixing a number (§14) |

**Deliberately excluded from this list** (per §34/§42, re-confirmed, not re-litigated here): `producer_sell_through_rate` (UNAVAILABLE, blocked on an `update_product` semantics decision), `demand_exposure_rate` for DIRECT/TENDER (UNAVAILABLE, blocked on a decision to build impression/notification instrumentation), `producer_retention` (NOT IMPLEMENTED, needs category segmentation first), `PRODUCER_FIRST_SALE` (derivable, not a metric needing its own event — §34).

## 49. Phase C backlog (unchanged from §34, reconfirmed)

Everything §34 already deferred stays deferred: `producer_daily_metrics` aggregate table, Producer AnalyticsService/Admin API/Dashboard, Market Balance, Sell-Through formula (blocked on an `update_product` restock-vs-correction product decision), Demand Exposure for DIRECT/TENDER (blocked on a decision to build search-impression/tender-notification instrumentation), Producer Retention with category segmentation, `repeat_producer_rate`'s window calibration. Phase B's `PRODUCT_SELLABLE_QUANTITY_CHANGED` history (§39) is the raw material a future Sell-Through design would consume — it does not itself close that gap; history only starts accumulating from this phase forward.

---

# Phase C — Metric Layer (implementation record)

Full detail lives in `docs/analytics/PRODUCER_METRIC_LAYER.md` (tables, refresh mechanics, per-metric mechanism, data quality, known limits) — this section is a pointer + summary, not a duplicate.

## 50. What shipped

Three new tables (migration `0010`, pure EXPAND — `CREATE TABLE`/`CREATE INDEX` only): `producer_daily_metrics` (grain `metric_date, producer_id` — order/GMV counts per journey, plus the activity-signal counts that define "active producer"), `producer_quantity_daily_metrics` (grain `metric_date, producer_id, canonical_unit` — DIRECT+RECURRING quantity fulfillment, TENDER structurally absent), `producer_supply_daily_snapshot` (grain `metric_date, producer_id, zone_id, category_id, sub_category_id, canonical_unit` — a live end-of-day snapshot, never a flow, never reconstructible for a past day).

`ProducerMetricsRefresher.recompute_day` mirrors the buyer layer's `DailyMetricsRefresher` exactly (advisory lock, DELETE+INSERT per day, cohort-by-creation-day). `snapshot_producer_supply` deliberately does NOT share that "any past day" contract — it refuses any day but today, since no historical ledger of product availability exists to reconstruct from.

`ProducerAnalyticsService` (`services/analytics/producer_analytics_service.py`) makes all 8 §48 KPIs queryable via `get_metric`/`get_metric_timeseries`/`get_metric_breakdown`/`compare_periods`, reusing the generic `Binding`/`MetricResult`/`DataStatus`/target-resolution engine from `domain/analytics/metric_layer.py` directly (no duplication, no premature shared base class either). `domain/analytics/metric_dictionary.py::METRICS` now carries live, fully-specified entries for all 8 KPIs plus `producer_sell_through_rate`/`producer_paid_gmv` (both UNAVAILABLE, with their reason, same convention as the buyer side's `active_recurring_needs`/`recurring_modification_rate`).

## 51. Verdict update on §4/§7/§34's open questions

- **`producer_sell_through_rate`**: re-confirmed UNAVAILABLE, unchanged from §4/§42 — the quantity-change history Phase B added is real but does not by itself resolve the restock-vs-correction ambiguity. Not revisited further in Phase C, per mission instruction.
- **`demand_exposure_rate`**: re-confirmed PARTIAL/UNAVAILABLE per journey, unchanged from §7 — no exposure instrumentation was added in Phase C either. Registered as one UNAVAILABLE entry (the mixed status is not one number) rather than three separate always-would-need-building metrics.
- **`available_supply`**: Phase C chose **Option B** from the mission's own A/B choice (§2 of the Phase C mission) — daily snapshots (`producer_supply_daily_snapshot`) rather than live-query-only — because it was simple to add and gives a consistent, indexed read path; it remains a pure snapshot, never additive, per the mission's explicit warning.
- **8 KPIs' reconstructibility, finalized** (was tentative in §48, now verified against the real implementation): `active_producers`, `producer_order_fulfillment_rate`, `producer_delivered_gmv`, `delivered_gmv_per_active_producer`, `repeat_producer_rate` are `Reconstructibility.YES` (built from transactional tables, recomputable for any past day via `recompute_range`). `available_supply` is `Reconstructibility.NO` (snapshot, forward-only). `time_to_first_sale` and `producer_quantity_fulfillment_rate` are `Reconstructibility.PARTIAL` (the former only from Phase B's event forward; the latter reliable for DIRECT+RECURRING, structurally absent for TENDER).

## 52. Tests and gates

Real-Postgres tests in `tests/schema/test_producer_metric_layer_pg.py` (self-skips locally without `SCHEMA_TEST_DSN`, runs for real in CI): recompute idempotency, late-delivery cohort update, distinct active-producer counting across days, multi-producer isolation, unit conversion + KG/TETE never summed, TENDER's structural absence from quantity fulfillment, GMV attribution across all 3 journeys, repeat-producer counting, zero-denominator-yields-null, data quality, grain-uniqueness rejection, and the supply snapshot's past-day refusal + idempotent re-run. Pure aggregation logic (`producer_daily_aggregation.py`) has its own database-free unit tests (`tests/unit/test_producer_daily_aggregation.py`), mirroring `test_analytics_daily_aggregation.py`'s style. Buyer Analytics regression re-verified (metric layer, daily aggregation, business events, metric dictionary, metric targets, schema contract — all green); full unit suite's failure set stays byte-identical to the pre-Phase-C baseline (34/34, all pre-existing/environmental — verified via the same git-stash-comparison methodology as Phase B). Ruff and mypy clean on every new/modified file.

**Known, disclosed gap**: `time_to_first_sale` has no dedicated real-Postgres test in this pass (the aggregation and service-layer logic exist and are documented, including the RECURRING `Order.updated_at` imprecision, but end-to-end proof against real data was not written here) — flagged for a follow-up, not silently skipped.

## 53. Phase D recommendation

Once this is reviewed: build the Admin API + a Producer Dashboard on top of `ProducerAnalyticsService` (mirroring the buyer side's Phase E), add the `time_to_first_sale` Postgres test noted above, and revisit `demand_exposure_rate`/`producer_sell_through_rate` only if/when the underlying instrumentation gaps (search-impression tracking, a restock-vs-correction product decision) are actually closed — not before. Market Balance (Demand vs Supply) becomes buildable once both sides share compatible dimensions — worth checking then whether `recurring_daily_metrics`' `requested_quantity` and `producer_supply_daily_snapshot`'s `available_quantity` are query-joinable as-is (same `sub_category_id`/`zone_id`/`canonical_unit` grain shape) or need reconciliation first.
