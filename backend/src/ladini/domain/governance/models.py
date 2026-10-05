"""ORM Governance — organizations/zones/categories/prix standards/termes interdits.

Extrait de l'ancien `domain/models.py` (voir `orm_base.py` pour la justification
du split — même `Base` partagé, zéro changement de schéma/colonnes/index).
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
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from ladini.domain.orm_base import Base


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = {"schema": "governance"}

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name = Column(Text, nullable=False)
    type = Column(Text, nullable=False)
    tax_id = Column(Text, unique=True)
    description = Column(Text)
    status = Column(Text, default="PENDING", nullable=False, server_default=text("'PENDING'"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class UserOrganization(Base):
    __tablename__ = "user_organizations"
    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="user_org_unique"),
        Index("user_org_org_idx", "organization_id"),
        Index("user_org_role_idx", "role_id"),
        Index("user_org_user_idx", "user_id"),
        {"schema": "governance"},
    )

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="CASCADE"), nullable=False)
    organization_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id", ondelete="CASCADE"), nullable=False
    )
    role = Column(Text, default="FIELD_AGENT", nullable=False, server_default=text("'FIELD_AGENT'"))
    role_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("governance.role_definitions.id", ondelete="SET NULL")
    )
    managed_zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id", ondelete="SET NULL"))


class RoleDefinition(Base):
    __tablename__ = "role_definitions"
    __table_args__ = {"schema": "governance"}

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name = Column(Text, unique=True, nullable=False)
    description = Column(Text)
    permissions = Column(
        PG_ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ClimaticRegion(Base):
    __tablename__ = "climatic_regions"
    __table_args__ = {"schema": "governance"}

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name = Column(Text, unique=True, nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Zone(Base):
    __tablename__ = "zones"
    __table_args__ = (
        Index("zones_region_idx", "climatic_region_id"),
        Index("zones_org_idx", "organization_id"),
        Index("zones_active_idx", "is_active"),
        Index("zones_parent_idx", "parent_id"),
        Index("zones_path_idx", "path"),
        Index("ix_zones_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}),
        {"schema": "governance"},
    )

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name = Column(Text, unique=True, nullable=False)
    code = Column(Text, unique=True, nullable=False)
    climatic_region_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("governance.climatic_regions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    organization_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id", ondelete="SET NULL")
    )
    parent_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id", ondelete="RESTRICT"))
    path = Column(Text)
    depth = Column(Integer, default=0, nullable=False, server_default=text("0"))
    latitude = Column(Float)
    longitude = Column(Float)
    is_active = Column(Boolean, default=True, nullable=False, server_default=text("true"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkZone(Base):
    __tablename__ = "work_zones"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "zone_id", name="work_zones_org_zone_unique"
        ),
        Index("work_zones_org_idx", "organization_id"),
        Index("work_zones_zone_idx", "zone_id"),
        {"schema": "governance"},
    )

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    organization_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id", ondelete="CASCADE"), nullable=False
    )
    zone_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("governance.zones.id", ondelete="CASCADE"), nullable=False
    )
    manager_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="SET NULL"))
    role = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )




class Category(Base):
    __tablename__ = "categories"
    __table_args__ = {"schema": "governance"}

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name = Column(Text, unique=True, nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SubCategory(Base):
    __tablename__ = "sub_categories"
    __table_args__ = (
        UniqueConstraint("category_id", "name", name="sub_categories_cat_name_unique"),
        Index("ix_subcategories_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}),
        {"schema": "governance"},
    )

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    category_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("governance.categories.id", ondelete="RESTRICT"), nullable=False
    )
    name = Column(Text, nullable=False)
    blocked_zone_ids = Column(
        PG_ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    # Politique plateforme (2026-09-02, demande explicite utilisateur) : quantité
    # minimale, en unité de BASE, qu'une commande de ce TYPE de produit doit
    # représenter pour être poursuivie — ex. "Tomates" -> 50 KG. Définie par
    # l'ADMIN au niveau du type de produit, jamais par le producteur (voir
    # `services/database/producer.py` — aucun payload de création/mise à jour
    # de `Product` ne touche `SubCategory`). NULL = aucune règle configurée =
    # comportement historique (tout produit historique reste commandable).
    # Sans rapport avec `PricingTier.min_order_quantity`
    # (domain/pricing_tiers.py) — celui-ci est un minimum de NOMBRE DE
    # PAQUETS pour UN palier tarifaire précis, jamais une quantité de base ni
    # une politique de plateforme. Source unique consommée par
    # `domain/order_policy.py::validate_minimum_order_quantity` — le web
    # (Next.js/drizzle, table miroir `governance.sub_categories`) et l'agent
    # lisent tous deux CES colonnes, jamais une copie locale.
    minimum_order_quantity = Column(Numeric(14, 3), nullable=True)
    minimum_order_unit = Column(Text, nullable=True)
    # Configuration des unités par type de produit (miroir de
    # `governance.sub_categories` côté Drizzle, migration 0004) : nullable, NULL =
    # aucune configuration = comportement historique.
    priority_unit = Column(Text, nullable=True)
    allowed_units = Column(PG_ARRAY(Text), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class StandardPrice(Base):
    __tablename__ = "standard_prices"
    __table_args__ = (
        UniqueConstraint(
            "sub_category_id", "zone_id", name="standard_prices_sub_zone_unique"
        ),
        Index("standard_prices_zone_idx", "zone_id"),
        {"schema": "governance"},
    )

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    sub_category_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("governance.sub_categories.id", ondelete="RESTRICT"),
        nullable=False,
    )
    zone_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("governance.zones.id", ondelete="RESTRICT"), nullable=False
    )
    price_per_unit = Column(Float, nullable=False)
    unit = Column(Text, default="KG", nullable=False, server_default=text("'KG'"))
    updated_by_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )




class ProhibitedTerm(Base):
    """Liste noire des produits interdits (drogue, armes…) gérée par les admins."""

    __tablename__ = "prohibited_terms"
    __table_args__ = (
        Index("prohibited_terms_active_idx", "is_active"),
        {"schema": "governance"},
    )

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    term = Column(Text, unique=True, nullable=False)
    category = Column(
        Text, default="ILLICIT", nullable=False, server_default=text("'ILLICIT'")
    )
    severity = Column(
        Text, default="HIGH", nullable=False, server_default=text("'HIGH'")
    )
    is_active = Column(
        Boolean, default=True, nullable=False, server_default=text("true")
    )
    created_at = Column(DateTime, server_default=func.now(), nullable=False)




class PlatformSetting(Base):
    """Réglage GLOBAL de la plateforme, éditable par un admin (clé -> valeur JSON versionnée).

    Première table de configuration administrable du dépôt (le reste vit dans `core/settings.py`, donc
    dans l'environnement de déploiement). Sert d'abord `recurring_supply.minimum_start_lead_days`
    (`services/platform_settings.py`). Chaque modification est auditée dans `intelligence.audit_logs`.
    Table Drizzle-authored (mirror, voir schema_contract/migrations/0015).
    """

    __tablename__ = "platform_settings"
    __table_args__ = (
        Index("platform_settings_updated_by_idx", "updated_by_id"),
        CheckConstraint("version >= 1", name="platform_settings_version_chk"),
        {"schema": "governance"},
    )

    key = Column(Text, primary_key=True)
    value = Column(JSONB, nullable=False)
    version = Column(Integer, default=1, nullable=False, server_default=text("1"))
    updated_by_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id", ondelete="SET NULL"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


__all__ = [
    "Organization",
    "UserOrganization",
    "RoleDefinition",
    "ClimaticRegion",
    "Zone",
    "WorkZone",
    "Category",
    "SubCategory",
    "StandardPrice",
    "ProhibitedTerm",
    "PlatformSetting",
]
