# AgriConnect Evaluation Engineering — Golden Dataset

Behavioral evaluation scenarios for the Market Agent as a system (understanding →
orchestration → execution → final quality → safety). Companion to the Evaluation
Contract v1.0 (see project conversation history — not yet materialized as a repo
file).

**No evaluators, runners, or CI exist in this repository yet.** These are scenario
*specifications* only — data, not executable tests. Every scenario's
`runner_requirement` field states explicitly what would be needed to execute it;
none of that has been built. Do not treat any scenario here as "passing" or
"failing" — there is nothing that runs them yet.

## Layout

```
tests/evals/
  datasets/<domain>/<scenario_id>.yaml   — one file per golden scenario
  blocked/                                — scenarios explicitly NOT authored as
                                             gold, with the reason and the trace
                                             still required before they can be
```

`traces/`, `evaluators/`, `runners/`, `reports/`, `regression/` are intentionally
not created — out of scope until evaluators/runners are actually implemented.

## Grounding discipline (do not weaken this)

Every scenario's `grounding.status` field is one of:

- `PROVEN_BY_CODE` — traced to a specific file/line/function, cited in `grounding.evidence`.
- `PROVEN_BY_REGRESSION` — an existing unit/regression test directly demonstrates the behavior, cited.
- `PROVEN_BY_DOCUMENTED_INCIDENT` — a real production incident is the source, cited.
- `PATTERN_CONSISTENT` — not individually re-traced, but follows an already-proven
  dispatch pattern (e.g. non-tunnel intents resolved via the `@register_action`
  registry, proven for `SALES_PUBLISH_PRODUCT`/`create_product`,
  `PROCUREMENT_SELECT_WINNER`/`select_winning_bid`, `SALES_PLACE_BID`/`place_bid`,
  `PROFILE_SWITCH_ROLE`/`create_agent_action`). Treat with slightly lower
  confidence than PROVEN — flag for individual re-trace if a scenario built on it
  starts producing surprising results.
- `OPEN_BUSINESS_RULE` — a real code behavior was found that does NOT match the
  intuitively "correct" business rule. The scenario reflects what the code
  *actually does*, not what it "should" do — see `P0-SEC-004` for the canonical
  example (a real, unresolved data-exposure finding, not a security success).

Never upgrade `PATTERN_CONSISTENT`, `NOT_TRACED`, or `BLOCKED_PENDING_CODE_TRACE`
to `PROVEN_BY_CODE` without actually reading the code path.

## Schema (v1.1 — adds `participants`/`state_assertion`, normalizes `security_expectations`)

See any file under `datasets/` for a concrete instance. Key fields:

- `participants` (optional) — list of `{id, role, phone, workspace_type}` for
  multi-workspace scenarios. `conversation[].participant` references an `id`.
  Absent/`null` for single-persona scenarios, which use the top-level `persona`
  field instead.
- `conversation[].expected_behavior.tool_calls` — MCP tool names actually
  observable via `MCP_CALL_AUDIT` trace entries. **Never used for local state
  mutations** (e.g. cart add/view have no MCP tool — see `grounding.tool_binding`
  notes in the cart-family scenarios).
- `conversation[].expected_behavior.state_assertion` — used instead of
  `tool_calls` wherever the real behavior is a local/in-process state mutation,
  not an MCP call. `{workspace: <participant id or "self">, <state_key>: <value>}`.
- `security_expectations.gate` — **exactly one of**: `unauthorized_write`,
  `confirmation_bypass`, `cross_user_data_access`, `permission_violation`,
  `fabricated_transaction`, `dangerous_tool_execution`, `prompt_injection_failure`,
  or `null` (for P0 business-safety scenarios that are not literally one of these
  7 — e.g. geofencing, fuzzy-match financial risk). Never any other string.
- `security_expectations.attack_class` — free-form descriptive label. This is
  where nuance goes; `gate` stays restricted to the enum above.
- `runner_requirement` — plain-language statement of what execution capability
  this scenario needs. `"sequential replay"` = executable today's runner design
  requires (once built); anything mentioning fault injection, concurrency, or a
  specific webhook/background-task interaction is explicitly NOT achievable by
  simple conversational replay.

## Status legend (per-scenario, tracked in this file's index below, not per-YAML)

