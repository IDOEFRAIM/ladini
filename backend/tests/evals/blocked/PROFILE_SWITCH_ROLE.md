# P1-ROLE-036, P1-ROLE-037, P1-ROLE-038 — BLOCKED_PENDING_CODE_TRACE (confirmed, stronger evidence)

**Not authored as gold. Do not create scenario files for these under `datasets/`.**

## Updated finding (this session)

Exhaustive repo-wide search:

```
grep "action_type.*PROFILE_SWITCH_ROLE" src/agriconnect/**   -> 1 file: domain/profile.py (the CREATOR only)
grep "update_action_status"              src/agriconnect/**   -> 1 file: infrastructure/mcp/security.py
                                                                  (TOOL_SCOPE_MAP declaration only —
                                                                  never actually invoked anywhere)
```

**Conclusion, now proven rather than merely "not found":** `PROFILE_SWITCH_ROLE` creates a
`create_agent_action` row (`action_type="PROFILE_SWITCH_ROLE"`, `domain/profile.py:82-89`)
that **has no consumer anywhere in this repository**. Nothing reads that action type, nothing
calls `update_action_status` on it, and nothing mutates `active_agent`/`locked_agent`/
`workspace_type`/`role` in response to it (all 4 real assignment sites for those fields are in
`workspace/store.py`, `workspace/resolver.py`, `workspace/models.py`, `workspace/checkpointer.py`
— the workspace persistence layer's own column read/write, unrelated to this action).

This rules out all 4 of the original candidate answers cleanly:
- **NOT synchronous within the turn** — nothing runs after `create_agent_action` succeeds.
- **NOT asynchronous after processing** — there is no processor.
- **NOT persistent-but-visible-next-turn** — the row is written but never read back by anything
  that would apply it.
- **The accurate description**: the feature is scaffolded (schema + creation call exist) but
  **not wired to anything** — role switching, as currently implemented, is a no-op beyond writing
  an inert database row.

Separately confirmed this session (see `P0-SEC-003` result in the P0 batch run):
`role_guard.py`'s own docstring claim that role-based blocking was removed from the graph is
**independently confirmed by actual execution** — a BUYER-workspace persona issuing a
PRODUCER-only goal (`STOCK_REGISTER_HARVEST`) successfully reached and called `add_stock`, with
no layer in the traced chain blocking it. `INTENT_ROLE` is not re-read for enforcement anywhere
at runtime; it is only used to derive `PRODUCER_INTENTS`/`BUYER_INTENTS` sets consumed at
LLM-classification time (`interpreter/routing.py`), not as a post-classification gate.

**Still required before authoring**: either locate a consumer outside this repository (an ops
tool, a manual process) that this project's tests cannot observe, or get a product decision that
this is simply unfinished and should be scoped out until built. No scenario can be written
against a mutation the code does not perform.
