import logging
import uuid

from agriconnect.core.database import get_db
from agriconnect.services.models_v3 import (
    User, Producer, Farm, Stock, StockMovement, Product,
    Order, OrderItem, Client, Expense, CropCycle,
    Warehouse, Batch,
    Zone, ClimaticRegion, Organization, Category, SubCategory,
    StandardPrice, WorkZone, ZoneMetric, ZoneSetting,
    AgentAction, Conversation, AuditLog, TrustScore,
    AIRatingReasoning, Anomaly, TerritoryEvent, AgentTelemetry,
    Auction, Bid, ExternalContext,
    TransactionStaging,
)

logger = logging.getLogger("service.database_v3")


def _uuid() -> str:
    return str(uuid.uuid4())
