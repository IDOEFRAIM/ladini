# Commercial Pricing — Negotiation Certification Pass (B2c.6)

Date: 2026-09-29. Follow-up to `COMMERCIAL_PRICING_FINAL_CERTIFICATION_2026-09-29.md`, whose verdict
was **NOT READY** on one named P0 (`negotiation.py`). This pass closes that P0, re-verifies the
Git/PR landing state fresh (not trusted from the prior report), and re-scans writers/readers — which
surfaced **two additional live instances of the same bug class** the prior pass did not catch, plus an
important correction to the severity of the `update_auction_fields` finding it did catch.

## Verdict

**COMMERCIAL PRICING PILOT NOT READY.**

The negotiation P0 is fixed (PR #49). Two live P0s remain, both newly found in this pass, both the
same "ambiguous price correction" bug class, both on the **producer** side (not negotiation). A third,
previously-known finding (`update_auction_fields`) is confirmed still present but is currently
non-exploitable due to an unrelated bug — explained below, because "currently broken" is not the same
as "safe."

---

## 1. Git / PR state — re-verified fresh, not assumed

**Backend (`IDOEFRAIM/ladini`)**:

| PR | State | Base | Head | Ancestor of `origin/main`? |
|---|---|---|---|---|
| #44 | MERGED | main | b2c1-web-parity | **Yes** (confirmed via `git merge-base --is-ancestor`) |
| #45 | MERGED | main | b2c3-future-production | **Yes** |
| #46 | MERGED | **b2c3 branch** (not main) | b2c4-buyer-consumers | **No** — merged into a stacked parent branch, never reached `main` |
| #47 | MERGED | **b2c4 branch** (not main) | b2c5-procurement-preorder-recurring | **No** — same landing gap |
| #48 | **OPEN** | main | b2c5-procurement-preorder-recurring | N/A — this is the PR that would actually land #46/#47's content into `main`. CI green (4/4 SUCCESS), `MERGEABLE`/`CLEAN`, no review yet. |
| **#49 (new, this pass)** | **OPEN** | #48's head branch (b2c5) | negotiation-certification | Stacked on #48, same convention as #46/#47. CI does **not** auto-trigger (`cicd.yml`'s `pull_request` trigger is `branches: [main]` only) — manually dispatched via `workflow_dispatch`: run [36552411746](https://github.com/IDOEFRAIM/ladini/actions/runs/36552411746). |

The prior report's Phase 0/1 finding (PRs showing "Merged" on GitHub without their commits actually
being ancestors of `main`) is **confirmed still the current state** — #48 has not been merged since
the last report.

**Frontend (`IDOEFRAIM/ladinifront`)** — re-verified fresh (the prior report explicitly flagged this
as "not re-verified," so this pass did it):

| PR | State | Notes |
|---|---|---|
| #11 | **MERGED into `master`** | migration 0012 (commercial pricing persistence contract) confirmed present at `drizzle/0012_commercial_pricing_persistence.sql` on `origin/master` |
| #13 | **OPEN** | migration 0013 (order item economic freeze) — CI green (Vercel + drizzle checks), confirmed **NOT** on `master` yet. The backend already has a test (`test_bid_pricing_award_contract.py::TestOrderItemEconomicFieldsAreFrozenWithTheSnapshot`) that assumes this migration's trigger exists — **do not deploy the backend award/settlement path before #13 lands** |
| #14 | **OPEN** | web bid/award price-basis carrying, base `master`, CI green, not landed |
| #15 | **OPEN** | web certified-award UX, stacked on #14 (also not landed), CI green |

None of #13/#14/#15 have landed since the prior report — land in order #13 → #14 → #15 (or one
reconciling PR, same pattern as backend's #48) before any web bid/award pilot traffic.

---

## 2. The negotiation P0 — root cause, fix, verification

### Root cause (confirmed, matches the prior report's finding)

