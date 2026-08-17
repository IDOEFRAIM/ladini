"""ORM Intelligence — audit, actions agent, conversations, mémoire, modération, outbox.

Extrait de l'ancien `domain/models.py` (voir `orm_base.py` pour la justification
du split — même `Base` partagé, zéro changement de schéma/colonnes/index).
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from agriconnect.domain.orm_base import Base, _uuid4


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("audit_logs_actor_idx", "actor_id"),
        Index("audit_logs_entity_idx", "entity_id"),
        Index("audit_logs_entity_time_idx", "entity_type", "created_at"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    actor_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False
    )
    action = Column(Text, nullable=False)
    entity_id = Column(Text, nullable=False)
    entity_type = Column(Text, nullable=False)
    old_value = Column(JSONB)
    new_value = Column(JSONB)
    ip_address = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class AgentAction(Base):
    __tablename__ = "agent_actions"
    __table_args__ = (
        Index("agent_actions_status_idx", "status"),
        Index("agent_actions_batch_idx", "batch_id"),
        Index("agent_actions_name_idx", "agent_name"),
        Index("agent_actions_order_unique", "order_id", unique=True),
        Index("agent_actions_queue_idx", "status", "priority"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    agent_name = Column(Text, nullable=False)
    action_type = Column(Text, nullable=False)
    batch_id = Column(Text)
    payload = Column(JSONB)
    status = Column(String, default="PENDING", nullable=False)
    priority = Column(String, default="MEDIUM", nullable=False)
    order_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.orders.id"), unique=True
    )
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"))
    audit_trail_id = Column(Text)
    ai_reasoning = Column(Text)
    admin_notes = Column(Text)
    validated_by_id = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("conversations_user_idx", "user_id"),
        Index("conversations_agent_idx", "agent_type"),
        Index("conversations_created_idx", "created_at"),
        Index("conversations_followup_idx", "needs_follow_up"),
        Index("conversations_audit_unique", "audit_trail_id", unique=True),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False)
    query = Column(Text, nullable=False)
    response = Column(Text)
    agent_type = Column(Text)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    mode = Column(Text, default="text", nullable=False)
    audio_url = Column(Text)
    is_waiting_for_input = Column(Boolean, default=False, nullable=False)
    missing_slots = Column(JSONB)
    execution_path = Column(JSONB)
    confidence_score = Column(DOUBLE_PRECISION)
    user_intent = Column(Text)
    needs_follow_up = Column(Boolean, default=False, nullable=False)
    total_tokens_used = Column(Integer, default=0, nullable=False)
    response_time_ms = Column(Integer)
    audit_trail_id = Column(Text, unique=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AgentContextMemory(Base):
    __tablename__ = "agent_context_memory"
    __table_args__ = (
        Index("acm_user_idx", "user_id"),
        Index("acm_key_idx", "context_key"),
        Index("acm_user_key_unique", "user_id", "context_key", unique=True),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False)
    market_offer_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.market_offers.id")
    )
    context_key = Column(Text, nullable=False)
    context_value = Column(JSONB, nullable=False)
    source = Column(Text, default="AGENT", nullable=False)
    confidence = Column(DOUBLE_PRECISION)
    expires_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AIRatingReasoning(Base):
    __tablename__ = "ai_rating_reasonings"
    __table_args__ = (
        Index("ai_rating_trust_idx", "trust_score_id"),
        Index("ai_rating_agent_idx", "agent_name"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    trust_score_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("intelligence.trust_scores.id"),
        nullable=False,
    )
    agent_name = Column(Text, nullable=False)
    justification = Column(Text, nullable=False)
    data_points = Column(JSONB, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ModerationEvent(Base):
    """Journal auditable des strikes de modération (produits interdits, scams).

    Le nombre de strikes d'un utilisateur = COUNT sur cette table.
    """

    __tablename__ = "moderation_events"
    __table_args__ = (
        Index("moderation_events_phone_idx", "phone"),
        Index("moderation_events_user_idx", "user_id"),
        Index("moderation_events_kind_idx", "kind"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    user_id = Column(PG_UUID(as_uuid=True))
    phone = Column(Text, nullable=False)
    kind = Column(String, nullable=False)  # PROHIBITED_PRODUCT | SCAM
    matched_term = Column(Text)
    excerpt = Column(Text)
    action_taken = Column(String)  # WARNED | BANNED
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class DemandSignal(Base):
    """Demande non satisfaite : produits recherchés en vain, agrégés par terme."""

    __tablename__ = "demand_signals"
    __table_args__ = (
        Index("demand_signals_term_unique", "normalized_term", unique=True),
        Index("demand_signals_occurrences_idx", "occurrences"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    normalized_term = Column(Text, nullable=False)
    raw_query = Column(Text, nullable=False)
    phone = Column(Text)
    user_id = Column(PG_UUID(as_uuid=True))
    zone_id = Column(PG_UUID(as_uuid=True))
    occurrences = Column(Integer, default=1, nullable=False, server_default=text("1"))
    resolved = Column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Solicitation(Base):
    """Sollicitation proactive (enchère→producteur, nouveau produit→acheteur).

    Porte l'état métier + l'idempotence. Taux de conversion = RESPONDED/NOTIFIED.
    """

    __tablename__ = "solicitations"
    __table_args__ = (
        Index(
            "solicitations_auction_producer_uq",
            "auction_id",
            "target_producer_id",
            unique=True,
        ),
        Index(
            "solicitations_offer_buyer_uq",
            "market_offer_id",
            "target_buyer_id",
            unique=True,
        ),
        Index("solicitations_kind_status_idx", "kind", "status"),
        Index("solicitations_auction_idx", "auction_id"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    kind = Column(String, nullable=False)  # AUCTION_INVITE | NEW_PRODUCT_ALERT
    auction_id = Column(PG_UUID(as_uuid=True))
    market_offer_id = Column(PG_UUID(as_uuid=True))
    target_producer_id = Column(PG_UUID(as_uuid=True))
    target_buyer_id = Column(PG_UUID(as_uuid=True))
    sub_category_id = Column(PG_UUID(as_uuid=True))
    zone_id = Column(PG_UUID(as_uuid=True))
    status = Column(
        String, default="PENDING", nullable=False, server_default=text("'PENDING'")
    )
    notified_at = Column(DateTime)
    responded_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class NotificationOutbox(Base):
    """File d'attente durable de messages (Outbox Pattern). Écrite par les crons
    métier, lue/envoyée par le dispatcher — découplage total de la notification.
    """

    __tablename__ = "notification_outbox"
    __table_args__ = (
        Index("outbox_dedupe_uq", "dedupe_key", unique=True),
        Index("outbox_due_idx", "status", "next_attempt_at"),
        Index("outbox_solicitation_idx", "solicitation_id"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4)
    solicitation_id = Column(PG_UUID(as_uuid=True))
    channel = Column(String, nullable=False)  # WHATSAPP | EMAIL | PUSH | IN_APP
    recipient_user_id = Column(PG_UUID(as_uuid=True))
    recipient_phone = Column(Text)
    template_key = Column(String, nullable=False)
    payload = Column(JSONB, nullable=False)
    dedupe_key = Column(Text, nullable=False)
    status = Column(
        String, default="PENDING", nullable=False, server_default=text("'PENDING'")
    )
    attempts = Column(Integer, default=0, nullable=False, server_default=text("0"))
    max_attempts = Column(Integer, default=5, nullable=False, server_default=text("5"))
    next_attempt_at = Column(DateTime, server_default=func.now(), nullable=False)
    last_error = Column(Text)
    sent_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


__all__ = [
    "AuditLog",
    "AgentAction",
    "Conversation",
    "AgentContextMemory",
    "AIRatingReasoning",
    "ModerationEvent",
    "DemandSignal",
    "Solicitation",
    "NotificationOutbox",
]
