# Business Event Catalog (Phase C)

Source of truth for what `analytics.business_events` contains. An event is a **past fact**, written as an
intent into `analytics.event_outbox` **in the same transaction as the business fact**, then landed by the
async dispatcher. No event is emitted from an intent, a draft, an LLM classification or a user utterance.

Status values: **INSTRUMENTED** (emitted from the canonical transition) · **NOT_INSTRUMENTED** (in the contract,
no emitter — never inferred from a proxy).

| Event | Journey | Meaning | Canonical source | Entity | Idempotency key | Historical reconstruction | Status |
|---|---|---|---|---|---|---|---|
| DIRECT_SEARCH_PERFORMED | DIRECT | A catalog search was actually executed | `BuyerMixin.search_products`, after the two queries ran | SEARCH (fresh uuid) | `DIRECT_SEARCH_PERFORMED:{search_uuid}` | NO (no search log ever existed) | INSTRUMENTED |
| DIRECT_SEARCH_SUCCEEDED | DIRECT | The executed search returned ≥ 1 eligible row (`catalog_rows + future_rows > 0`; eligibility is the query's own filters: available, stock > 0…) | same call | SEARCH | `DIRECT_SEARCH_SUCCEEDED:{search_uuid}` | NO | INSTRUMENTED |
| DIRECT_ORDER_CREATED | DIRECT | Order row persisted from a cart/preorder | `BuyerMixin.create_preorder`, after `total_amount` flush | ORDER | `DIRECT_ORDER_CREATED:{order_id}` | PARTIAL (Order.created_at) | INSTRUMENTED |
| DIRECT_ORDER_CONFIRMED | DIRECT | Order confirmed by the producer | — | — | — | PARTIAL | **NOT_INSTRUMENTED** |
| DIRECT_ORDER_DELIVERED | DIRECT | Non-tender, non-recurring order reached terminal delivery | `verify_delivery_otp` (escrow), `confirm_delivery_and_payment` (cash) via `emit_order_delivered` | ORDER | `DIRECT_ORDER_DELIVERED:{order_id}` | PARTIAL (no delivered_at column) | INSTRUMENTED |
| DIRECT_ORDER_FAILED | DIRECT | — | — | — | — | NO | NOT_INSTRUMENTED |
| TENDER_CREATED | TENDER | Auction row persisted | `AuctionMixin.create_auction` | AUCTION | `TENDER_CREATED:{auction_id}` | YES (Auction.created_at) | INSTRUMENTED |
| TENDER_PUBLISHED | TENDER | — | — | — | — | — | NOT_INSTRUMENTED (no distinct publish transition: creation = publication) |
| TENDER_BID_RECEIVED | TENDER | Bid row persisted | `AuctionMixin.place_bid` | BID | `TENDER_BID_RECEIVED:{bid_id}` | YES | INSTRUMENTED |
| TENDER_WINNER_SELECTED | TENDER | Winning bid persisted with its order | `select_winning_bid` | AUCTION | `TENDER_WINNER_SELECTED:{auction_id}` (one winner per auction) | PARTIAL | INSTRUMENTED |
| TENDER_ORDER_CREATED | TENDER | Order created from the winning bid | `select_winning_bid` | ORDER | `TENDER_ORDER_CREATED:{order_id}` | YES | INSTRUMENTED |
| TENDER_DELIVERED | TENDER | Tender-linked order (`auction_id` set) reached delivery | same helper as DIRECT delivery | ORDER | `TENDER_DELIVERED:{order_id}` | PARTIAL (no OrderStatusHistory from `select_winning_bid`) | INSTRUMENTED |
| RECURRING_NEED_CREATED | RECURRING | A real `RecurringNeed` row was inserted (never a draft) | `RecurringSupplyMixin._insert_one_recurring_need` | RECURRING_NEED | `RECURRING_NEED_CREATED:{need_id}` | YES (created_at) | INSTRUMENTED |
| RECURRING_OCCURRENCE_CREATED | RECURRING | An occurrence row was actually inserted (initial window **and** daily replenishment) | `RecurringSupplyMixin._materialize_occurrences` (single point) | RECURRING_OCCURRENCE | `RECURRING_OCCURRENCE_CREATED:{occurrence_id}` | YES | INSTRUMENTED |
| RECURRING_MATCH_FOUND | RECURRING | A `need_allocations` row was materialized for an occurrence | `NeedMatchingService._persist_allocations` | NEED_ALLOCATION | `RECURRING_MATCH_FOUND:{allocation_id}` | PARTIAL (allocations get expired/overwritten) | INSTRUMENTED |
| RECURRING_DIGEST_SENT | RECURRING | Digest **queued for outbound delivery** (row inserted in `notification_outbox`) — NOT provider-confirmed reception | `RecurringSupplyDigestService.run` | RECURRING_DIGEST (`uuid5(dedupe_key)`) | `RECURRING_DIGEST_SENT:{digest dedupe_key}` | PARTIAL (`notified_at`) | INSTRUMENTED |
| RECURRING_DIGEST_ACCEPTED | RECURRING | Acceptance persisted (orders created, allocations CONVERTED, occurrence ACCEPTED/PARTIALLY_ACCEPTED) | `RecurringSupplyMixin.accept_match_proposal` (ACCEPT) | RECURRING_OCCURRENCE | `RECURRING_DIGEST_ACCEPTED:{occurrence_id}` | YES (`accepted_at`) | INSTRUMENTED |
| RECURRING_DIGEST_MODIFIED | RECURRING | — | — | — | — | NO | NOT_INSTRUMENTED (out of Phase C scope) |
| RECURRING_OCCURRENCE_SKIPPED | RECURRING | Occurrence persisted as `SKIPPED` | `RecurringSupplyMixin._apply_occurrence_skip` | RECURRING_OCCURRENCE | `RECURRING_OCCURRENCE_SKIPPED:{occurrence_id}` | PARTIAL (status only, no timestamp) | INSTRUMENTED |
| RECURRING_OCCURRENCE_CONFIRMED | RECURRING | — | — | — | — | — | NOT_INSTRUMENTED (out of scope) |
| RECURRING_OCCURRENCE_DELIVERED | RECURRING | — | — | — | — | **NO** | NOT_INSTRUMENTED — `quantity_delivered` has no writer; the real signal is `RECEIVED` on the linked order |

## Details

### DIRECT_ORDER_CONFIRMED — NOT_INSTRUMENTED
*Reason:* no single reliable canonical business transition currently identified. "Confirmed" is reached through
several independent paths (`confirm_order_by_producer`, cash confirmation, escrow flows, the preorder confirm
in the conversational layer) that set different fields (`Order.status`, `payment_status`, `confirmed_at`), and
`confirmed_at` is also reused for *delivery* confirmation (`verify_delivery_otp` and `confirm_delivery_and_payment`
write it at delivery time). Deducing "confirmed" from any of these would double-count or mislabel.

*What is needed to instrument it:* one service-level function that owns the `PENDING_PRODUCER_CONFIRMATION → CONFIRMED`
transition for non-recurring, non-tender orders (or an `OrderStatusHistory` row written for every such transition),
emitted with key `DIRECT_ORDER_CONFIRMED:{order_id}`.

*Impact:* `order_confirmation_rate`-style metrics have no real-time source; they can only be approximated from the
current `Order.status`, which drops orders that were later cancelled or delivered. Phase D must not publish them as exact.

### Delivery rule (DELIVERED / FULFILLED / RECEIVED)
Order-type conditional, implemented once in `BusinessEventEmitter.emit_order_delivered`:
- `order_type = RECURRING_SUPPLY` → **skipped** (its `DELIVERED` is the producer's intermediate claim; the buyer-confirmed
  state is `RECEIVED`, and `RECURRING_OCCURRENCE_DELIVERED` is not instrumented).
- `auction_id IS NOT NULL` → `TENDER_DELIVERED`; otherwise `DIRECT_ORDER_DELIVERED`.
- `FULFILLED` is only written by walk-in `record_sale` (`DIRECT_SALE`, no buyer profile) — deliberately not a buyer-journey event.

### RECURRING_MATCH_FOUND
One event per `need_allocations` row. The upsert `(occurrence, producer, product)` keeps the allocation `id` across
rematches, so a retry or an identical rematch hits the same key. Emitted only when the active allocation set
actually changed. If an allocation's quantity/price later changes, the event keeps its **first** values (the fact
"a match was found" is not repeated); current values live in `need_allocations`.

