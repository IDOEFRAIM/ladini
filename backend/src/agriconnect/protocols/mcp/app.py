from __future__ import annotations

import logging
from fastmcp import FastMCP

logger = logging.getLogger("MCP.app")

# Single FastMCP instance — minimal, importable without side-effects.
mcp = FastMCP("AgriConnect Database MCP Server")

__all__ = ["mcp"]
