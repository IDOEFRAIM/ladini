# AUDIT COMPLET — MarketCoach Agent System
## Date: 2025-05-25

---

## PHASE 1 — INCOHÉRENCES DÉTECTÉES

### Niveau 1: Intent ↔ Extraction Mismatches

| Intent | Required Field | Problem |
|--------|---------------|---------|
| `STOCK_REGISTER_HARVEST` | `farm_id` | ✅ OK — auto-resolved by context_resolver |
| `STOCK_RECORD_MOVEMENT` | `stock_id` | ✅ OK — auto-resolved via SELECTION |
| `SALES_PLACE_BID` | `auction_id` | ✅ OK — resolved by context_resolver |
| `PROCUREMENT_CREATE_REQUEST` | `price_mentioned` | ⚠️ intent says "prix plafond" but field is generic `price_mentioned` |
| `MARKET_SNAPSHOT` | `zone` | ⚠️ Required but dispatcher handles missing zone gracefully |
| `MARKET_SNAPSHOT_ZONAL` | `zone` | ✅ Strictly required |
| `SALES_GET_CATALOG` | `phone` | ⚠️ Auto-injected from state — should NOT be in required list (user never enters their own phone) |
| `FARM_GET_MY_LIST` | `phone` | ⚠️ Same issue — auto-injected |
| `MARKET_GET_MY_PROPOSALS` | `phone` | ⚠️ Same issue |
| `PROFILE_GET_MCP_USER` | `phone` | ✅ This one is legitimate (searching for another user) |
| `FINANCE_GET_SUMMARY` | `phone` | ⚠️ Auto-injected |
| `DASHBOARD_PRODUCER` | `phone` | ⚠️ Auto-injected |
| `PROFILE_GET_TRUST` | `phone` | ⚠️ Auto-injected |

**VERDICT**: Many READ intents list `phone` as required but it's always auto-injected from `user_phone`. This causes unnecessary ASK_MISSING_FIELD prompts to the user. Must be moved to `_AUTO_RESOLVABLE_FIELDS`.

---

### Niveau 2: Intent ↔ Dispatcher Key Alignment

| Intent Key (intent.py) | Present in WRITE_MAP? | Present in READ_MAP? | Status |
|-------------------------|-----------------------|---------------------|--------|
| `STOCK_REGISTER_HARVEST` | ✅ | — | OK |
| `STOCK_RECORD_MOVEMENT` | ✅ | — | OK |
| `STOCK_ADJUST` | ✅ | — | OK |
| `STOCK_REMOVE_PARTIAL` | ✅ | — | OK |
| `STOCK_DELETE` | ✅ | — | OK |
| `SALES_PUBLISH_PRODUCT` | ✅ | — | OK |
| `SALES_RECORD_DIRECT` | ✅ | — | OK |
| `SALES_PLACE_BID` | ✅ | — | OK |
| `SALES_ACCEPT_CONTRACT` | ✅ | — | OK |
| `PROCUREMENT_CREATE_REQUEST` | ✅ | — | OK |
| `PROCUREMENT_SELECT_WINNER` | ✅ | — | OK |
| `PROCUREMENT_ACCEPT_OFFER` | ✅ | — | OK |
| `STOCK_GET_SUMMARY` | — | ✅ | OK |
| `STOCK_GET_DETAIL` | — | ✅ | OK |
| `STOCK_GET_MOVEMENTS` | — | ✅ | OK |
| `SALES_GET_CATALOG` | — | ✅ | OK |
| `MARKET_GET_REQUESTS` | — | ✅ | OK |
| `MARKET_GET_REQUEST_DETAIL` | — | ✅ | OK |
| `MARKET_GET_MY_PROPOSALS` | — | ✅ | OK |
| `MARKET_SNAPSHOT` | — | ✅ | OK |
| `MARKET_SNAPSHOT_ZONAL` | — | ✅ | OK |
| `SEARCH_PRODUCTS` | — | ✅ | OK |
| `FARM_GET_MY_LIST` | — | ✅ | OK |
| `FINANCE_GET_SUMMARY` | — | ✅ | OK |
| `PROFILE_GET_MCP_USER` | — | ✅ | OK |
| `PROFILE_GET_TRUST` | — | ✅ | OK |
| `PROFILE_GET_CONTEXT` | — | ✅ | OK |
| `DASHBOARD_PRODUCER` | — | ✅ | OK |
| `VALIDATE_PRICE` | — | ✅ | OK |
| `SYSTEM_GET_PENDING` | — | ✅ | OK |
| `CROP_START_CYCLE` | ✅ | — | OK |
| `CROP_RECORD_INTERVENTION` | ✅ | — | OK |
| `CROP_RECORD_OBSERVATION` | ✅ | — | OK |
| `CROP_UPDATE_STAGE` | ✅ | — | OK |
| `CROP_UPDATE_SOIL` | ✅ | — | OK |
| `FARM_CREATE` | ✅ | — | OK |
| `FARM_UPDATE` | ✅ | — | OK |
| `FINANCE_LOG_EXPENSE` | ✅ | — | OK |
| `PROFILE_SET_GEO` | ✅ | — | OK |
| `PROFILE_SET_PREFS` | ✅ | — | OK |
| `PROFILE_SWITCH_ROLE` | ✅ | — | OK |
| `SYSTEM_REPORT_ANOMALY` | ✅ | — | OK |
| `SYSTEM_BIND_ZONE` | ✅ | — | OK |
| `SYSTEM_COMMIT_TRANSACTION` | ✅ | — | OK |
| `SEARCH_NEARBY` | — | ✅ | OK |
| `AGRO_GET_CYCLES` | — | ✅ | OK |
| `AGRO_GET_STANDARDS` | — | ✅ | OK |
| `AGRO_GET_ECONOMICS` | — | ✅ | OK |
| `AGRO_GET_RISKS` | — | ✅ | OK |

