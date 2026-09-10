# Ladini System Components

This document summarizes the main subsystems requested by the team—MCP, database, market agent services, and formation services—and explains how they work together inside the current codebase.

## 1. MCP Stack

### 1.1 Unified server entry point
- `backend/src/ladini/protocols/mcp/server.py` exposes a single FastMCP instance that lazily registers every async method listed in `EXPOSED_METHODS` from the database service (`db_service`). Each handler keeps its docstring summary so tools remain self-documented. (@backend/src/ladini/protocols/mcp/server.py#1-150)
- After database handlers are registered, `_register_additional_modules` mounts stateless providers for agronomy, weather, and unit conversion. Each provider class is introspected and every public coroutine/function is registered as an MCP tool. (@backend/src/ladini/protocols/mcp/server.py#100-124)
- Health resources (`mcp://health`, `mcp://db/status`) are attached directly on the FastMCP instance so observability is available from the same endpoint without extra deployment steps. (@backend/src/ladini/protocols/mcp/server.py#69-96)

### 1.2 Handler generation, schemas, and security
- `backend/src/ladini/protocols/mcp/h.py` introspects `AgriDatabaseService` to build three synchronized maps: `TOOL_HANDLERS`, `TOOL_DESCRIPTIONS`, and `TOOL_SCHEMAS`. Wrappers are decorated with `_safe(...)`, guaranteeing structured error envelopes even if the underlying coroutine fails. (@backend/src/ladini/protocols/mcp/h.py#11-163)
- `build_mcp_infrastructure` converts Python type hints to JSON Schema so every MCP tool advertises strict argument requirements—the same schemas are later consumed by both FastMCP and the agent-side executor. (@backend/src/ladini/protocols/mcp/h.py#99-163)
- Higher-level orchestration lives in `ladini/infrastructure/mcp`. `MCPToolSpec` and `MCPServerApp` wrap handlers with Pydantic validation, request/response envelopes, and the Shield permission layer before any tool is executed. (@backend/src/ladini/infrastructure/mcp/base.py#20-149)
- `AgriDBMCPServer` is the fail-closed runtime used by agents: before invoking a handler it enforces scope membership, host preflight scans, and per-tool permission checks; every call is normalized and audited. (@backend/src/ladini/infrastructure/mcp/runtime.py#210-406)

### 1.3 Consumer pattern inside agents
- Market and Formation agents call `mc_runtime.call_db(...)` with tool names; these ultimately route through the FastMCP server above. Because tool schemas are shared, the Market executor can auto-build arguments and retry on transient errors without guessing. (@backend/src/ladini/services/market/shared_core.py#1339-1767)

## 2. Database Service

### 2.1 Session lifecycle & mixin architecture
- `AgriDatabaseService` (ContextVar edition in `services/database/d.py`) composes 12 mixins (auth, marketplace, transactions, intelligence, dashboards, crop, etc.). Each public coroutine is wrapped automatically: read-only methods reuse the current ContextVar session, while write methods open a fresh session, inject it transparently, and commit or rollback as needed. (@backend/src/ladini/services/database/d.py#1-170)
- The facade version in `services/database/database_service.py` exposes the same behaviors to FastAPI/MCP callers that may already hold a session. `_execute_transaction` either uses the injected session or creates one via `get_sessionmaker()`, translating SQLAlchemy errors into domain-specific exceptions. (@backend/src/ladini/services/database/database_service.py#1-200)

### 2.2 Capabilities
- Read helpers cover identity resolution, normalization, anomaly detection, dashboards, and crop intelligence; write helpers span farm/product CRUD, stock movements, auctions, staging flows, and trust scoring. Each mixin method is surfaced with consistent argument names so dispatcher maps (e.g., `MARKET_WRITE_ACTIONS_MAP`) can call them safely. (@backend/src/ladini/services/database/database_service.py#200-605)
- `get_producer_stocks`, `adjust_stock_by_id`, `remove_stock_by_id`, and similar helpers encapsulate authorization (producer ownership) plus unit-safe arithmetic before logging `StockMovement` records—critical for the market agent’s inventory workflows. (@backend/src/ladini/services/database/database_service.py#203-400)

### 2.3 Relationship to MCP
- Because `_READ_ONLY_METHODS` is declared centrally, the ContextVar wrapper can skip commits for read flows, giving the MCP wrapper deterministic semantics (no unwanted writes when an MCP tool only queries data). (@backend/src/ladini/services/database/d.py#56-144)

## 3. Market Agent Services

### 3.1 State graph wiring
- `services/market/graph_builder.py` compiles a 14-node LangGraph that is shared by producer and buyer personas. Routing helpers short-circuit clarification, disambiguation, and validator outputs so the graph only asks the user when necessary. (@backend/src/ladini/services/market/graph_builder.py#1-240)
- `_safe_node` wraps every node with try/except, ensuring that any crash is converted into a deterministic `status="ERROR"` update, preventing WhatsApp tunnel resets. (@backend/src/ladini/services/market/shared_core.py#75-110)

### 3.2 Shared core nodes
- `input_normalizer` handles audio transcription, profile bootstrap via `get_user_by_phone`, farm cache hydration, and tunnel bookkeeping. It also resets stale render fields each turn to avoid re-sending old AG-UI components. (@backend/src/ladini/services/market/shared_core.py#118-231)
- `memory_update` + `validator` merge interpreted entities into `transaction_payload`, infer missing slots from `INTENT_CONFIG`, and set `expected_input` for slot-filling loops. (@backend/src/ladini/services/market/shared_core.py#488-992)
- `context_resolver` (in `producer_flow.py` / `buyer_flow.py`) populates IDs such as `auction_id`, `bid_id`, or `stock_id` before writes, leveraging `available_mapping` and MCP lookups.
- `confirmation_gate` enforces explicit CONFIRM events for write intents, while read intents flow straight to execution with `execution_authorized=True`. (@backend/src/ladini/services/market/shared_core.py#1110-1297)

### 3.3 MCP executor & resilience
- `_build_resolved_tool_args` consumes the shared JSON Schemas to coerce, repair, and validate tool arguments before the MCP call; missing required inputs route back to slot-filling instead of sending placeholders. (@backend/src/ladini/services/market/shared_core.py#1339-1650)
- `mcp_tool_executor` performs dispatcher self-healing, schema validation, ASCII folding, transient retry with exponential backoff, MCP error translation, and proactive success hints—all while logging execution history for observability. (@backend/src/ladini/services/market/shared_core.py#1508-1767)

## 4. Formation Services

### 4.1 Node purposes
- `graphs/agents/formation/nodes.py` defines six guarded nodes: analyze (intent/crop extraction), validate (AG-UI prompts for missing crop/zone), consult_crop (FormationAdvisor + JSON fallback), compose (LLM synthesis), critique, and evaluate. Each node applies Postel’s Law and returns deterministic fallbacks on failure. (@backend/src/ladini/graphs/agents/formation/nodes.py#1-496)
- Retrieval helpers (`retrieve_node`, `grade_sources_node`, `rewrite_node`) provide an optional RAG loop; when no context is found, the graph degrades gracefully instead of blocking. (@backend/src/ladini/graphs/agents/formation/nodes.py#499-750)

### 4.2 Graph wiring and adapters
- `graphs/agents/formation/graph.py` assembles the sequence ANALYZE → VALIDATE → CONSULT_CROP → COMPOSE → CRITIQUE → EVALUATE with early exits when clarification is required. The adapter exposes both synchronous `handle` and async `run` entrypoints so legacy orchestrators can keep their APIs while using the new LangGraph runtime. (@backend/src/ladini/graphs/agents/formation/graph.py#1-291)
- `FormationState` stores all intermediate artifacts (learner profile, technical canvas, AG-UI components, warnings), which keeps nodes pure and simplifies testing. (@backend/src/ladini/graphs/agents/formation/state.py#1-65)

## 5. Cross-Component Interaction Map

1. **Profile bootstrap** – Market `input_normalizer` calls MCP tool `get_user_by_phone`, which resolves to `AgriDatabaseService.get_user_by_phone` via the FastMCP handler pipeline described above. (@backend/src/ladini/services/market/shared_core.py#165-220)
2. **Inventory & bidding flows** – After validator/context resolver capture product and auction metadata, `mcp_tool_executor` invokes write tools such as `create_product`, `place_bid`, or `adjust_stock_by_id`, all of which ultimately execute the database mixins within a managed session. (@backend/src/ladini/services/market/shared_core.py#1508-1767; @backend/src/ladini/services/database/database_service.py#200-605)
3. **Formation knowledge** – Formation nodes prefer live advisory data (FormationAdvisor/MCP) but fall back to local JSON when those services are degraded, guaranteeing responses even when network resources are unavailable. (@backend/src/ladini/graphs/agents/formation/nodes.py#404-496)

This overview should make it easier to onboard new contributors and reason about how conversational agents, MCP tooling, and the shared data layer fit together.
