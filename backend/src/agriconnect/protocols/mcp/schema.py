"""MCP Schema — tool spec builder.

Re-exports canonical definitions from their single sources of truth:
  - ``PermissionScope`` / ``RiskLevel`` / ``TOOL_SCOPE_MAP`` / ``TOOL_RISK_MAP``
    → ``infrastructure.mcp.security``
  - ``MCPToolSpec``
    → ``infrastructure.mcp.base``

Provides:
  - ``build_tool_specs()`` to auto-generate specs from ``handlers.TOOL_HANDLERS``.
"""
from __future__ import annotations

import inspect
from typing import List

# Canonical definitions — single source of truth
from agriconnect.infrastructure.mcp.security import (
    PermissionScope,
    RiskLevel,
    TOOL_RISK_MAP,
    TOOL_SCOPE_MAP,
)
from agriconnect.infrastructure.mcp.base import MCPToolSpec


# ---------------------------------------------------------------------------
# Auto-build specs from handlers
# ---------------------------------------------------------------------------
def build_tool_specs() -> List[MCPToolSpec]:
    """Build a list of ``MCPToolSpec`` from ``handlers.TOOL_HANDLERS``.

    Descriptions are auto-extracted from handler docstrings.
    Timeout is set based on risk level (HIGH → 20 s, else 12 s).
    """
    from agriconnect.protocols.mcp.h import TOOL_HANDLERS

    specs: List[MCPToolSpec] = []
    for name, fn in TOOL_HANDLERS.items():
        desc = (inspect.getdoc(fn) or name).split("\n")[0]
        risk = TOOL_RISK_MAP.get(name, RiskLevel.MEDIUM)
        timeout = 20.0 if risk in (RiskLevel.HIGH, RiskLevel.CRITICAL) else 12.0
        specs.append(
            MCPToolSpec(
                name=name,
                handler=fn,
                description=desc,
                timeout_seconds=timeout,
            )
        )
    return specs
