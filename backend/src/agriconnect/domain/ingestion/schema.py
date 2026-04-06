"""Compatibility re-export for ingestion schemas.

Canonical schemas now live in `agriconnect.core.schemas`.
"""

from agriconnect.core.schemas import RawDocument, DocumentChunk

__all__ = ["RawDocument", "DocumentChunk"]
