# Protocols

Application-level protocols and adapters used to exchange structured data with clients and tools.

## Contents

| Folder/File | Role |
| --- | --- |
| `ag_ui/` | UI protocol: component schema, formatter, and renderer for WhatsApp/AG-UI messages. |
| `mcp/servers/` | MCP server wrappers exposing DB tools over stdio (used in development/runtime).

## Notes

- Keep protocol schemas here (component shapes, tool request/response) to avoid duplication across packages.
- No business logic; only translation/formatting and thin server wrappers.
