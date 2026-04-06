"""Shared base config for agents."""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class BaseAgentConfig:
    llm_client: Any = None
    mcp_rag: Optional[Any] = None
    mcp_context: Optional[Any] = None
    mcp_session: Optional[Any] = None
    shield: Any = None
    retriever: Any = None
    evaluator: Any = None
    ctx: Any = None
    # internal cache for built runtime
    _runtime: Any = field(default=None, repr=False)


__all__ = ["BaseAgentConfig"]
