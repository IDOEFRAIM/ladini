"""Miroir SQLAlchemy Core des tables d'état runtime de l'agent.

Ces tables sont DÉFINIES par Drizzle (`src/db/schema/runtime.ts`, source de vérité)
et créées par les migrations Drizzle — JAMAIS par le backend. Les stores Python
(`services/database/*_draft_store.py`, `mcp_idempotency_store.py`,
`workspace/store.py`) y accèdent en SQL brut ; ce module les déclare pour que le
test de cohérence de schéma (`tests/schema/`) vérifie colonnes, types, nullabilité,
défauts, clés et index contre Drizzle et PostgreSQL.

Ne PAS appeler `metadata.create_all()` : voir `tests/schema/test_no_runtime_ddl.py`.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Float,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TIMESTAMP

from ladini.domain.orm_base import Base

_NOW = text("now()")


def _draft_columns() -> list:
    return [
        Column("draft_id", Text, primary_key=True),
        Column("conversation_id", Text, nullable=False),
        Column("version", Integer, nullable=False),
        Column("status", Text, nullable=False),
        Column("payload", JSONB, nullable=False),
        Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=_NOW),
        Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=_NOW),
    ]


preorder_drafts = Table(
    "preorder_drafts",
    Base.metadata,
    *_draft_columns(),
    Column("order_id", Text),
    Index("ix_preorder_drafts_conversation", "conversation_id"),
    Index(
        "ix_preorder_drafts_order_id",
        "order_id",
        postgresql_where=text("order_id IS NOT NULL"),
    ),
    schema="marketplace",
)

procurement_drafts = Table(
    "procurement_drafts",
    Base.metadata,
    *_draft_columns(),
    Index("ix_procurement_drafts_conversation", "conversation_id"),
    schema="marketplace",
)

sales_publish_drafts = Table(
    "sales_publish_drafts",
    Base.metadata,
    *_draft_columns(),
    Index("ix_sales_publish_drafts_conversation", "conversation_id"),
    schema="marketplace",
)

recurring_need_drafts = Table(
    "recurring_need_drafts",
    Base.metadata,
    *_draft_columns(),
    Index("ix_recurring_need_drafts_conversation", "conversation_id"),
    schema="marketplace",
)

mcp_idempotency_records = Table(
    "mcp_idempotency_records",
    Base.metadata,
    Column("idempotency_key", Text, nullable=False),
    Column("tool_name", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("external_result", JSONB),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=_NOW),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=_NOW),
    PrimaryKeyConstraint("idempotency_key", "tool_name"),
    schema="marketplace",
)

agri_workspaces = Table(
    "agri_workspaces",
    Base.metadata,
    Column("workspace_id", Text, primary_key=True),
    Column("workspace_type", Text, nullable=False, server_default=text("'producer'")),
    Column("active_agent", Text, nullable=False, server_default=text("'market'")),
    Column("active_goal", Text, nullable=False, server_default=text("''")),
    Column("active_form", Text),
    Column("locked_agent", Text),
    Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("langgraph_state", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("updated_at", Float(53), nullable=False, server_default=text("0")),
)

__all__ = [
    "agri_workspaces",
    "mcp_idempotency_records",
    "preorder_drafts",
    "procurement_drafts",
    "recurring_need_drafts",
    "sales_publish_drafts",
]