`flows/buyer/negotiation.py`'s `_initiate_negotiation` did `offer = float(payload.get("price"))` and
`_handle_counter_price` read `payload.get("price")` directly, both feeding straight into
`services/database/buyer.py::initiate_negotiation_session`/`update_negotiation_offer`, which write
`Auction.max_price_per_unit` with **zero** `CommercialOffer`/`PriceBasis`/provenance check. A buyer
saying "4 millions pour tout" on a 10-TONNE auction would be stored as `max_price_per_unit = 4,000,000`
— a ×10 misinterpretation, the identical class of bug already closed for `PROCUREMENT_CREATE_REQUEST`
(B2c.5) and bids (B2b).

### Fix (PR #49) — no new pricing engine, reuses what B2b/B2c.5 already built

- **`domain/negotiation_offer.py`** (new): `CertifiedNegotiationOffer`, structurally identical to the
  already-shipped `CertifiedAwardDecision` (frozen dataclass, canonical-JSON fingerprint,
  `idempotency_key`, `to_state`/`from_state` round-trip, a pure-projection `confirmation_text`). Built
  from a `CommercialPricingSnapshot` — the *same* snapshot type bids already certify against — via
  `total_for(...)`, so the per-unit ceiling is Decimal-exact, never a float division.
- **`flows/buyer/negotiation.py`** (rewritten): both negotiation-opening and counter-offers now do
  **parse → certify → confirm → execute**:
  1. `bid_pricing_flow.parse_bid_price(text, auction_unit=..., auction_quantity=...)` — the same
     amount/basis/provenance extractor already certified for bids. Never resolves a bare number unless
     the question that was asked fixes the basis (`BidPriceContext.per_auction_unit`).
  2. If unresolved (`NEEDS_BASIS`/`NEEDS_PACKAGE_SIZE`/`NO_PRICE`/`INVALID`): ask, store the partial
     parse in `negotiation_context`, **no write**.
  3. If resolved: build the certified offer, render its `confirmation_text` (TOTAL_LOT shows "budget
     total... pour l'ensemble", never a per-unit number the user didn't say), ask for confirmation.
     **Still no write.**
  4. On confirm: re-reads the **frozen** offer from `negotiation_context.pending_offer` — never the
     live text or `transaction_payload` — and only then calls
     `initiate_session(offered_price=float(offer.ceiling_per_unit))` /
     `update_offer(new_price=float(offer.ceiling_per_unit))`.
  5. A price correction mid-confirmation ("finalement 4 millions pour tout") re-runs the full parser —
     it switches basis, it doesn't just replace the amount inside the old one (Golden E).
  6. `claim_once` idempotency guard on the confirm step, same fail-open philosophy as
     `CertifiedAwardDecision`.
- The `NEGOTIATION_COUNTER` menu question now names the auction's own unit
  (`price_per_unit_question(unit)`) so a bare reply resolves via `QUESTION_CONTEXT_EXPLICIT` instead
  of re-asking (Golden D).

### Verification

