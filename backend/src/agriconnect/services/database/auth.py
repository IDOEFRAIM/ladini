from typing import Any, Dict, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .common import _uuid, logger, User, Producer


class AuthMixin:
    async def get_user_by_phone(self, session: AsyncSession, phone: str) -> Optional[Dict[str, Any]]:
        # Select explicit, minimal columns to remain compatible with Prisma-managed users table
        stmt = select(User.id, User.name, User.phone, User.zone_id, User.role).where(User.phone == phone)
        result = await session.execute(stmt)
        row = result.first()
        if not row:
            return None
        data = {
            "id": row[0],
            "name": row[1],
            "phone": row[2],
            "zone_id": row[3],
            "role": row[4],
        }
        prod_stmt = select(Producer).where(Producer.user_id == data["id"])
        prod_result = await session.execute(prod_stmt)
        producer = prod_result.scalar_one_or_none()
        data["producer_id"] = producer.id if producer else None
        data["is_certified"] = getattr(producer, "is_certified", False) if producer else False
        return data

    async def get_user_by_id(self, session: AsyncSession, user_id: str) -> Optional[Dict[str, Any]]:
        stmt = select(User.id, User.name, User.phone, User.zone_id, User.role).where(User.id == user_id)
        result = await session.execute(stmt)
        row = result.first()
        if not row:
            return None
        return {"id": row[0], "name": row[1], "phone": row[2], "zone_id": row[3], "role": row[4]}

    async def identify_or_create_user(
        self, session: AsyncSession, phone: str, name: str = None, zone_id: str = None
    ) -> Dict[str, Any]:
        # call the mixin implementation directly on the class to avoid dispatching
        # to the AgriDatabaseService wrapper which expects different signature
        existing = await AuthMixin.get_user_by_phone(self, session, phone)
        if existing:
            existing["is_new"] = False
            return existing

        user_id = _uuid()
        user = User(
            id=user_id, phone=phone, name=name or phone,
            zone_id=zone_id, role="PRODUCER",
        )
        session.add(user)

        producer_id = _uuid()
        producer = Producer(
            id=producer_id, user_id=user_id,
            zone_id=zone_id, status="ACTIVE",
        )
        session.add(producer)
        await session.flush()

        logger.info("Nouveau User+Producer créé: %s (%s)", phone, user_id)
        return {
            "id": user_id, "phone": phone, "name": name or phone,
            "zone_id": zone_id, "producer_id": producer_id,
            "role": "PRODUCER", "is_new": True, "is_certified": False,
        }
