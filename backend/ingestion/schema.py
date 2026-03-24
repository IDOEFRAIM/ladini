"""
Core data schemas for the ingestion pipeline.
"""
from typing import Dict, Optional, Any
from datetime import datetime, timezone
import hashlib
from pydantic import BaseModel, Field, field_validator

class RawDocument(BaseModel):
    """Represents a raw document ingested from a source (JSON or structured extraction)."""
    source_id: str  # Unique ID from source (e.g. filename, URL hash)
    source_type: str  # 'weather_bulletin', 'news', 'fews_report'
    url: Optional[str] = None
    title: str = "Untitled"
    content: str  # Full raw text
    summary: Optional[str] = None
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_s3_key: Optional[str] = None  # Populated after S3 upload
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def ensure_content(cls, v: str):
        if not v or not v.strip():
             raise ValueError("Content cannot be empty")
        return v.strip()


class DocumentChunk(BaseModel):
    """Represents a processed chunk ready for embedding."""
    chunk_id: str  # Usually UUID
    parent_doc_source_id: str
    chunk_index: int
    text_content: str
    content_hash: str  # MD5 checksum of text_content
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding: Optional[list[float]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    
    @classmethod
    def generate_hash(cls, text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()
