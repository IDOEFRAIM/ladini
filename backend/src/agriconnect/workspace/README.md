# Workspace

Session and state persistence for conversations. Centralizes how agent state is loaded, checkpointed, and guarded between user turns.

## Contents

| File | Role |
| --- | --- |
| `models.py` | Workspace dataclass defining persisted fields (mono-agent MarketCoach).
| `store.py` | Persistence layer (load/save with size caps, corruption guards).
| `resolver.py` | Computes active/locked agent/tunnel flags and normalizes metadata. |
| `checkpointer.py` | LangGraph checkpointer implementation with compaction/coalescing. |
| `context_guard.py` | Minimal safety guard for agent execution.
| `metadata.py` | Helpers for pruning and serializing metadata/state.

## Principles

- Mono-agent: no over-engineering for multi-agent switching.
- Defensive: cap sizes, handle corrupt rows, never block the WhatsApp flow.
