from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional
import hashlib

from pydantic import BaseModel, Field, HttpUrl


class RawDocument(BaseModel):
    """Canonical in-memory document emitted by scraper bricks."""

    id: str = Field(..., description="Stable SHA-256 id derived from normalized content")
    url: HttpUrl | str
    title: str
    content_markdown: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    language: Optional[str] = None


class ScraperLog(BaseModel):
    """Monitoring sidecar for each scrape execution."""

    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    scraper_name: str
    version: str = "2.0.0"
    http_status: Optional[int] = None
    duration_ms: int = 0
    bytes_downloaded: int = 0
    content_density: float = 0.0
    success: bool = False
    error_trace: Optional[str] = None


class DocumentChunk(BaseModel):
    """Canonical processed chunk used by ingestion and vector indexing."""

    chunk_id: str
    parent_doc_id: str
    chunk_index: int
    text_content: str
    content_hash: str
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding: Optional[list[float]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def generate_hash(cls, text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()
