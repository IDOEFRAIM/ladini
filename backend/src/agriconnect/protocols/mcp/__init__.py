"""MCP protocol layer — public entry points (stdio servers + tool introspection).

Layered architecture:
  * `infrastructure/mcp/`     — engine (base, client, runtime, context, security, utils)
  * `protocols/mcp/servers/`  — this layer: stdio entry points + TOOL_HANDLERS/DESCRIPTIONS/SCHEMAS
  * `graphs/agents/market_coach/services/mcp/gateway.py` — high-level agent adapter

Server entry points live in `protocols/mcp/servers/db_server.py`. The tool
registry (`TOOL_HANDLERS`, `TOOL_DESCRIPTIONS`, `TOOL_SCHEMAS`) is generated
automatically in `protocols/mcp/servers/h.py` by introspection of
`AgriDatabaseService`.
"""
