"""
MCP Security — "The Shield" Layer
==================================
Zero-Trust enforcement between LLM agents and MCP tool servers.

Modules:
  - client_base  : MCPPermissionClient — Pydantic validation, scopes, audit
  - host_app     : MCPPermissionHostApp — Pre-flight SQL injection check, dynamic risk
  - client_app   : MCPSessionManager — Session-based trust, diff visualization helpers
  - constants    : Shared enums, scope definitions, sensitive columns list
"""

from .constants import PermissionScope, RiskLevel
from .client_base import MCPPermissionClient
from .host_app import MCPPermissionHostApp
from .client_app import MCPSessionManager

__all__ = [
    "PermissionScope",
    "RiskLevel",
    "MCPPermissionClient",
    "MCPPermissionHostApp",
    "MCPSessionManager",
]
