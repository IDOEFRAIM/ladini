# Commercial Pricing — Pilot Checklist

Status as of 2026-09-29 (updated by the negotiation-certification pass — see
`docs/COMMERCIAL_PRICING_NEGOTIATION_CERTIFICATION_2026-09-29.md`). Original source: final
certification pass across Phases A, B1, B2a, B2b, B2c.1–B2c.5
(`docs/COMMERCIAL_PRICING_FINAL_CERTIFICATION_2026-09-29.md`). **This checklist is a tracking
artifact, not an approval — do not treat a checked box as "safe to enable," read the blocker list in
the relevant report first.**

## Landing (must happen before anything else)

- [ ] **PR #48 merged into `main`** — B2c.4 + B2c.5 are currently *not* in `main` despite #46/#47
      showing "Merged" on GitHub (they merged into their stacked parent branches, not `main`; see
      final report Phase 0/1). Re-verified fresh in the negotiation pass (2026-09-29): still OPEN,
      CI green (4/4 checks SUCCESS), `MERGEABLE`/`CLEAN`, no review yet. Nothing below is actually
      live until this lands.
- [ ] **PR #49 merged** (stacked on #48's branch, i.e. land #48 first or together) — certifies
      negotiation pricing, see below. Its CI does not auto-trigger (base is a feature branch, not
      `main` — `cicd.yml`'s `pull_request` trigger is scoped to `branches: [main]`, same reason #46/
      #47 show empty check lists); manually dispatched via `workflow_dispatch` for signal
      (run: https://github.com/IDOEFRAIM/ladini/actions/runs/36552411746).
- [ ] No new migration required for B2c.5 or the negotiation fix (`Auction`/`RecurringNeed` reused
      existing columns) — confirm this is still the accepted tradeoff before pilot (the original
      pricing framing — TOTAL_LOT vs PER_BASE_UNIT — is not retained on the persisted `Auction` row,
      only the derived per-unit ceiling; the original framing does survive in the conversation's
      certified confirmation).

## P0 blockers (see final/negotiation reports for full detail)

- [x] **`negotiation.py` / `initiate_negotiation_session` / `update_negotiation_offer`** — FIXED,
      PR #49 (`feat/commercial-pricing-negotiation-certification`). Both now go through
      parse → certify (`domain/negotiation_offer.py`, reusing `bid_pricing_flow.parse_bid_price`,
      the same engine already certified for bids) → confirm (rendered from the certified offer) →
      execute (reads the frozen offer from state, never the live text); no write happens before the
      price basis is resolved. Verified via new architecture-lock + golden A–F tests (confirmed to
      FAIL against the pre-fix code — a real revert-check), plus the full negotiation/bid/award/
      procurement/auction/commercial-pricing/confirmation/idempotency/ownership/architecture local
      test set (all green).
- [ ] **`update_auction_fields`** (via `flows/buyer/order_tracking.py`'s `PROCUREMENT_UPDATE_REQUEST`
      auction-correction flow) — **STILL OPEN, NOT fixed by PR #49.** Confirmed still present during
      this pass: `_extract_auction_price_correction`'s regex treats "prix"/"plafond"/"budget"/"coûte"
      identically and writes the captured number straight to `Auction.max_price_per_unit` with zero
      basis disambiguation (no `TOTAL_LOT` handling anywhere in that file) — a buyer correcting an
      open auction with "budget 4000000 pour tout le lot" would get it written as 4 000 000 FCFA
      *per unit*. Deliberately left out of PR #49 to keep it focused and reviewable (per the mission's
      "stop and report, don't auto-expand scope" instruction) — same fix shape as PR #49, reusing the
      same `negotiation_offer.py`/`bid_pricing_flow.py` engine, sized as its own small follow-up.
      **This alone keeps the overall verdict NOT READY** unless this specific correction feature is
      excluded from pilot scope (auction creation and negotiation are both safe; only *editing* an
      already-open auction's price via free text is not).

## Core pricing semantics

- [ ] Migrations apply cleanly on a fresh PostgreSQL (0001→latest, in order)
- [ ] Taxonomy configured for pilot products (maïs, tomate, lait, bœuf, + actual pilot list) —
      `docs/domain/sql/taxonomy_pilot_audit_READONLY.sql` run and reviewed by a human
- [ ] Producer publish, simple (PER_BASE_UNIT: "100 KG maïs à 300 FCFA/KG")
- [ ] Producer publish, package (PER_PACKAGE: "50 L lait, 500 FCFA/sachet de 0,5 L")
- [ ] Producer publish, TOTAL_LOT ("200 T pour 5M au total")
- [ ] Future production, PER_BASE_UNIT and TOTAL_LOT
- [ ] Procurement (call for tenders), PER_BASE_UNIT and TOTAL_LOT — safe (certified since B2c.5)
- [ ] Negotiation (direct buyer↔producer offer), PER_BASE_UNIT and TOTAL_LOT — **now safe (PR #49)**
- [ ] Auction price *correction* after it's open — **still unsafe, see `update_auction_fields` above**
- [ ] Bid, PER_BASE_UNIT and TOTAL_LOT
- [ ] Award / winner selection
- [ ] Buyer search (certified `pricing_label`, safe ranking)
- [ ] Direct purchase / cart
- [ ] Preorder (package snapshot survives to `OrderItem`)
- [ ] Recurring need (price cap now shown/confirmed at creation)

## Web

- [ ] Bid submit, TOTAL_LOT, ranking, award confirmation, stale-fingerprint reject, settlement,
      order snapshot (via `ladinifront`). Re-verified fresh in the negotiation pass (2026-09-29):
      - #11 (migration 0012, commercial pricing persistence contract) — **MERGED into `master`**,
        confirmed by ancestry (`drizzle/0012_commercial_pricing_persistence.sql` present on
        `origin/master`).
      - #13 (migration 0013, order item economic freeze) — **still OPEN**, CI green (Vercel + drizzle
        checks pass); `drizzle/0013_order_item_economic_freeze.sql` NOT yet on `master`. The backend's
        own migration-0013 trigger (guarded by
        `tests/architecture/test_bid_pricing_award_contract.py::TestOrderItemEconomicFieldsAreFrozenWithTheSnapshot`)
        assumes this migration is applied — **do not deploy the backend award/settlement path to an
        environment where #13 hasn't landed**.
      - #14 (web bid/award pricing basis) — **still OPEN**, base `master`, CI green, not yet landed.
      - #15 (web certified-award UX) — **still OPEN**, stacked on #14 (not yet landed either), CI
        green.
      - Land in order: #13 → #14 → #15 (or reconcile as one corrective PR, same pattern as
        backend's #48), before any web bid/award pilot traffic.

## Cross-cutting

- [ ] WhatsApp end-to-end smoke suite green (`test_all_manual_smoke_flows_complete` — was RED on CI,
      fixed this pass; confirm still green after the fixes above land)
- [ ] Real-LLM difficult-phrasing spot check ("500f le sachet", "4 millions pour tout",
      "finalement 3,5 millions pour l'ensemble", correction mid-confirmation) — **still not run in
      this environment (no live LLM credential in either certification pass); recommend running once
      before enabling the pilot.** Note: the negotiation fix's price/basis extraction
      (`parse_bid_price`) is 100% deterministic regex, not LLM-dependent — the LLM is only used for
      the adaptive "I didn't understand, let me ask again" acknowledgment text, never for deciding an
      amount or its basis. An LLM misclassification there can at most trigger an extra clarification
      turn, never an unsafe write. The spot check is still worth running for UX quality, not for this
      safety property specifically.
- [ ] Idempotency: double "oui", double webhook, MCP retry — covered by existing
      `test_execution_idempotency_key.py`/`test_mcp_idempotency.py`, exercised in CI. Negotiation
      opening/counter-offer confirm now has its own `claim_once` guard (same fail-open-if-Redis-down
      philosophy as `CertifiedAwardDecision`'s idempotency_key) — a double "oui"/webhook redelivery on
      the exact same certified terms produces one write, not two.
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
