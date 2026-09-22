"""Miroir SQLAlchemy de l'approvisionnement récurrent (schéma `marketplace`).

Source de vérité : Drizzle (`src/db/schema/marketplace.ts`, dépôt frontend, table `recurringNeeds` et
suivantes) ; ces classes en sont le miroir exact, vérifié par `tests/schema/`. Aucune création de table
ici (les migrations Drizzle en sont l'unique source) — voir `orm_base.py`.

Phase 1 (fondation de données uniquement) : ce module ne contient QUE le schéma. Aucun domaine
applicatif (`RecurringNeedDraft`), aucun intent, aucun matching, aucune conversion en commande —
ce sera l'objet de phases séparées, décidées après validation de cette fondation.

À ne PAS confondre avec `PROCUREMENT_CREATE_REQUEST` / `Auction` (`domain/orders/models.py`) : un
appel d'offres est ponctuel et n'a qu'un seul gagnant (`bids_one_winner_per_auction_uq`) ; un besoin
récurrent est une règle permanente d'un acheteur qui se matérialise en occurrences datées, chacune
pouvant être couverte par PLUSIEURS fournisseurs (`need_allocations`).

`recurring_need_overrides` et `need_proposals` ont été délibérément écartés du modèle validé : une
occurrence (`RecurringNeedOccurrence`) porte directement son exception éventuelle (quantité modifiée,
`status='SKIPPED'`) et son état de matching — il n'existe qu'une seule ligne par (besoin, date).
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import relationship

from ladini.domain.orm_base import Base, _uuid4


class RecurringNeed(Base):
    """Règle permanente d'approvisionnement d'un acheteur (ex: "40 kg de tomate chaque jour").

    Changer le PRODUIT d'un besoin n'est jamais une mise à jour en place : le domaine applicatif (à
    venir) doit annuler ce besoin (`status='CANCELLED'`) et en créer un nouveau. Cette table ne
    l'empêche pas techniquement (aucune règle DB ne peut exprimer "ce champ est immuable après
    création") ; c'est une règle de gestion documentée ici pour la phase qui implémentera le domaine.
    """

    __tablename__ = "recurring_needs"
    __table_args__ = (
        Index("recurring_needs_buyer_idx", "buyer_id"),
        Index("recurring_needs_subcategory_status_idx", "sub_category_id", "status"),
        CheckConstraint("quantity > 0", name="recurring_needs_quantity_chk"),
        CheckConstraint(
            "max_price_per_unit IS NULL OR max_price_per_unit >= 0",
            name="recurring_needs_max_price_chk",
        ),
        CheckConstraint(
            "recurrence_type IN ('DAILY','WEEKLY_DAYS','WEEKLY','ONE_OFF')",
            name="recurring_needs_recurrence_type_chk",
        ),
        CheckConstraint("status IN ('ACTIVE','PAUSED','CANCELLED')", name="recurring_needs_status_chk"),
        CheckConstraint(
            "recurrence_type <> 'WEEKLY_DAYS' OR weekly_days IS NOT NULL",
            name="recurring_needs_weekly_days_chk",
        ),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    buyer_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.buyer_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    sub_category_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("governance.sub_categories.id", ondelete="RESTRICT"), nullable=False
    )
    quantity = Column(Numeric(14, 3), nullable=False)
    unit = Column(Text, default="KG", nullable=False, server_default=text("'KG'"))
    # DAILY | WEEKLY_DAYS | WEEKLY | ONE_OFF — volontairement fermé (pas de RRULE/cron générique).
    recurrence_type = Column(Text, nullable=False)
    # Jours ISO (1=lundi..7=dimanche) — utilisé seulement si recurrence_type = WEEKLY_DAYS.
    weekly_days = Column(PG_ARRAY(Integer), nullable=True)
    # Jours ISO exclus en permanence (ex: "tous les jours sauf le dimanche" sur un besoin DAILY) — un
    # paramètre de récurrence, PAS une exception ponctuelle (qui vit sur l'occurrence).
    excluded_weekdays = Column(PG_ARRAY(Integer), nullable=True)
    starts_at = Column(DateTime, nullable=False)
    ends_at = Column(DateTime, nullable=True)
    status = Column(Text, default="ACTIVE", nullable=False, server_default=text("'ACTIVE'"))
    paused_until = Column(DateTime, nullable=True)
    max_price_per_unit = Column(Numeric(12, 2), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    occurrences = relationship(
        "RecurringNeedOccurrence", back_populates="recurring_need", cascade="all, delete-orphan"
    )


class RecurringNeedOccurrence(Base):
    """Demande concrète d'UN jour pour un besoin récurrent.

    `requested_quantity`/`unit` sont des SNAPSHOTS pris au moment de la génération : une modification
    ultérieure de `RecurringNeed` ne réécrit JAMAIS une occurrence déjà créée (propriété testée dans
    `tests/schema/`). Porte directement l'exception ponctuelle ("demain seulement 10 kg" = modifier
    `requested_quantity` sur CETTE ligne ; "suspends demain" = `status='SKIPPED'`) — pas de table
    d'override séparée : c'est le remplacement délibéré de `recurring_need_overrides`.

    `quantity_matched`/`quantity_confirmed`/`quantity_delivered` sont `0 NOT NULL`, jamais NULL :
    NULL n'apporterait aucune sémantique de plus que 0 ("rien pour l'instant") pour des compteurs
    cumulatifs — un 3ᵉ état (NULL / 0 / valeur) sans utilité aurait juste été une source de bugs.

    `version` est le CAS de cette ligne : une confirmation qui cible la version N est refusée si
    l'occurrence est déjà en version N+1 (même principe que `ProcurementDraft.version`).
    """

    __tablename__ = "recurring_need_occurrences"
    __table_args__ = (
        Index(
            "recurring_need_occurrences_need_date_uq",
            "recurring_need_id",
            "occurrence_date",
            unique=True,
        ),
        Index("recurring_need_occurrences_status_date_idx", "status", "occurrence_date"),
        CheckConstraint("requested_quantity > 0", name="recurring_need_occurrences_requested_qty_chk"),
        CheckConstraint("quantity_matched >= 0", name="recurring_need_occurrences_matched_qty_chk"),
        CheckConstraint("quantity_confirmed >= 0", name="recurring_need_occurrences_confirmed_qty_chk"),
        CheckConstraint("quantity_delivered >= 0", name="recurring_need_occurrences_delivered_qty_chk"),
        CheckConstraint("version >= 1", name="recurring_need_occurrences_version_chk"),
        CheckConstraint(
            "status IN ('OPEN','SKIPPED','MATCHED','PROPOSED','ACCEPTED','PARTIALLY_ACCEPTED',"
            "'REJECTED','EXPIRED','FULFILLED','PARTIALLY_FULFILLED','UNFULFILLED','CANCELLED')",
            name="recurring_need_occurrences_status_chk",
        ),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    recurring_need_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("marketplace.recurring_needs.id", ondelete="CASCADE"),
        nullable=False,
    )
    # `DateTime`, pas `Date` : aucune table du schéma marketplace n'utilise le type SQL DATE
    # (harvest_date, delivery_deadline, etc. sont tous des DateTime) — pas de type inédit pour ce seul champ.
    occurrence_date = Column(DateTime, nullable=False)
    requested_quantity = Column(Numeric(14, 3), nullable=False)
    unit = Column(Text, nullable=False)
    status = Column(Text, default="OPEN", nullable=False, server_default=text("'OPEN'"))
    quantity_matched = Column(Numeric(14, 3), default=0, nullable=False, server_default=text("'0'"))
    quantity_confirmed = Column(Numeric(14, 3), default=0, nullable=False, server_default=text("'0'"))
    quantity_delivered = Column(Numeric(14, 3), default=0, nullable=False, server_default=text("'0'"))
    version = Column(Integer, default=1, nullable=False, server_default=text("1"))
    notified_at = Column(DateTime, nullable=True)
    accepted_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    # Corrélation vers la/les commandes issues de cette occurrence — même convention que
    # `orders.checkout_group_id` (uuid libre, volontairement SANS FK : ce n'est pas une entité).
    order_group_id = Column(PG_UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    recurring_need = relationship("RecurringNeed", back_populates="occurrences")
    allocations = relationship("NeedAllocation", back_populates="occurrence", cascade="all, delete-orphan")


class NeedAllocation(Base):
    """Partie d'une occurrence couverte par UN fournisseur — ligne PostgreSQL réelle (FK + CHECK),
    jamais un blob JSON, car elle participe ensuite à la consommation de stock, aux commandes, et
    potentiellement aux paiements.

    Phase 1 : la source de stock est exclusivement `products.quantity_for_sale` (catalogue vivant).
    `market_offer_id` (prévente de récolte future) est volontairement absent — ajoutable plus tard par
    migration additive, sans toucher à cette table.
    """

    __tablename__ = "need_allocations"
    __table_args__ = (
        Index(
            "need_allocations_occurrence_producer_product_uq",
            "occurrence_id",
            "producer_id",
            "product_id",
            unique=True,
        ),
        Index("need_allocations_occurrence_idx", "occurrence_id"),
        Index("need_allocations_producer_idx", "producer_id"),
        Index("need_allocations_product_idx", "product_id"),
        Index("need_allocations_order_item_idx", "order_item_id"),
        CheckConstraint("quantity > 0", name="need_allocations_quantity_chk"),
        CheckConstraint("unit_price >= 0", name="need_allocations_unit_price_chk"),
        CheckConstraint(
            "status IN ('PROPOSED','ACCEPTED','REJECTED','EXPIRED','CONVERTED')",
            name="need_allocations_status_chk",
        ),
        {"schema": "marketplace"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid4, server_default=text("gen_random_uuid()"))
    occurrence_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("marketplace.recurring_need_occurrences.id", ondelete="CASCADE"),
        nullable=False,
    )
    producer_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.producers.id", ondelete="RESTRICT"), nullable=False
    )
    product_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.products.id", ondelete="RESTRICT"), nullable=False
    )
    quantity = Column(Numeric(14, 3), nullable=False)
    unit_price = Column(Numeric(12, 2), nullable=False)
    unit = Column(Text, nullable=False)
    status = Column(Text, default="PROPOSED", nullable=False, server_default=text("'PROPOSED'"))
    # NULL jusqu'à la conversion en commande (hors scope Phase 1) ; SET NULL si la ligne de commande
    # disparaissait un jour — ne bloque jamais la suppression d'un OrderItem pour une ligne de traçabilité.
    order_item_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("marketplace.order_items.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    occurrence = relationship("RecurringNeedOccurrence", back_populates="allocations")


__all__ = ["RecurringNeed", "RecurringNeedOccurrence", "NeedAllocation"]
