"""Miroir SQLAlchemy de la télémétrie de l'agent (schéma `intelligence`).

Source de vérité : Drizzle (`src/db/schema/telemetry.ts`, dépôt frontend) ; ces classes en sont le miroir exact,
vérifié par `tests/schema/`. Aucune création de table ici (les migrations Drizzle en sont l'unique source).

Confidentialité : jamais de téléphone en clair (`phone_hash` HMAC + `phone_last4`), jamais d'argument/résultat d'outil.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from ladini.domain.orm_base import Base, _uuid4

_NOW = text("now()")
_ZERO = text("0")


def _tz() -> DateTime:
    return DateTime(timezone=True)


class AgentTurn(Base):
    """Un tour = un message utilisateur traité par l'agent. Append-only."""

    __tablename__ = "agent_turns"
    __table_args__ = (
        Index("agent_turns_created_idx", "created_at"),
        Index("agent_turns_conversation_idx", "conversation_id", "started_at"),
        Index("agent_turns_user_idx", "user_id"),
        Index("agent_turns_phone_idx", "phone_hash", "created_at"),
        Index("agent_turns_intent_idx", "intent", "created_at"),
        Index("agent_turns_workflow_idx", "workflow", "created_at"),
        Index("agent_turns_outcome_idx", "outcome", "created_at"),
        Index(
            "agent_turns_error_idx",
            "error_code",
            "created_at",
            postgresql_where=text("error_code IS NOT NULL"),
        ),
        Index("agent_turns_trace_idx", "trace_id", postgresql_where=text("trace_id IS NOT NULL")),
        Index(
            "agent_turns_sid_retry_uq",
            "message_sid",
            "task_retries",
            unique=True,
            postgresql_where=text("message_sid IS NOT NULL"),
        ),
        CheckConstraint(
            "outcome IN ('COMPLETED','CLARIFICATION','WAITING_USER','BLOCKED','HUMAN_REQUIRED','FALLBACK','ERROR')",
            name="agent_turns_outcome_chk",
        ),
        CheckConstraint(
            "response_status IN ('SENT','FAILED','SKIPPED','DUPLICATE')",
            name="agent_turns_response_status_chk",
        ),
        CheckConstraint("channel IN ('WHATSAPP','WEBCHAT','API')", name="agent_turns_channel_chk"),
        CheckConstraint(
            "error_category IS NULL OR error_category IN "
            "('TOOL','LLM','DB','REDIS','WHATSAPP','TIMEOUT','VALIDATION','SECURITY','INTERNAL')",
            name="agent_turns_error_category_chk",
        ),
        CheckConstraint("duration_ms >= 0", name="agent_turns_duration_chk"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    conversation_id = Column(PG_UUID(as_uuid=True), nullable=False)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="SET NULL"))
    phone_hash = Column(Text, nullable=False)
    phone_last4 = Column(Text)
    channel = Column(Text, nullable=False, server_default=text("'WHATSAPP'"))
    user_role = Column(Text)
    message_sid = Column(Text)
    task_retries = Column(Integer, nullable=False, server_default=_ZERO)

    intent = Column(Text)
    intent_confidence = Column(Float(53))
    workflow = Column(Text)
    workflow_step = Column(Text)
    goal_status = Column(Text)
    outcome = Column(Text, nullable=False)

    started_at = Column(_tz(), nullable=False)
    completed_at = Column(_tz(), nullable=False)
    duration_ms = Column(Integer, nullable=False)
    queue_duration_ms = Column(Integer)

    db_query_count = Column(Integer, nullable=False, server_default=_ZERO)
    db_duration_ms = Column(Integer, nullable=False, server_default=_ZERO)
    db_max_query_ms = Column(Integer, nullable=False, server_default=_ZERO)
    redis_command_count = Column(Integer, nullable=False, server_default=_ZERO)
    redis_duration_ms = Column(Integer, nullable=False, server_default=_ZERO)
    mcp_call_count = Column(Integer, nullable=False, server_default=_ZERO)
    mcp_duration_ms = Column(Integer, nullable=False, server_default=_ZERO)
    llm_call_count = Column(Integer, nullable=False, server_default=_ZERO)
    llm_duration_ms = Column(Integer, nullable=False, server_default=_ZERO)
    intent_llm_duration_ms = Column(Integer, nullable=False, server_default=_ZERO)
    response_llm_duration_ms = Column(Integer, nullable=False, server_default=_ZERO)
    llm_fallback_count = Column(Integer, nullable=False, server_default=_ZERO)
    whatsapp_duration_ms = Column(Integer)

    response_status = Column(Text, nullable=False)
    error_code = Column(Text)
    error_category = Column(Text)
    trace_id = Column(Text)

    user_message_excerpt = Column(Text)
    agent_response_excerpt = Column(Text)

    created_at = Column(_tz(), nullable=False, server_default=_NOW)


class AgentToolCall(Base):
    """Appel d'un outil MCP au cours d'un tour (nom, durée, statut — jamais d'arguments ni de résultat)."""

    __tablename__ = "agent_tool_calls"
    __table_args__ = (
        Index("agent_tool_calls_turn_idx", "turn_id"),
        Index("agent_tool_calls_tool_idx", "tool_name", "created_at"),
        Index("agent_tool_calls_status_idx", "status", "created_at"),
        CheckConstraint("status IN ('SUCCESS','ERROR','DENIED','REPLAY')", name="agent_tool_calls_status_chk"),
        CheckConstraint("tool_category IN ('READ','WRITE','UNKNOWN')", name="agent_tool_calls_category_chk"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    turn_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("intelligence.agent_turns.id", ondelete="CASCADE"), nullable=False
    )
    seq = Column(Integer, nullable=False)
    tool_name = Column(Text, nullable=False)
    tool_category = Column(Text, nullable=False, server_default=text("'UNKNOWN'"))
    started_at = Column(_tz(), nullable=False)
    completed_at = Column(_tz(), nullable=False)
    duration_ms = Column(Integer, nullable=False)
    status = Column(Text, nullable=False)
    error_code = Column(Text)
    error_category = Column(Text)
    trace_id = Column(Text)
    created_at = Column(_tz(), nullable=False, server_default=_NOW)


class AgentLlmCall(Base):
    """Appel LLM d'un tour (provider, modèle, durée, fallback — jamais le prompt ni la réponse)."""

    __tablename__ = "agent_llm_calls"
    __table_args__ = (
        Index("agent_llm_calls_turn_idx", "turn_id"),
        Index("agent_llm_calls_provider_idx", "provider", "created_at"),
        CheckConstraint("kind IN ('INTERPRETER','RESPONSE','OTHER')", name="agent_llm_calls_kind_chk"),
        CheckConstraint("status IN ('SUCCESS','ERROR')", name="agent_llm_calls_status_chk"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    turn_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("intelligence.agent_turns.id", ondelete="CASCADE"), nullable=False
    )
    seq = Column(Integer, nullable=False)
    kind = Column(Text, nullable=False)
    provider = Column(Text)
    model = Column(Text)
    duration_ms = Column(Integer, nullable=False)
    status = Column(Text, nullable=False)
    is_fallback = Column(Boolean, nullable=False, server_default=text("false"))
    fallback_from = Column(Text)
    error_category = Column(Text)
    prompt_tokens = Column(Integer)
    completion_tokens = Column(Integer)
    created_at = Column(_tz(), nullable=False, server_default=_NOW)


__all__ = ["AgentLlmCall", "AgentToolCall", "AgentTurn"]
