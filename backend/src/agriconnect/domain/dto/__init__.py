"""DTO Pydantic v2 alignés 1-pour-1 sur le schéma marketplace (source de vérité)."""

from agriconnect.domain.base_model import BaseMarketplaceModel
from agriconnect.domain.dto.catalog import FarmModel, MarketOfferModel, ProductModel
from agriconnect.domain.dto.orders import (
    DeliveryModel,
    OrderItemModel,
    OrderModel,
    OrderReminderModel,
    OrderStatusHistoryModel,
    PaymentModel,
)
from agriconnect.domain.dto.identity import (
    BuyerProfileModel,
    ProducerModel,
    TrustScoreModel,
    UserContextModel,
    UserModel,
)

__all__ = [
    "BaseMarketplaceModel",
    "FarmModel",
    "MarketOfferModel",
    "ProductModel",
    "OrderItemModel",
    "OrderModel",
    "PaymentModel",
    "OrderStatusHistoryModel",
    "OrderReminderModel",
    "DeliveryModel",
    "UserModel",
    "ProducerModel",
    "BuyerProfileModel",
    "TrustScoreModel",
    "UserContextModel",
]
