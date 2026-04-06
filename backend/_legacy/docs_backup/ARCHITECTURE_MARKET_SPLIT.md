# Architecture Refactor: Market Agent vs Marketplace Agent

## Goal

Split responsibilities between:

1. `MarketCoach` (user-facing conversational assistant)
2. `MarketplaceBackgroundAgent` (system-facing matching engine)

This document describes the implemented architecture and the new DB memory primitives.

## Market Agent (`MarketCoach`)

Responsibilities:

- Natural-language parsing from user messages
- Progressive slot filling for incomplete input
- Persistent conversational memory (`pending_intent`, `draft_data`)
- User-side operations through MCP tools:
  - user identity/profile
  - stocks
  - product creation
  - surplus registration
  - orders, expenses, dashboard
- Audit persistence (`persist_conversation`)

Behavioral changes:

- Supports incomplete user input and stores draft context.
- Uses `upsert_user_context_state` when information is missing.
- Clears pending draft state when transaction payload is complete.
- Emits a background event (`create_agent_action` with `RUN_MATCHING`) after successful surplus registration.

## Marketplace Agent (`MarketplaceBackgroundAgent`)

Responsibilities:

- No direct user conversation
- Async scanning/matching using DB tools only
- Persists opportunities into `matches`

Current flow:

1. Read candidate opportunities from `search_products` and `get_open_auctions`
2. Compute lightweight score
3. Persist via `create_market_match`

## New MCP DB tools

- `get_user_context_state`
- `upsert_user_context_state`
- `create_market_match`
- `list_market_matches`

## New DB entities (ORM)

Added in `services/models_v3.py`:

- `intelligence.user_context`
  - `user_id`
  - `last_intent`
  - `pending_intent`
  - `draft_data` (JSON)
  - timestamps

- `intelligence.matches`
  - `product_id`
  - `buyer_id`
  - `score`
  - `status`
  - `meta` (JSON)
  - timestamps

## Event-driven decoupling

`MarketCoach` no longer performs matching logic directly.
It registers a matching action for background processing.

Execution worker:

- Celery task `agriconnect.workers.tasks.marketplace.process_pending_actions`
- Polls pending actions for `MarketplaceBackgroundAgent`
- Executes `RUN_MATCHING` and updates action status (`EXECUTED` or `FAILED`)

## Notes

- This refactor keeps backward compatibility with existing MCP flows.
- Existing tools and orchestration remain functional while responsibilities are cleaner.
- Production deployment should include SQL migration for `intelligence.user_context` and `intelligence.matches`.
