# Commercial pricing — `PRODUCTION_UPDATE_FUTURE` price correction (B2c.7)

Follow-up to [ladini#49](https://github.com/IDOEFRAIM/ladini/pull/49) (negotiation, B2c.6), which
reported this writer as **P0 finding #2, not fixed** (see
`COMMERCIAL_PRICING_NEGOTIATION_CERTIFICATION_2026-09-29.md` §8/§9). Same engine, same pattern.

## The bug (two defects, one writer)

`flows/producer/flow.py::_resolve_cycle_for_update` → `StockGateway.update_production` →
`producer.py::update_production_fields`:

1. **Basis ambiguity.** The price came from the bare regex `_PRICE_RE` (`prix|coûte|vaut|N fcfa`) and
   was written as `cycle.price_per_unit = <float>` with no basis/provenance. "prix 4 millions" on a
   10-TONNE lot could become 4 000 000 FCFA/TONNE (×10) instead of a 4 000 000 FCFA lot total. The
   shared recap also printed "Nouveau prix : X FCFA/{unit}" for any price.
2. **Stale certified snapshot.** `pricing_snapshot` (B2c.3, set by `declare_future_production`) was
   never touched. After a "correction" the raw column had the new value while the snapshot still
   asserted the OLD certified price/basis — and buyer search (`market_offer_pricing_view`, B2c.4)
   reads the snapshot first, so buyers kept seeing the old price.

## The fix

| Layer | Change |
|---|---|
| `domain/production_update_offer.py` (new, pure) | `CertifiedProductionPriceCorrection`: frozen, fingerprint + idempotency key, `to_state`/`from_state` (tampered state → rejected), `recap_line()`. `build_production_price_correction` derives `price_per_unit` in `Decimal` (`quantize_normalized(total / qty)`), refuses `PER_PACKAGE` (fail-closed, as `declare_future_production`). |
| `flows/producer/flow.py` | Price is parsed with `bid_pricing_flow.parse_bid_price` against the lot's own quantity/unit (re-read via `list_producer_productions`). Bare amount → asks the basis (`update_price_pending`), reply via `resolve_basis_reply`. Certified offer frozen in `update_pending.price_offer`; execution sends only the frozen parse + `expected_fingerprint`, never a raw `price`. A later quantity/unit change re-certifies the price (or drops it and re-asks). Raw `price` in an old in-flight state is refused. |
| `_format_pending_recap` | Renders `price_offer.recap_line()` ("pour l'ensemble" for TOTAL_LOT) — no more per-unit claim for a total. Product-catalogue flow unchanged. |
| `services/database/pricing_persistence.py` | `certify_market_offer_price_correction` (re-derives snapshot + legacy `price_per_unit` server-side, checks `expected_fingerprint`) and `invalidate_market_offer_pricing_on_edit` (mirror of `invalidate_commercial_pricing_on_edit`). |
| `producer.py::update_production_fields` | New `pricing` / `expected_fingerprint` params. Certified path **replaces** `pricing_snapshot` and `price_per_unit` together. Any edit without a certified price (raw `price`, unit change, quantity change on a TOTAL_LOT) **invalidates** the snapshot — never a fake re-certification. `pricing` + `price` together is refused. |

Recompute vs invalidate: a full snapshot *can* be built from the correction (amount + basis +
provenance + the lot's known quantity/unit), so the certified path recomputes; only paths that carry
no certified price invalidate.

## Tests

`tests/architecture/test_production_update_pricing_contract.py` (23 tests): architecture locks
(no bare regex / raw price reaches the writer; snapshot certified-or-invalidated; Decimal math) +
goldens A–G at flow level (per-unit, TOTAL_LOT no ×10, ambiguous asks and never writes, basis reply
then frozen write, basis switch on correction, tampered state, quantity re-certification, package
refused) + DB goldens (buyer view shows the new price; raw write / unit change / TOTAL_LOT resize
invalidate; changed terms since confirmation rejected untouched).
Revert check: against the pre-fix `src`, 22/23 fail (the remaining one is the Decimal-math lock on
the new, untracked-in-revert domain module).
Updated for the new flow shape: `test_producer_flow_resolvers.py` (5 tests seeded raw `price`),
`test_p2_4_typed_working_memory_workflows.py` (two new working-memory keys).

## Still open (deliberately out of scope)

- `update_product_price_and_qty` correction path (catalogue) — tracked separately.
- `update_auction_fields` landmine — tracked separately.
- Other callers of `update_production_fields` with a raw `price` (e.g. the `SALES_UPDATE_PRODUCTION`
  action DTO) still write a basis-less number, but can no longer leave a stale snapshot behind.
