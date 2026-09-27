# Producer Analytics Architecture — Phase A (Audit + Design)

**Status: AUDIT ONLY. No migration, no event, no table, no dashboard, no code change in this phase.** Everything below is either a fact proven by reading the code (cited file:line) or a design decision explicitly flagged as a decision, never invented to fill a gap. Companion to `BUYER_ANALYTICS_ARCHITECTURE.md`, `BUSINESS_EVENT_CATALOG.md`, `METRIC_LAYER.md` — the buyer-side infrastructure this reuses without change.

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
