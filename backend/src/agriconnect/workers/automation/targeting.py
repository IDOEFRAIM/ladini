"""Ciblage — qui solliciter, par (sous-catégorie × zone).

Requêtes SQL pures et bornées. On renvoie des dicts plats (pas d'entités ORM
détachées) pour rester simple à sérialiser et à tester.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agriconnect.domain.models import (
    BuyerProfile,
    Producer,
    Product,
    User,
)


async def producers_for_auction(
    session: AsyncSession,
    *,
    sub_category_id: Any,
    target_zone_id: Optional[Any],
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Producteurs pertinents pour une enchère : même sous-catégorie, même zone.

    Critère : le producteur propose (au catalogue) un produit de la
    sous-catégorie visée. Si une zone cible est fournie, on la filtre ; sinon on
    ne restreint que par catégorie. Un producteur n'apparaît qu'une fois.
    """
    if not sub_category_id:
        return []

    stmt = (
        select(
            Producer.id.label("producer_id"),
            Producer.user_id.label("user_id"),
            User.phone.label("phone"),
            Producer.business_name.label("business_name"),
            User.name.label("user_name"),
        )
        .join(User, User.id == Producer.user_id)
        .join(Product, Product.producer_id == Producer.id)
        .where(
            Product.sub_category_id == sub_category_id,
            Product.is_available.is_(True),
            User.phone.isnot(None),
        )
        .distinct()
        .limit(limit)
    )
    if target_zone_id is not None:
        stmt = stmt.where(Producer.zone_id == target_zone_id)

    rows = (await session.execute(stmt)).mappings().all()
    return [
        {
            "producer_id": r["producer_id"],
            "user_id": r["user_id"],
            "phone": r["phone"],
            "display_name": r["business_name"] or r["user_name"] or "Producteur",
        }
        for r in rows
    ]


async def buyers_in_zone_for_category(
    session: AsyncSession,
    *,
    zone_id: Optional[Any],
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Acheteurs locaux à notifier d'un nouveau produit (matching de proximité).

    Critère de proximité : acheteurs dont l'utilisateur est rattaché à la zone du
    produit. (Le raffinement par historique d'achat/catégorie viendra ensuite.)
    """
    if zone_id is None:
        return []

    stmt = (
        select(
            BuyerProfile.id.label("buyer_id"),
            BuyerProfile.user_id.label("user_id"),
            User.phone.label("phone"),
            User.name.label("user_name"),
        )
        .join(User, User.id == BuyerProfile.user_id)
        .where(
            User.zone_id == zone_id,
            User.phone.isnot(None),
        )
        .distinct()
        .limit(limit)
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [
        {
            "buyer_id": r["buyer_id"],
            "user_id": r["user_id"],
            "phone": r["phone"],
            "display_name": r["user_name"] or "Client",
        }
        for r in rows
    ]
