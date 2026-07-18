"""UserContextService — identité, profils, mémoire agent et réputation.

Remplace l'ancien BaseMixin polymorphe : une seule requête résout téléphone →
(User, Producer, BuyerProfile, DeliveryAgent, Zone) et produit un UserContextModel.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agriconnect.domain.models import (
    User,
    Producer,
    BuyerProfile,
    DeliveryAgent,
    Zone,
    TrustScore,
    AgentContextMemory,
)
from agriconnect.domain.identity.dto import UserContextDTO as UserContextModel, TrustScoreDTO as TrustScoreModel
from agriconnect.services.database.base_service import BaseService, transactional
from agriconnect.services.database.common import normalize_phone


class UserContextService(BaseService):

    # ── Résolution d'identité (une requête, jointure polymorphe) ────────────
    @transactional(write=False)
    async def resolve_by_phone(
        self, session: AsyncSession, phone: str
    ) -> Optional[UserContextModel]:
        clean = normalize_phone(phone)
        if not clean:
            return None
        stmt = (
            select(User, Producer, BuyerProfile, DeliveryAgent, Zone)
            .outerjoin(Producer, Producer.user_id == User.id)
            .outerjoin(BuyerProfile, BuyerProfile.user_id == User.id)
            .outerjoin(DeliveryAgent, DeliveryAgent.user_id == User.id)
            .outerjoin(Zone, Zone.id == User.zone_id)
            .where(User.phone == clean)
        )
        row = (await session.execute(stmt)).first()
        if not row:
            return None
        user, producer, buyer, delivery, zone = row
        is_admin = (user.role == "ADMIN")
        return UserContextModel(
            id=str(user.id),
            name=user.name or f"User_{clean[-4:]}",
            phone=user.phone or clean,
            role=user.role or "USER",
            zone_id=str(zone.id) if zone else None,
            zone_name=zone.name if zone else None,
            latitude=user.latitude,
            longitude=user.longitude,
            producer_id=str(producer.id) if producer else None,
            buyer_id=str(buyer.id) if buyer else None,
            delivery_agent_id=str(delivery.id) if delivery else None,
            can_sell=bool(producer) or is_admin,
            can_buy=bool(buyer) or is_admin,
            can_deliver=bool(delivery) or is_admin,
            is_admin=is_admin,
            identity_verified=bool(user.identity_verified),
            onboarding_completed=bool(user.onboarding_completed),
        )

    # ── Réputation ──────────────────────────────────────────────────────────
    @transactional(write=False)
    async def get_trust_score(self, session: AsyncSession, user_id: str) -> Optional[TrustScoreModel]:
        stmt = select(TrustScore).where(TrustScore.user_id == user_id)
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return TrustScoreModel.model_validate(obj) if obj else None

    # ── Mémoire agent (upsert idempotent sur (user_id, context_key)) ────────
    @transactional(write=True)
    async def set_memory(
        self, session: AsyncSession, user_id: str, key: str, value: Any,
        *, source: str = "AGENT", market_offer_id: Optional[str] = None,
    ) -> None:
        stmt = (
            pg_insert(AgentContextMemory.__table__)
            .values(
                user_id=user_id, context_key=key, context_value=value,
                source=source, market_offer_id=market_offer_id,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "context_key"],
                set_={"context_value": value, "source": source},
            )
        )
        await session.execute(stmt)

    @transactional(write=False)
    async def get_memory(self, session: AsyncSession, user_id: str, key: str) -> Any:
        stmt = (
            select(AgentContextMemory.context_value)
            .where(AgentContextMemory.user_id == user_id)
            .where(AgentContextMemory.context_key == key)
        )
        return (await session.execute(stmt)).scalar_one_or_none()
