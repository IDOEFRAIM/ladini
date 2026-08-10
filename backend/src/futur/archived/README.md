# Archived modules (temporary)

This folder contains modules and packages moved out of the active codebase to reduce over‑engineering and keep production paths lean. They can be restored later if needed.

Current items moved from `agriconnect/`:

- `utils/` → moved as `agriconnect_utils/` (was empty; previously blocked by .gitignore)
- `workers/` → moved as `workers/` (was empty; previously blocked by .gitignore)
- `graphs/roles.py` (partial) → moved as `role_gating_dead_code.py` (2026-08): the
  prefix-based role gate (`is_goal_allowed`/`is_tool_allowed`/`get_allowed_goals`/
  `get_allowed_tools`) had zero live callers left after the dual-role refactor
  (a single user can now sell AND buy; real security boundary moved to identity
  pinning in `services/mcp/schema_resolver.py`). `normalize_role` stayed in
  `graphs/roles.py` — it is still used live for UI/workspace routing.

Guideline: Only reintroduce items here after confirming real production need.
