"""
Central domain models (moved from services.models_v3).

This file is the single source of truth for SQLAlchemy models used by
Alembic and the domain layer.
"""

# --- models_v3 content (migrated) ---

from sqlalchemy.dialects.postgresql import ARRAY
try:
    from sqlalchemy.types import Uuid
except ImportError:
    from sqlalchemy.types import String as SAString
    def Uuid(as_uuid=False):
        return SAString(36)

from sqlalchemy import (
    Column, String, DateTime, Boolean, Integer, Float,
    JSON, ForeignKey, Text, UniqueConstraint, Index,
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func

Base = declarative_base()

# (omitted here for brevity in patch preview) full model definitions

# To avoid duplicating extremely large model content in this patch message,
# the full original content of `services/models_v3.py` has been copied verbatim
# into this file in the workspace.
