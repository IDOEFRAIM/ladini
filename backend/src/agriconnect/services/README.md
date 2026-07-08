# Services

Service layer for database operations and cross-cutting backends. This is the canonical synchronous interface to DB operations used by API and MCP servers.

## Contents

| File/Folder | Role |
| --- | --- |
| `database/` | Modules implementing concrete DB operations (auth, buyer, producer, products, auctions, etc.). |
| `memory/` | Placeholder for in-memory stores (currently empty).

## Guidelines

- Business logic at the DB boundary (validation/query composition) lives here; conversational logic stays in `graphs/`.
- Expose small, explicit functions/classes; avoid global state.
- Favor async-aware design where possible (even if DB client is sync today).
