# Graphs (LangGraph agents)

Home of the conversation graphs for agents. Today we run a single production agent: MarketCoach.

## Contents

| File/Folder | Role |
| --- | --- |
| `agents/` | Agent packages. `market_coach/` contains the full LangGraph implementation (state, nodes, interpreter, actions). |
| `factory.py` | Helpers to build/compose graphs from their components. |
| `roles.py` | Role/agent enumerations. |
| `state.py` | Shared state helpers for graphs.

## Principles

- Deterministic, testable nodes (no hidden global state).
- Business logic lives next to the agent (e.g., `market_coach/utils.py`, `nodes/`).
- Reuse cross-agent utilities from `ladini/agents/` (reducers, dispatcher, forms) when truly shared.
