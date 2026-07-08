# Agents package

Shared primitives used by every conversational agent (MarketCoach today, others archived under `futur/`). The modules here stay intentionally framework-agnostic so they can be reused by LangGraph nodes, async services, or CLI scripts.

## What's inside

| Module | Purpose |
| --- | --- |
| `dispatcher.py` | Declarative registry (`BaseAgentDispatcher`) that maps high-level intents to executable actions with optional MCP→DB fallbacks. |
| `gateway.py` | `DataGateway` facade that exposes resilient MCP/DB data access with context propagation and fallback handling. |
| `identity.py` | Canonical identity resolution helpers (`resolve_identity`, `UserIdentity`, extractors) used before running agent flows. |
| `forms.py` | Slot-filling utilities (`FormSpec`, `run_form_step`, built-in form definitions) shared by MarketCoach flows. |
| `onboarding.py` | Deterministic onboarding FSM that collects role/name/zone and creates a profile over `DataGateway`. |
| `reducers.py` | Pure reducer functions (`replace_value`, `merge_dict`, `_KEEP`, …) consumed by TypedDict LangGraph states. |
| `task_handler.py` | Legacy TaskHandler orchestrator kept for reference in `futur/` experiments. |

## How other packages use it

- `graphs/agents/market_coach` imports reducers, dispatcher primitives, forms and identity helpers when constructing LangGraph nodes.
- `futur/formation` still uses the dispatcher + gateway for archival flows.
- `workspace/` relies on the onboarding + identity logic to bootstrap sessions.

## When to add code here

Only place cross-agent utilities that must stay decoupled from a specific graph implementation. If logic is tied to MarketCoach, prefer keeping it under `graphs/agents/market_coach/` instead.
