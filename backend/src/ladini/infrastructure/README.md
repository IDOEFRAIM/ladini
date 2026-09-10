# Infrastructure integrations

Stable adapters and clients for external runtimes. Primarily MCP (Model Context Protocol) runtime, context propagation, and security policies.

## Contents

| File | Role |
| --- | --- |
| `base.py` | Canonical definitions for MCP providers and tool specifications. |
| `client.py` | Unified MCP client used by runtimes and tests. |
| `context.py` | Context propagation (e.g., `FarmerContext`) and helpers. |
| `runtime.py` | MCP runtime orchestration and server wrappers. |
| `security.py` | Permission/risk policies and managers for tool execution. |
| `utils.py` | Small helpers related to MCP/runtime glue. |

## Usage

- `agents/gateway.py` may depend on these to call tools with the right context.
- Keep this package free of business logic; it should be reusable across agents.
