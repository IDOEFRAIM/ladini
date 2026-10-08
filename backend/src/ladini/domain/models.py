"""Agrégateur ORM — préserve `from ladini.domain.models import X` partout.

Le fichier original (1215 lignes, 49 classes en un bloc) a été scindé par
domaine métier dans des modules dédiés :

    orm_base.py           Base déclarative partagée, `_uuid4`
    identity/models.py     User, Account, Session, Producer, Client, BuyerType,
                          BuyerProfile, DeliveryAgent, TrustScore
    catalog/models.py      Warehouse, Farm, MarketOffer, Stock, StockMovement,
                          Expense, Product
    orders/models.py       Delivery, Order, OrderItem, Payment,
                          OrderStatusHistory, OrderReminder, OrderDispute,
                          Auction, Bid
    governance/models.py   Organization, Zone, Category, StandardPrice...
    intelligence/models.py AuditLog, Conversation...

Toutes les classes partagent le MÊME `Base` (orm_base.py) — un seul registre
de mappers, les `relationship("NomClasse", ...)` inter-fichiers se résolvent
normalement (résolution paresseuse par nom, indépendante du fichier source).

Ce fichier ne DÉFINIT plus rien : il ré-exporte, pour que les imports
existants (`from ladini.domain.models import Order`) continuent de
fonctionner sans modification. Nouveau code : importer directement depuis le
module de contexte concerné (`from ladini.domain.orders.models import
Order`) — plus précis, et le fichier qu'on ouvre correspond à ce qu'on lit.
"""

from __future__ import annotations

from ladini.domain import (
    runtime_tables,  # noqa: F401  (tables d'état runtime, miroir de Drizzle)
)
from ladini.domain.analytics.models import (
    BusinessEventRecord,
    BuyerDailyMetricRecord,
    DirectDailyMetricRecord,
    EventOutboxRecord,
    MetricTargetRecord,
    RecurringDailyMetricRecord,
    TenderDailyMetricRecord,
)
from ladini.domain.catalog.models import (
    CropCycle,
    Expense,
    Farm,
    MarketOffer,
    Product,
    Stock,
    StockMovement,
    Warehouse,
)
from ladini.domain.governance.models import (
    Category,
    ClimaticRegion,
    Organization,
    PlatformSetting,
    ProhibitedTerm,
    RoleDefinition,
    StandardPrice,
    SubCategory,
    UserOrganization,
    WorkZone,
    Zone,
)
from ladini.domain.identity.models import (
    Account,
    BuyerProfile,
    BuyerType,
    Client,
    DeliveryAgent,
    Producer,
    Session,
    TrustScore,
    User,
)
from ladini.domain.intelligence.campaign_models import (
    AvailabilityCampaign,
    AvailabilityCampaignRecipient,
    AvailabilityInterest,
    CommunicationConsent,
)
from ladini.domain.intelligence.models import (
    AgentAction,
    AuditLog,
    CommercialFollowup,
    Conversation,
    DemandSignal,
    ModerationEvent,
    NotificationOutbox,
    Solicitation,
)
from ladini.domain.orders.models import (
    Auction,
    Bid,
    Delivery,
    Order,
    OrderDispute,
    OrderItem,
    OrderReminder,
    OrderStatusHistory,
    Payment,
)
from ladini.domain.orm_base import Base, _uuid4
from ladini.domain.recurring_supply.models import (
    NeedAllocation,
    RecurringNeed,
    RecurringNeedOccurrence,
)
from ladini.domain.telemetry import AgentLlmCall, AgentToolCall, AgentTurn

__all__ = [
    "AgentLlmCall",
    "AgentToolCall",
    "AgentTurn",
    "Base",
    "_uuid4",
    # analytics (Phase C)
    "EventOutboxRecord",
    "BusinessEventRecord",
    "MetricTargetRecord",
    # analytics (Phase D)
    "BuyerDailyMetricRecord",
    "DirectDailyMetricRecord",
    "TenderDailyMetricRecord",
    "RecurringDailyMetricRecord",
    # approvisionnement récurrent (Phase 1 — fondation de données)
    "RecurringNeed",
    "RecurringNeedOccurrence",
    "NeedAllocation",
    # auth
    "User",
    "Account",
    "Session",
    # governance
    "Organization",
    "UserOrganization",
    "RoleDefinition",
    "ClimaticRegion",
    "Zone",
    "PlatformSetting",
    "AvailabilityCampaign",
    "AvailabilityCampaignRecipient",
    "AvailabilityInterest",
    "CommunicationConsent",
    "WorkZone",
    "Category",
    "SubCategory",
    "StandardPrice",
    "ProhibitedTerm",
    # marketplace
    "Warehouse",
    "Producer",
    "Client",
    "BuyerType",
    "BuyerProfile",
    "DeliveryAgent",
    "Delivery",
    "Farm",
    "MarketOffer",
    "CropCycle",
    "Stock",
    "StockMovement",
    "Expense",
    "Product",
    "Order",
    "OrderItem",
    "Payment",
    "OrderStatusHistory",
    "OrderReminder",
    "OrderDispute",
    "Auction",
    "Bid",
    # intelligence
    "AuditLog",
    "AgentAction",
    "Conversation",
    "TrustScore",
    "ModerationEvent",
    "DemandSignal",
    "Solicitation",
    "NotificationOutbox",
    "CommercialFollowup",
]
