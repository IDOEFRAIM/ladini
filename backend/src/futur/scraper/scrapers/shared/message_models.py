from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, HttpUrl


class ScraperQueueMessage(BaseModel):
    """SQS payload schema for PDF discovery events."""

    url: HttpUrl
    source_id: str = Field(min_length=1)
    partner_metadata: Dict[str, Any] = Field(default_factory=dict)
    trace_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