### RECURRING_DIGEST_SENT
Guarantee level = *queued for outbound delivery*. No WhatsApp/provider receipt exists in this system, so nothing
stronger is claimed (`metadata.delivery_guarantee = QUEUED_FOR_OUTBOUND_DELIVERY`).

### Quantities and units
`quantity`/`unit` are the original values. `canonical_*` is only set when the pair is aggregation-compatible
(mass↔mass, volume↔volume): `1500 G` → `1.5 KG`; `20 TETE` → `20 TETE`; `10 SAC` is never converted. For
RECURRING need/occurrence events the canonical unit is the sub-category `priority_unit`; MATCH/ACCEPT/SKIP events
keep their own unit as canonical target (no extra lookup on those paths).

### Identity
`actor_id` = `auth.users.id`; `buyer_id` = `BuyerProfile.id`; `producer_id` = `Producer.id`. `marketplace.clients` is never used.
System-driven events (matching, digest, cron replenishment, delivery helper) use `actor_type = SYSTEM`.

## Known data-quality gaps (never back-filled)
1. `RecurringNeedOccurrence.quantity_delivered` has no writer → no recurring delivered quantity, historical or live.
2. `DIRECT_ORDER_CONFIRMED` not instrumented (above).
3. `select_winning_bid` writes no `OrderStatusHistory` → tender timeline history is partial.
4. Search events (`DIRECT_SEARCH_*`) have no history before Phase C.
5. Events for facts created before the Phase C deploy do not exist; only forward data is trustworthy.
