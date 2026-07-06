from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"


class VersionedPayload(BaseModel):
    schema_version: str = SCHEMA_VERSION


class RAGDocument(BaseModel):
    title: str
    excerpt: str
    source: str
    score: float = 0.0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RAGPayload(VersionedPayload):
    query: str
    documents: List[RAGDocument] = Field(default_factory=list)
    context_text: str = ""
    total_found: int = 0


class EpisodeSummary(VersionedPayload):
    episode_id: str
    user_id: str
    summary: str
    category: Optional[str] = None
    relevance_score: float = 0.0


class HealthPayload(VersionedPayload):
    status: str
    service: str
    details: Dict[str, Any] = Field(default_factory=dict)