- **New tests** (`tests/architecture/test_negotiation_pricing_contract.py`): 3 architecture locks (no
  raw-float writer, confirmation/execution use the frozen offer, Decimal-exact normalization) + Goldens
  A–F. **Revert-checked**: 11/12 fail against the pre-fix code (the 12th exercises the untouched
  `bid_pricing_flow` parser directly, so it's unaffected by the bug either way) — this is a real proof
  the tests exercise the fix, not a vacuous pass.
- **Regression**: full negotiation/bid/award/procurement/auction/commercial-pricing/confirmation/
  idempotency/ownership/architecture local test set — green. Two pre-existing tests updated for the new
  flow shape (`test_negotiation_resilience.py`, `test_buyer_deviation_adaptivity.py`), same pattern as
  the earlier `e15eb0e` procurement-gate CI fix.
- **Ruff**: clean. **mypy**: 0 new errors (2 pre-existing `no-any-return` errors in `helpers.py`,
  confirmed present on `main` too, untouched by this PR).
- **Full local suite**: run once clean, then contaminated by this same machine's persistent local Redis
  across repeated runs (see §5) — not a code regression, see below.
- **LLM dependency**: `parse_bid_price` is 100% deterministic regex/Decimal math. The LLM is only used
  for the adaptive "I didn't understand, let me ask again" acknowledgment text — it has **zero**
  authority over the certified amount or basis (`Provenance.LLM_INFERRED`/`UNKNOWN` are the only two
  values `is_execution_safe` rejects, and this engine never produces either). An LLM mistake can only
  ever cause an extra clarification turn, never an unsafe write — a structural guarantee, not just a
  spot-check result.

---

## 3. Global writer re-scan — 2 additional live P0s found, not in PR #49

A dedicated re-scan (beyond negotiation.py) found that the **same bug class exists in two producer-side
"correction" flows**, both sharing `flows/producer/flow.py`'s own price-correction parser
(`_PRICE_RE`/`_extract_price_correction`/`_parse_update_correction`/`_format_pending_recap` — the
producer-side twin of `order_tracking.py`'s buyer-side one). Neither was touched by B2c.3–B2c.5 or by
this pass's PR #49.

| Writer | Call path | Live or blocked? | Classification |
|---|---|---|---|
| `services/database/buyer.py::update_negotiation_offer` | `negotiation.py` (this PR) | Live | **CERTIFIED** (fixed, PR #49) |
| `services/database/buyer.py::initiate_negotiation_session` | `negotiation.py` (this PR) | Live | **CERTIFIED** (fixed, PR #49) |
| `services/database/auction.py::update_auction_fields` | `order_tracking.py`'s `PROCUREMENT_UPDATE_REQUEST` correction flow | **Blocked today by an unrelated bug** (see §4) | **UNSAFE (landmine)** — zero basis guard; only not-yet-exploited because of a naming-mismatch bug elsewhere on the same path |
| `services/database/producer.py::update_production_fields` | `flow.py`'s `PRODUCTION_UPDATE_FUTURE` correction flow, line 2314 | **Live, executes today** | **UNSAFE** — `cycle.price_per_unit = positive_float(price, ...)`, zero basis check, **and** `cycle.pricing_snapshot` (the B2c.3-certified snapshot) is never invalidated — a stale certified snapshot survives asserting the OLD price/basis while the raw column changes underneath it. Buyer search (B2c.4) reads `pricing_snapshot` preferentially, so a buyer could see the *old* certified price even after a producer "corrects" it. |
| `services/database/product.py::update_product_price_and_qty` | `flow.py`'s `SALES_UPDATE_PRODUCT` correction flow | **Live, executes today** | **UNSAFE, partially mitigated** — `product.price = positive_float(price, ...)` is raw and ambiguous, **but** `invalidate_commercial_pricing_on_edit(...)` correctly blanks the certified `commercial_pricing` snapshot (a real, pre-existing B2a safeguard) rather than falsely re-certifying it. So the wrong number does land in `Product.price` (used for search/sort/display), but the system never *lies* that it's certified. |
| Every writer already covered by the prior report (`create_product`, `declare_future_production`, `create_auction`, `place_bid`/`update_bid_price`, award, `finalize_multi_order`/preorder, RECURRING allocation, `record_sale`, escrow, `Order.total_amount` sites) | — | — | Re-spot-checked, unchanged: CERTIFIED / STRUCTURALLY_EXPLICIT / LEGACY_ONLY / DISPLAY_ONLY as previously classified. `RecurringNeed.max_price_per_unit` is deliberately, permanently PER_BASE_UNIT by design (documented at `recurring_need_draft.py:260-268`) — confirmed not a gap. |

**UNSAFE WRITER COUNT: 3** (`update_auction_fields` [blocked/landmine], `update_production_fields`
[live], `update_product_price_and_qty` [live, mitigated]). Down from the effectively-unbounded risk of
`negotiation.py`'s 2 call sites, which are now certified, but **not zero**.

## 4. `update_auction_fields` — a correction to the prior report's severity, not a retraction

The prior report classified this as "LEGACY_ONLY / gap." This pass traced the full call chain and found
a nuance that changes the *current* risk (not the underlying defect): `_parse_auction_update_correction`
names its price field `"price"`, but `update_auction_fields`'s parameter is `max_price_per_unit` — there
is no rename step anywhere in `AuctionGateway.update_auction` → MCP dispatch → the DB function (which
calls the handler as `await fn(**arguments)` with no `**kwargs` catch-all). So today, a price correction
on this path raises `TypeError: update_auction_fields() got an unexpected keyword argument 'price'`,
caught by `order_tracking.py`'s own `try/except` around the gateway call, and the buyer sees "Impossible
d'enregistrer la modification..." — **a broken feature, not a silent corruption.** No test in the repo
exercises this path (confirmed by search), so this has likely never been triggered in anger.

This does **not** make it safe to leave alone: it is a landmine. Whoever notices the feature is broken
and "fixes" the kwarg name (an easy, tempting one-line change) will instantly re-open the exact silent
×10-style corruption this whole series exists to prevent, because nothing else on that path adds a
basis guard. Recommend fixing the naming bug and the basis certification **together**, in the same
follow-up, specifically so no one ships the naming fix alone.

## 5. Reader re-scan

| Reader | Classification | Reasoning |
|---|---|---|
| `flows/producer/flow.py::_format_pending_recap` (line ~1484), shared by both live producer writer bugs above | **UNSAFE** | Labels the ambiguous corrected price as `"Nouveau prix : {X} FCFA/{unit}"` — presents it to the producer as a settled per-unit fact before confirming, then the confirmed value is what gets written by the two writers in §3. |
| `services/database/buyer.py:2744` (`update_negotiation_offer`'s own log message) | **Now safe** (side effect of PR #49) | The message format (`"{old} → {price} FCFA/{unit}"`) is unchanged, but the caller (fixed `negotiation.py`) now only ever passes an already-certified per-auction-unit `new_price` — the message is truthful again. |
| `order_tracking.py`'s own price-correction recap | UNSAFE in principle, **unreachable today** | Same reasoning as §4 — dead code path until the kwarg bug is fixed, at which point this becomes live again. |
| `services/database/auction.py` bid list ordering (`ORDER BY Bid.offered_price` then re-sorted by `comparable_total`) | Not unsafe (false positive) | Initial fetch order is cosmetic; the actually-returned order is basis-normalized, non-comparable bids pushed last. |
| `pricing_persistence.py::award_total_and_snapshot`'s legacy `offered_price × quantity` fallback | Not unsafe (documented legacy-only) | Only used for pre-B2a bids with no certified snapshot; `award_decision_for` separately hard-refuses to award such bids at all (`bid_basis_unknown`). |
| All other display sites (`flows/producer/auctions.py`, `nodes/rendering/success.py`, etc.) | DISPLAY_ONLY | Read already-persisted values verbatim — correct in isolation; they will faithfully display whatever the writer bugs above wrote, which is a derivative risk of §3, not an independent reader defect. |

**UNSAFE READER COUNT: 1 live** (`flows/producer/flow.py`'s shared recap) **+ 1 currently dead**
(`order_tracking.py`'s, tied to §4).

## 6. Idempotency, ownership, concurrency (negotiation flow specifically)

- **Idempotency**: `claim_once(offer.idempotency_key)` guards the confirm step for both opening and
  counter-offers — a fingerprint of the fully-certified terms, same fail-open-if-Redis-down philosophy
  as `CertifiedAwardDecision`. Verified via a dedicated test (with `claim_once` monkeypatched to
  isolate it from this dev machine's real local Redis — see §7).
- **Ownership**: unchanged, already present and unaffected — `update_negotiation_offer` filters by
  `Auction.buyer_id == profile.id` under `.with_for_update()`, `initiate_negotiation_session` creates a
  fresh row scoped to the caller's own `buyer_id`. Not touched by this fix (only the *price value*
  passed in changed, not the guard).
- **Concurrency**: `.with_for_update()` pessimistic lock on `update_negotiation_offer`, `OPEN`-status
  guard on both writers — unchanged, confirmed present, not this pass's concern (no CAS-style race was
  introduced or removed).

## 7. Local test environment caveats (not a code regression)

- **Real local Redis contamination**: this developer machine has a real, persistent Redis reachable at
  `redis://localhost:6379/0`. Several pre-existing tests (this session's own new resilience test
  included, and pre-existing ones like `test_process_agent_task_message_dedup.py`) use deterministic,
  hardcoded idempotency-style keys — re-running the same tests within the same Redis TTL window
  produces "already claimed" flakiness **unrelated to any code change**. Demonstrated directly: the same
  4 test files pass cleanly in isolation and fail only when re-run back-to-back sharing Redis state;
  2 of those 4 (`test_turn_policy_classification.py`) fail **identically on `main`**, confirming
  pre-existing, unrelated debt. Per the mission's own instruction, CI (fresh Redis/Postgres per run) is
  the authoritative gate here, not this local, contaminated instance.
- **No live LLM credential, no live Postgres, no live Redis-for-audits** in this environment (only
  `.env.example` templates present, `DATABASE_URL` unset) — the mission's real-LLM spot-check
  (real messages against a real model) and the real-Postgres READONLY data-quality audits are
  **BLOCKED — OPERATOR REQUIRED**, not fabricated. See §2's note on why the LLM dependency is
  structurally safe regardless.

## 8. P0/P1/P2/P3 classification (this pass)

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | `negotiation.py` — 2 uncertified `Auction.max_price_per_unit` writers | P0 | **Fixed, PR #49** |
| 2 | `update_production_fields` (producer future-production correction) — uncertified write + stale certified-snapshot divergence | **P0** | **Found this pass, NOT fixed** (new scope, reported per the mission's "stop, don't auto-expand" instruction) |
| 3 | `update_product_price_and_qty` (producer catalog correction) — uncertified raw-price write, certification-lie mitigated | **P0** | **Found this pass, NOT fixed** (same reason) |
| 4 | `update_auction_fields` (buyer auction correction) — uncertified write, currently blocked by an unrelated kwarg bug | P1 today / **P0 landmine** | Confirmed still present (known from the prior report); severity nuance added this pass |
| 5 | B2c.4/B2c.5 not actually in `main` despite "Merged" PRs (#46/#47) | P0-adjacent (blocks everything) | Re-confirmed still open (PR #48, unmerged) |
| 6 | `ladinifront` PR stack (#13/#14/#15) not landed | P1 (blocks web pilot scope specifically) | Re-confirmed still open, fresh-checked this pass |

**Pilot gate requires P0 = 0. Findings #2 and #3 are open P0s. Verdict: NOT READY.**

## 9. Exact unresolved blockers before "PILOT READY"

1. Fix `update_production_fields` (producer future-production correction) — same engine as PR #49
   (`bid_pricing_flow.parse_bid_price` + a certified-offer object), **and** invalidate/refresh
   `pricing_snapshot` on any price/unit change instead of leaving it stale.
2. Fix `update_product_price_and_qty`'s correction path the same way (the snapshot-invalidation
   mitigation can stay; the raw `Product.price` write itself needs the same certify-before-write
   treatment).
3. Fix `update_auction_fields`'s naming bug **and** add a basis guard in the same change — never ship
   the naming fix alone.
4. Merge PR #48 (lands B2c.4+B2c.5), then PR #49 (or merge both as one landing), after confirming CI.
5. Land `ladinifront` #13 → #14 → #15 before any web bid/award pilot traffic.
6. Run the real-LLM difficult-phrasing spot check and the real-Postgres READONLY audits — both require
   an operator with credentials this environment doesn't have.
7. Explicit business sign-off on pilot scope and legacy policy (unchanged from the prior report).

---

## Decision point for the repo owner

Findings #2/#3 (producer-side correction flows) are the **same fix shape** as PR #49 — reuse
`bid_pricing_flow`/`negotiation_offer.py`-style certification, no new architecture. They were left out
of PR #49 per the mission's explicit "stop and report a newly-found unsafe writer, don't silently
expand scope" instruction. Whether to fix them now (as a follow-up in this same session) or treat them
as a separately-scoped, separately-reviewed phase is a scope decision for you, not one this report makes
unilaterally.
