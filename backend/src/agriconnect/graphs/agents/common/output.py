"""Standardized output contract for all agent adapters."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ExpertMetadata(BaseModel):
    name: str
    confidence: float = 0.0
    sources: List[Dict[str, Any]] = Field(default_factory=list)


class AgriAgentOutput(BaseModel):
    full_text: str = ""
    structured_data: Dict[str, Any] = Field(default_factory=dict)
    handoff: Optional[str] = None
    expert_metadata: ExpertMetadata


__all__ = ["AgriAgentOutput", "ExpertMetadata"]
