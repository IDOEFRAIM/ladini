# Commercial Pricing — Pilot Checklist

Status as of 2026-09-29. Source: final certification pass across Phases A, B1, B2a, B2b, B2c.1–B2c.5.
See `docs/COMMERCIAL_PRICING_FINAL_CERTIFICATION_2026-09-29.md` for the full report and evidence
behind every line below. **This checklist is a tracking artifact, not an approval — do not treat a
checked box as "safe to enable," read the blocker list in the final report first.**

## Landing (must happen before anything else)

- [ ] **PR #48 merged into `main`** — B2c.4 + B2c.5 are currently *not* in `main` despite #46/#47
      showing "Merged" on GitHub (they merged into their stacked parent branches, not `main`; see
      final report Phase 0/1). Nothing below is actually live until this lands.
- [ ] No new migration required for B2c.5 (`Auction`/`RecurringNeed` reused existing columns) —
      confirm this is still the accepted tradeoff before pilot (original framing not retained on
      persisted `Auction` rows, only the derived per-unit ceiling).

## P0 blocker (must fix before pilot — see final report for full detail)

- [ ] **`negotiation.py` / `initiate_negotiation_session` / `update_negotiation_offer` /
      `update_auction_fields`** carry the exact same "total vs per-unit" ambiguity bug that B2c.5
      closed for `PROCUREMENT_CREATE_REQUEST` — a buyer's negotiation offer or auction-ceiling
      correction is written as a raw float with zero `CommercialOffer`/`PriceBasis` involvement.
      **Not fixed in this pass** (new-scope discovery, reported per the "stop and report" instruction
      rather than auto-fixed). This is the reason the final verdict is NOT READY.

## Core pricing semantics

- [ ] Migrations apply cleanly on a fresh PostgreSQL (0001→latest, in order)
- [ ] Taxonomy configured for pilot products (maïs, tomate, lait, bœuf, + actual pilot list) —
      `docs/domain/sql/taxonomy_pilot_audit_READONLY.sql` run and reviewed by a human
- [ ] Producer publish, simple (PER_BASE_UNIT: "100 KG maïs à 300 FCFA/KG")
- [ ] Producer publish, package (PER_PACKAGE: "50 L lait, 500 FCFA/sachet de 0,5 L")
- [ ] Producer publish, TOTAL_LOT ("200 T pour 5M au total")
- [ ] Future production, PER_BASE_UNIT and TOTAL_LOT
- [ ] Procurement (call for tenders), PER_BASE_UNIT and TOTAL_LOT — **only safe after the P0 fix above**
- [ ] Bid, PER_BASE_UNIT and TOTAL_LOT
- [ ] Award / winner selection
- [ ] Buyer search (certified `pricing_label`, safe ranking)
- [ ] Direct purchase / cart
- [ ] Preorder (package snapshot survives to `OrderItem`)
- [ ] Recurring need (price cap now shown/confirmed at creation)

## Web

- [ ] Bid submit, TOTAL_LOT, ranking, award confirmation, stale-fingerprint reject, settlement,
      order snapshot (via ladinifront — confirm PRs #13/#14/#15 status before pilot; not
      re-verified in this pass, see final report)

## Cross-cutting

- [ ] WhatsApp end-to-end smoke suite green (`test_all_manual_smoke_flows_complete` — was RED on CI,
      fixed this pass; confirm still green after the fixes above land)
- [ ] Real-LLM difficult-phrasing spot check ("500f le sachet", "4 millions pour tout",
      "finalement 3,5 millions pour l'ensemble", correction mid-confirmation) — **not run in this
      environment (no live LLM credential exercised here); recommend running once before enabling
      the pilot**
- [ ] Idempotency: double "oui", double webhook, MCP retry — covered by existing
      `test_execution_idempotency_key.py`/`test_mcp_idempotency.py`, exercised in CI
- [ ] Concurrency: two buyers on the same auction/stock/bid — covered by existing
      `tests/schema/*` PG-gated tests (CI-only, this environment has no local Postgres)
- [ ] Legacy rows read honestly (`LEGACY_PARTIAL`/`UNKNOWN_BASIS`, never an invented basis) —
      confirmed by design across all `*_pricing_view` functions
- [ ] Stock-shortage decision (TAKE_AVAILABLE/START_TENDER/CANCEL) keeps quantity and price in sync
- [ ] Observability events present with no raw user text — confirmed (`COMMERCIAL_OFFER_*`,
      `BID_PRICING_*`, `BID_AWARD_*`, `FUTURE_OFFER_*`, `PROCUREMENT_PRICING_*`,
      `BUYER_PRICING_VIEW_*` (added this pass), `PRICE_BASIS_*`)
- [ ] Rollback plan reviewed (see final report Phase 31) — no destructive migration in this series,
      app-level rollback is redeploy-previous-image

## Explicitly deferred (documented, not blocking, unless the pilot scope needs them)

- [ ] `record_sale` — already correct (TOTAL_LOT bookkeeping), no action needed
- [ ] `PROCUREMENT`/`PREORDER`/`RECURRING` matching internals — audited safe via the
      `products.price` normalization invariant, no action needed
- [ ] A real `Auction.pricing_snapshot` migration, if the team later wants the original commercial
      framing retained on persisted procurement rows (currently only the derived ceiling survives)