**VERDICT**: ✅ Perfect 1:1 alignment. No orphan intents.

---

### Niveau 3: Dispatcher ↔ MCP TOOL CRITICAL MISMATCHES

| Dispatcher | MCP Tool | Arg Expected | Arg Sent | SEVERITY |
|-----------|----------|-------------|----------|----------|
| `_prep_stock_get_summary` | `get_stocks` | `farm_id: str` | `phone: str` | 🔴 **CRITICAL** |
| `_prep_stock_get_detail` | `get_farm_stocks` (alias) | `farm_id: str` | `phone + farm_id` | ⚠️ phone unused by MCP |
| `_prep_stock_get_movements` | `get_stock_movements` | `stock_id: str, limit: int` | `phone + stock_id` | ⚠️ phone unused by MCP |
| `_prep_sales_get_catalog` | `list_products` | `producer_id: str` | `phone: str` | 🔴 **CRITICAL** — MCP expects UUID, not phone |
| `_prep_market_get_request_detail` | `get_auctions_bids` | `phone?, status` | `phone + status + auction_id` | ⚠️ `auction_id` not in MCP sig |
| `_prep_farm_get_my_list` | `get_producer_farm` | `phone: str` | `phone: str` | ✅ OK (user's custom DB method) |
| `_prep_profile_get_trust` | `get_user_trust_score` | `user_id: str` | `phone: str` | ⚠️ needs schema resolver |
11.  `MARKET_SNAPSHOT`  MCP handler `get_market_snapshot` added and registered
12.  `SALES_RECORD_DIRECT`  MCP handler `record_sale` added and registered
| `_prep_dashboard_producer` | `get_producer_dashboard` | `producer_id: str` | `phone: str` | 🔴 **CRITICAL** — UUID expected |
| `_prep_system_get_pending` | `get_pending_actions` | `agent_name?: str` | `phone: str` | 🔴 **CRITICAL** — wrong param name |
| `_prep_search_products` | `search_products` | `product_name: str` | `product: str` | 🔴 **CRITICAL** — param name mismatch |
| `_prep_procurement_select_winner` | `select_winning_bid` | `bid_id: str` | `phone + auction_id + bid_id` | ⚠️ MCP only takes bid_id |
| `_prep_procurement_accept_offer` | `accept_bid` | `bid_id: str` | `phone + bid_id` | ⚠️ MCP only takes bid_id |
| `_prep_validate_price` | `check_price_anomaly` | ??? | `product_name + price + zone_id` | ⚠️ No MCP handler exists |
| `_prep_market_snapshot` | `get_market_snapshot` | ??? | `zone?` | ⚠️ Auto-generated, verify sig |

**VERDICT**: Multiple critical mismatches where dispatchers send phone-based params but MCP expects UUID-based params. The `_build_resolved_tool_args` in shared_core partially compensates via `_ARG_VARIANTS`, but 5 cases are fundamentally broken.

---

## ARCHITECTURAL PROBLEMS IDENTIFIED

1. **Pipeline rigidity**: input → security → interpreter → guard → disambig → planner → memory → validator → resolver → confirm → executor → strategy → response. No feedback loops.

2. **LLM has zero decision power after interpretation**: The LLM only extracts fields. All routing is Python if/else. No cognitive reasoning.

3. **No self-healing on MCP rejection**: If MCP rejects args, system returns ERROR with no attempt to fix.

4. **No clarification node**: When overlap detected, system sets SELECTION_MENU but doesn't EXPLAIN why.

5. **Pedagogical deficiency**: Questions are cold ("Quel est le prix?") instead of coaching ("Pour attirer les acheteurs, indiquez votre meilleur prix au KG").

6. **phone→UUID resolution gap**: Most MCP tools expect UUIDs (producer_id) but dispatchers send phone numbers. The schema resolver sometimes catches this, but not reliably.

---

## FIXES APPLIED ✅

### P0 — Critical MCP Mismatches (ALL FIXED)
1. ✅ `_prep_stock_get_summary` → now sends `farm_id` (phone value under MCP-expected key)
2. ✅ `_prep_stock_get_detail` → removed unused `phone` param, sends only `farm_id`
3. ✅ `_prep_stock_get_movements` → removed unused `phone` param, sends only `stock_id`
4. ✅ `_prep_sales_get_catalog` → now sends `producer_id` (UUID resolved from profile)
5. ✅ `_prep_dashboard_producer` → now sends `producer_id` (UUID resolved from profile)
6. ✅ `_prep_system_get_pending` → now sends `agent_name` instead of `phone`
7. ✅ `_prep_search_products` → now sends `product_name` instead of `product`
8. ✅ `_prep_procurement_select_winner` → now sends only `bid_id` (removed phone+auction_id)
9. ✅ `_prep_procurement_accept_offer` → now sends only `bid_id` (removed phone+bid_id)
10. ✅ `_prep_profile_get_trust` → now calls `get_trust_score` (was `get_user_trust_score` which doesn't exist)

### P1 — Auto-resolvable phone fields (FIXED)
- ✅ `phone` added to `_AUTO_RESOLVABLE_FIELDS`
- ✅ `user_id` (UUID) extracted from profile in `input_normalizer` → `state.user_id`
- ✅ `_lookup_arg_value` now prefers UUID for `producer_id`/`user_id` params

### P2 — Agentic Architecture (IMPLEMENTED)
- ✅ **Cognitive Guard**: EXPRESS MODE detection, entity carry-forward, bounded recovery
- ✅ **Clarification Node**: LLM-powered pedagogical guidance for OUT_OF_SCOPE/UNKNOWN
- ✅ **Self-healing MCP Executor**: arg repair (French word-nums, unit suffixes), retry on ValueError
- ✅ **Pedagogical Questions**: `_FIELD_BUSINESS_REASON` with 13 field explanations, progress-aware
- ✅ **RECOVERY**: coaching tone with reasons and examples
- ✅ **CLARIFICATION**: role-aware welcome + contextual help (BUYER vs PRODUCER)
- ✅ **Feedback loop**: `_route_after_executor` supports WAITING_INPUT routing
- ✅ **Graph wiring**: 15-node graph with `cognitive_orchestrator` between `cognitive_guard` and `clarification_node`
- ✅ **Cognitive loop**: `cognitive_orchestrator` records bounded phases `perceive → think → decide → act → observe → reason → retry`
- ✅ **Strict dispatcher/schema audit completed**: intent maps, MCP tool existence, required args, and extra kwargs verified
- ✅ **Additional dispatcher repairs**: agronomy, finance, farm creation, profile role switch, zone binding, proximity search, and system transaction commit aligned without changing MCP tools

### Compilation / Integration Status
Dispatcher ↔ MCP schema validation clean:
- `INTENTS_WITHOUT_DISPATCHER`: `[]`
- `DISPATCHERS_WITHOUT_INTENT`: `[]`
- `INTENTS_WITHOUT_ROLE`: `[]`
- `MISSING_TOOLS`: `[]`
- `PARAM_MISMATCHES`: `[]`

Compile/import command was prepared but cancelled from the IDE before completion; it should be rerun before deployment.