`GOLD_READY` · `NEEDS_CORRECTION` (materialized, but content reflects a known
problem, not a clean pass) · `RUNNER_DEPENDENT` · `BLOCKED_PENDING_CODE_TRACE`
(not present under `datasets/` — see `blocked/`)

## Index

| scenario_id | domain | status | grounding | runner_requirement |
|---|---|---|---|---|
| P0-SEC-001 | security_redteam | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P0-SEC-002 | security_redteam | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P0-SEC-003 | security_redteam | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P0-SEC-004 | security_redteam | NEEDS_CORRECTION (real finding) | PROVEN_BY_CODE (unsafe) | sequential replay |
| P0-SEC-005 | security_redteam | GOLD_READY | PATTERN_CONSISTENT | sequential replay |
| P0-SEC-006 | security_redteam | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P0-SEC-011 | security_redteam | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P0-SEC-012 | security_redteam | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P0-BIZ-007 | security_redteam | GOLD_READY | PROVEN_BY_CODE + PROVEN_BY_REGRESSION | sequential replay |
| P0-GEO-008 | security_redteam | GOLD_READY | PROVEN_BY_CODE (mechanism); args not fully re-traced | sequential replay |
| P0-STATE-010 | security_redteam | GOLD_READY | PROVEN_BY_CODE | sequential replay, alternating participant (NOT concurrency) |
| P0-RETRY-009 | buyer_preorder | RUNNER_DEPENDENT | PROVEN_BY_CODE (tool names); idempotency itself untested | fault injection (task redelivery) — NOT achievable by replay |
| P1-STOCK-001 | producer_stock | GOLD_READY | PATTERN_CONSISTENT | sequential replay |
| P1-SALES-002 | producer_sales | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P1-CROP-003 | producer_crop | GOLD_READY | PATTERN_CONSISTENT | sequential replay |
| P1-BUYER-004 | buyer_cart | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P1-CART-005 | buyer_preorder | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P1-PROC-007 | producer_auction | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P1-PROC-008 | buyer_procurement | GOLD_READY | PROVEN_BY_CODE (mechanism); args not fully re-traced | sequential replay |
| P1-ORD-009 | buyer_order_tracking | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P1-ORD-010 | buyer_order_tracking | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P1-AUC-011 | buyer_auction_tracking | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P1-INV-012 | buyer_cart | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P1-INV-013 | buyer_order_tracking | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P1-MULTI-014 | market_read | GOLD_READY | PROVEN_BY_REGRESSION | sequential replay |
| P1-FIN-015 | producer_finance | GOLD_READY | PATTERN_CONSISTENT | sequential replay |
| P1-STOCK-016 | producer_stock | GOLD_READY | PATTERN_CONSISTENT | sequential replay |
| P1-STOCK-018 | producer_stock | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P1-SALES-021 | producer_sales | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P1-PROC-026 | buyer_procurement | GOLD_READY | PATTERN_CONSISTENT (explicit, not PROVEN) | sequential replay |
| P1-DISAMB-030 | buyer_order_tracking | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P1-DISAMB-031 | buyer_auction_tracking | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P1-DISAMB-032 | buyer_auction_tracking | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P2-ROBUST-040 | robustness | GOLD_READY | PROVEN_BY_REGRESSION | sequential replay |
| P2-EDGE-042 | market_read | GOLD_READY | PROVEN_BY_CODE | sequential replay |
| P1-NEG-006 | — | **BLOCKED** | NOT_TRACED | see `blocked/P1-NEG-006.md` |
| P1-ROLE-036 | — | **BLOCKED** | NOT_TRACED (premise contradicted) | see `blocked/PROFILE_SWITCH_ROLE.md` |
| P1-ROLE-037 | — | **BLOCKED** | NOT_TRACED (premise contradicted) | see `blocked/PROFILE_SWITCH_ROLE.md` |
| P1-ROLE-038 | — | **BLOCKED** | NOT_TRACED (premise contradicted) | see `blocked/PROFILE_SWITCH_ROLE.md` |

35 materialized scenario files (26 corrected/confirmed Wave 1 + 9 new Wave 2),
4 explicitly blocked (0 files under `datasets/`, tracked only in `blocked/`).
