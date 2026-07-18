"""ORM Governance — organizations/zones/categories/prix standards/termes interdits.

Extrait de l'ancien `domain/models.py` (voir `orm_base.py` pour la justification
du split — même `Base` partagé, zéro changement de schéma/colonnes/index).
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, ARRAY as PG_ARRAY

from agriconnect.domain.orm_base import Base


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = {"schema": "governance"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)
    tax_id = Column(String, unique=True)
    description = Column(Text)
    status = Column(String, default="PENDING", nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class UserOrganization(Base):
    __tablename__ = "user_organizations"
    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="user_org_unique"),
        {"schema": "governance"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False)
    organization_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id"), nullable=False)
    role = Column(String, default="FIELD_AGENT", nullable=False)
    role_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.role_definitions.id"))
    managed_zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))


class RoleDefinition(Base):
    __tablename__ = "role_definitions"
    __table_args__ = {"schema": "governance"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(String, unique=True, nullable=False)
    description = Column(Text)
    permissions = Column(PG_ARRAY(String), nullable=False, server_default=text("'{}'::text[]"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ClimaticRegion(Base):
    __tablename__ = "climatic_regions"
    __table_args__ = {"schema": "governance"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(String, unique=True, nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class Zone(Base):
    __tablename__ = "zones"
    __table_args__ = (
        Index("zones_region_idx", "climatic_region_id"),
        Index("zones_org_idx", "organization_id"),
        Index("zones_active_idx", "is_active"),
        Index("zones_parent_idx", "parent_id"),
        Index("zones_path_idx", "path"),
        {"schema": "governance"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(String, unique=True, nullable=False)
    code = Column(String, unique=True, nullable=False)
    climatic_region_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.climatic_regions.id"), nullable=False)
    organization_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id"))
    parent_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"))
    path = Column(String)
    depth = Column(Integer, default=0, nullable=False)
    latitude = Column(Float)
    longitude = Column(Float)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class WorkZone(Base):
    __tablename__ = "work_zones"
    __table_args__ = (
        UniqueConstraint("organization_id", "zone_id", name="work_zones_org_zone_unique"),
        Index("work_zones_org_idx", "organization_id"),
        Index("work_zones_zone_idx", "zone_id"),
        {"schema": "governance"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    organization_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.organizations.id"), nullable=False)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"), nullable=False)
    manager_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"))
    role = Column(String)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class ZoneMetric(Base):
    __tablename__ = "zone_metrics"
    __table_args__ = (
        Index("zone_metrics_composite_idx", "zone_id", "date", "metric_name"),
        {"schema": "governance"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"), nullable=False)
    date = Column(DateTime, server_default=func.now(), nullable=False)
    metric_name = Column(String, nullable=False)
    value = Column(Float, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = {"schema": "governance"}

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    name = Column(String, unique=True, nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class SubCategory(Base):
    __tablename__ = "sub_categories"
    __table_args__ = (
        UniqueConstraint("category_id", "name", name="sub_categories_cat_name_unique"),
        {"schema": "governance"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    category_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.categories.id"), nullable=False)
    name = Column(String, nullable=False)
    blocked_zone_ids = Column(PG_ARRAY(String), nullable=False, server_default=text("'{}'::text[]"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class StandardPrice(Base):
    __tablename__ = "standard_prices"
    __table_args__ = (
        UniqueConstraint("sub_category_id", "zone_id", name="standard_prices_sub_zone_unique"),
        Index("standard_prices_zone_idx", "zone_id"),
        {"schema": "governance"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    sub_category_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.sub_categories.id"), nullable=False)
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"), nullable=False)
    price_per_unit = Column(Float, nullable=False)
    unit = Column(String, default="KG", nullable=False)
    updated_by_id = Column(PG_UUID(as_uuid=True), ForeignKey("auth.users.id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class ZoneSetting(Base):
    __tablename__ = "zone_settings"
    __table_args__ = (
        UniqueConstraint("zone_id", "key", name="zone_settings_zone_key_unique"),
        Index("zone_settings_zone_idx", "zone_id"),
        {"schema": "governance"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"), nullable=False)
    key = Column(String, nullable=False)
    value = Column(JSONB, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class ProhibitedTerm(Base):
    """Liste noire des produits interdits (drogue, armes…) gérée par les admins."""

    __tablename__ = "prohibited_terms"
    __table_args__ = (
        Index("prohibited_terms_active_idx", "is_active"),
        {"schema": "governance"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    term = Column(String, unique=True, nullable=False)
    category = Column(String, default="ILLICIT", nullable=False, server_default=text("'ILLICIT'"))
    severity = Column(String, default="HIGH", nullable=False, server_default=text("'HIGH'"))
    is_active = Column(Boolean, default=True, nullable=False, server_default=text("true"))
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class OverlayLayer(Base):
    __tablename__ = "overlay_layers"
    __table_args__ = (
        UniqueConstraint("zone_id", "key", name="overlay_layers_zone_key_unique"),
        Index("overlay_layers_zone_idx", "zone_id"),
        {"schema": "governance"},
    )

    id = Column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    zone_id = Column(PG_UUID(as_uuid=True), ForeignKey("governance.zones.id"), nullable=False)
    key = Column(String, nullable=False)
    label = Column(String, nullable=False)
    enabled = Column(Boolean, default=False, nullable=False)
    settings = Column(JSONB)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


__all__ = [
    "Organization",
    "UserOrganization",
    "RoleDefinition",
    "ClimaticRegion",
    "Zone",
    "WorkZone",
    "ZoneMetric",
    "Category",
    "SubCategory",
    "StandardPrice",
    "ZoneSetting",
    "ProhibitedTerm",
    "OverlayLayer",
]
