"""DTO identité & réputation : User, Producer, BuyerProfile, TrustScore, contexte."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from agriconnect.domain.base_model import BaseMarketplaceModel


class UserModel(BaseMarketplaceModel):
    """↔ auth.users"""

    id: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    role: str = "USER"
    zone_id: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    identity_verified: bool = False
    whatsapp_enabled: bool = True
    onboarding_completed: bool = False


class ProducerModel(BaseMarketplaceModel):
    """↔ marketplace.producers"""

    id: Optional[str] = None
    user_id: str
    business_name: Optional[str] = None
    status: str = "PENDING"
    is_certified: bool = False
    zone_id: Optional[str] = None
    region: Optional[str] = None
    province: Optional[str] = None
    commune: Optional[str] = None
    phone_number: Optional[str] = None
    rating: Optional[int] = None
    reviews_count: int = 0


class BuyerProfileModel(BaseMarketplaceModel):
    """↔ marketplace.buyer_profiles"""

    id: Optional[str] = None
    user_id: str
    buyer_type_id: Optional[str] = None
    establishment_name: Optional[str] = None
    default_delivery_address: Optional[str] = None
    is_verified: bool = False
    trust_badge: Optional[str] = None
    rating: Optional[float] = None
    reviews_count: int = 0


class TrustScoreModel(BaseMarketplaceModel):
    """↔ intelligence.trust_scores"""

    id: Optional[str] = None
    user_id: str
    global_score: float = 0.0
    reliability_index: float = 0.0
    quality_index: float = 0.0
    compliance_index: float = 0.0
    resilience_bonus: float = 0.0


class UserContextModel(BaseMarketplaceModel):
    """Vue agrégée résolue par UserContextService (identité + profils + droits)."""

    id: str
    name: str
    phone: str
    role: str = "USER"
    zone_id: Optional[str] = None
    zone_name: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    producer_id: Optional[str] = None
    buyer_id: Optional[str] = None
    delivery_agent_id: Optional[str] = None
    can_sell: bool = False
    can_buy: bool = False
    can_deliver: bool = False
    is_admin: bool = False
    identity_verified: bool = False
    onboarding_completed: bool = False
