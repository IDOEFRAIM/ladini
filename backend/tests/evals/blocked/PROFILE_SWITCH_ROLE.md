# P1-ROLE-036, P1-ROLE-037, P1-ROLE-038 — BLOCKED_PENDING_CODE_TRACE

**Not authored as gold. Do not create scenario files for these under `datasets/`.**

- Intent: `PROFILE_SWITCH_ROLE`
- Traced: `actions/profile.py:112-122` (`@register_action("PROFILE_SWITCH_ROLE",
  mode="WRITE")`) → `ProfileService.switch_role()`
  (`domain/profile.py:82-89`).
- What the code actually does: returns
  `DomainResult(tool_id=ToolId.CREATE_AGENT_ACTION, tool_args={"agent_name":
  "MarketCoach", "action_type": "PROFILE_SWITCH_ROLE", "payload": {"phone":
  ..., "target_role": ...}})`. It calls the **generic** `create_agent_action`
  tool — the same generic mechanism used for `SYSTEM_BIND_ZONE`.
- **No code was found** that:
  - mutates `active_agent` / `locked_agent` / `workspace_type` synchronously,
  - touches `active_cart` or `preorder_workflow`,
  - interacts with `confirmation_gate` state,
  - re-evaluates `INTENT_ROLE` in response to this specific action.
- `TOOL_SCOPE_MAP` (`infrastructure/mcp/security.py`) separately lists
  `update_action_status` as its own `DB_DATA_WRITE` tool — this strongly
  suggests agent actions (including role-switch requests) go through a
  create-pending → later-processed lifecycle, **not** an immediate,
  same-turn mutation. This directly contradicts the premise all three
  original stubs were written against (an immediate mid-session role flip).

**Required before authoring**: locate the actual consumer of
`action_type == "PROFILE_SWITCH_ROLE"` (likely wherever `create_agent_action`
rows get processed/approved — not yet located), and confirm whether role
switching is synchronous within a conversational turn at all. Until that's
established, no scenario can be written against a premise the code doesn't
support.
