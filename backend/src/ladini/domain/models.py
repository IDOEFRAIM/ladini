"""Agrégateur ORM — préserve `from ladini.domain.models import X` partout.

Le fichier original (1215 lignes, 49 classes en un bloc) a été scindé par
domaine métier dans des modules dédiés :

    orm_base.py           Base déclarative partagée, `_uuid4`
    identity/models.py     User, Account, Session, Producer, Client, BuyerType,
                          BuyerProfile, DeliveryAgent, TrustScore
    catalog/models.py      Warehouse, Farm, MarketOffer, Stock, StockMovement,
                          Batch, Expense, Product
    orders/models.py       Delivery, Order, OrderItem, Payment,
                          OrderStatusHistory, OrderReminder, OrderDispute,
                          Auction, Bid, MarketplaceRating
    governance/models.py   Organization, Zone, Category, StandardPrice...
    intelligence/models.py AuditLog, Conversation, AgentContextMemory...

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

from ladini.domain.catalog.models import (
    Batch,
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
    OverlayLayer,
    ProhibitedTerm,
    RoleDefinition,
    StandardPrice,
    SubCategory,
    UserOrganization,
    WorkZone,
    Zone,
    ZoneMetric,
    ZoneSetting,
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
from ladini.domain.intelligence.models import (
    AgentAction,
    AgentContextMemory,
    AIRatingReasoning,
    AuditLog,
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
    MarketplaceRating,
    Order,
    OrderDispute,
    OrderItem,
    OrderReminder,
    OrderStatusHistory,
    Payment,
)
from ladini.domain.orm_base import Base, _uuid4

__all__ = [
    "Base",
    "_uuid4",
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
    "WorkZone",
    "ZoneMetric",
    "Category",
    "SubCategory",
    "StandardPrice",
    "ZoneSetting",
    "OverlayLayer",
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
    "Batch",
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
    "MarketplaceRating",
    # intelligence
    "AuditLog",
    "AgentAction",
    "Conversation",
    "AgentContextMemory",
    "TrustScore",
    "AIRatingReasoning",
    "ModerationEvent",
    "DemandSignal",
    "Solicitation",
    "NotificationOutbox",
]
