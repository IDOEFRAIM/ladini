from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from .constants import PermissionScope, RiskLevel


class MCPServerKind(str, Enum):
    RAG = "rag"
    DB = "db"


class MCPToolMeta(BaseModel):
    name: str
    server: MCPServerKind
    scope: PermissionScope
    risk: RiskLevel
    description: str = ""
    timeout_seconds: float = Field(default=15.0, ge=0.1)
    retries: int = Field(default=1, ge=0, le=5)


class MCPToolCall(BaseModel):
    tool: str
    args: Dict[str, Any] = Field(default_factory=dict)
    session_id: Optional[str] = None


class MCPToolResult(BaseModel):
    ok: bool
    tool: str
    result: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
