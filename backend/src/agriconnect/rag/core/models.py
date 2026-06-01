from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass(slots=True)
class QueryBundle:
    vector: List[float]
    top_k: int = 5
    filters: Dict[str, Any] = field(default_factory=dict)
    text_query: str = ""


@dataclass(slots=True)
class Node:
    id: str
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


@dataclass(slots=True)
class SessionSummary:
    session_id: str
    parent_trace_id: str
    summary_text: str


@dataclass(slots=True)
class RetrievalContext:
    sources: List[Dict[str, Any]] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    search_strategy: str = "hybrid"
    provenance_audit: List[Dict[str, Any]] = field(default_factory=list)
    confidence_score: float = 0.0
    low_confidence: bool = False
    trace_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sources": list(self.sources or []),
            "metrics": dict(self.metrics or {}),
            "search_strategy": str(self.search_strategy or "hybrid"),
            "provenance_audit": list(self.provenance_audit or []),
            "confidence_score": float(self.confidence_score or 0.0),
            "low_confidence": bool(self.low_confidence),
            "trace_id": str(self.trace_id or ""),
            "metadata": dict(self.metadata or {}),
        }
