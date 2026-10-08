"""ORM des campagnes de disponibilités (diffusion proactive WhatsApp) — miroir de la migration 0016.

Quatre tables Drizzle-authored (voir `schema_contract/migrations/0016_availability_campaigns.sql`) :

* `availability_campaigns`            : la campagne (contenu figé à la validation, planification).
* `availability_campaign_recipients`  : un suivi INDÉPENDANT par destinataire et par exécution ;
                                        `UNIQUE (campaign_id, run_key, phone)` = garde anti-doublon en base.
* `communication_consents`            : consentement explicite / désinscription, avec preuve.
* `availability_interests`            : intérêt exprimé en réponse à une campagne (jamais une commande).
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from ladini.domain.orm_base import Base, _uuid4

_PK = dict(primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))


def _created_at() -> Column:
    return Column(DateTime, server_default=func.now(), nullable=False)


def _updated_at() -> Column:
    return Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class AvailabilityCampaign(Base):
    __tablename__ = "availability_campaigns"
    __table_args__ = (
        Index("availability_campaigns_due_idx", "status", "next_run_at"),
        Index("availability_campaigns_validated_by_idx", "validated_by_id"),
        Index("availability_campaigns_created_by_idx", "created_by_id"),
        CheckConstraint(
            "status in ('DRAFT','SCHEDULED','RUNNING','COMPLETED','CANCELLED','FAILED')",
            name="availability_campaigns_status_chk",
        ),
        CheckConstraint("frequency in ('ONCE','WEEKLY','CUSTOM_DAYS')", name="availability_campaigns_frequency_chk"),
        CheckConstraint("content_version >= 1", name="availability_campaigns_version_chk"),
        CheckConstraint("interval_days is null or interval_days >= 1", name="availability_campaigns_interval_chk"),
        CheckConstraint("response_window_hours >= 1", name="availability_campaigns_window_chk"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), **_PK)
    name = Column(Text, nullable=False)
    status = Column(Text, nullable=False, server_default=text("'DRAFT'"))
    audience = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    offer_filter = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    send_at = Column(DateTime)
    frequency = Column(Text, nullable=False, server_default=text("'ONCE'"))
    interval_days = Column(Integer)
    response_window_hours = Column(Integer, nullable=False, server_default=text("48"))
    delivery_date = Column(Date)
    next_run_at = Column(DateTime)
    last_run_key = Column(Text)
    content_version = Column(Integer, nullable=False, server_default=text("1"))
    content_hash = Column(Text)
    preview = Column(JSONB)
    validated_at = Column(DateTime)
    validated_by_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="SET NULL"))
    created_by_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="SET NULL"))
    cancelled_at = Column(DateTime)
    last_error = Column(Text)
    created_at = _created_at()
    updated_at = _updated_at()


class AvailabilityCampaignRecipient(Base):
    __tablename__ = "availability_campaign_recipients"
    __table_args__ = (
        Index("availability_recipients_run_phone_uq", "campaign_id", "run_key", "phone", unique=True),
        Index("availability_recipients_provider_ref_idx", "provider_ref"),
        Index("availability_recipients_phone_sent_idx", "phone", "sent_at"),
        Index("availability_recipients_user_idx", "user_id"),
        Index("availability_recipients_outbox_idx", "outbox_id"),
        CheckConstraint(
            "status in ('PREPARED','QUEUED','SENT','DELIVERED','READ','REPLIED','FAILED','SKIPPED')",
            name="availability_recipients_status_chk",
        ),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), **_PK)
    campaign_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("intelligence.availability_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    run_key = Column(Text, nullable=False)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="SET NULL"))
    phone = Column(Text, nullable=False)
    status = Column(Text, nullable=False, server_default=text("'PREPARED'"))
    skip_reason = Column(Text)
    last_error = Column(Text)
    outbox_id = Column(PG_UUID(as_uuid=True), ForeignKey("intelligence.notification_outbox.id", ondelete="SET NULL"))
    provider_ref = Column(Text)
    offers = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    content_version = Column(Integer, nullable=False, server_default=text("1"))
    queued_at = Column(DateTime)
    sent_at = Column(DateTime)
    delivered_at = Column(DateTime)
    read_at = Column(DateTime)
    replied_at = Column(DateTime)
    created_at = _created_at()
    updated_at = _updated_at()


class CommunicationConsent(Base):
    __tablename__ = "communication_consents"
    __table_args__ = (
        Index("communication_consents_phone_topic_uq", "phone", "topic", unique=True),
        Index("communication_consents_user_idx", "user_id"),
        CheckConstraint("status in ('OPTED_IN','OPTED_OUT')", name="communication_consents_status_chk"),
        CheckConstraint("version >= 1", name="communication_consents_version_chk"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), **_PK)
    phone = Column(Text, nullable=False)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="SET NULL"))
    topic = Column(Text, nullable=False, server_default=text("'AVAILABILITY_CAMPAIGNS'"))
    status = Column(Text, nullable=False)
    source = Column(Text, nullable=False)
    proof = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    consented_at = Column(DateTime)
    revoked_at = Column(DateTime)
    version = Column(Integer, nullable=False, server_default=text("1"))
    created_at = _created_at()
    updated_at = _updated_at()


class AvailabilityInterest(Base):
    __tablename__ = "availability_interests"
    __table_args__ = (
        Index("availability_interests_recipient_msg_label_uq", "recipient_id", "message_ref", "product_label", unique=True),
        Index("availability_interests_campaign_idx", "campaign_id"),
        Index("availability_interests_user_idx", "user_id"),
        Index("availability_interests_product_idx", "product_id"),
        CheckConstraint(
            "kind in ('INTEREST','PRICE_REQUEST','INFO_REQUEST','PREORDER_STARTED','DECLINED')",
            name="availability_interests_kind_chk",
        ),
        CheckConstraint("status in ('OPEN','PRODUCER_NOTIFIED','CLOSED')", name="availability_interests_status_chk"),
        {"schema": "intelligence"},
    )

    id = Column(PG_UUID(as_uuid=True), **_PK)
    campaign_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("intelligence.availability_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    recipient_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("intelligence.availability_campaign_recipients.id", ondelete="CASCADE"),
        nullable=False,
    )
    phone = Column(Text, nullable=False)
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="SET NULL"))
    product_id = Column(PG_UUID(as_uuid=True), ForeignKey("marketplace.products.id", ondelete="SET NULL"))
    product_label = Column(Text, nullable=False)
    quantity = Column(DOUBLE_PRECISION)
    unit = Column(Text)
    kind = Column(Text, nullable=False)
    status = Column(Text, nullable=False, server_default=text("'OPEN'"))
    message_ref = Column(Text, nullable=False)
    changes = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    producer_notified_at = Column(DateTime)
    created_at = _created_at()
    updated_at = _updated_at()


__all__ = [
    "AvailabilityCampaign",
    "AvailabilityCampaignRecipient",
    "CommunicationConsent",
    "AvailabilityInterest",
]
