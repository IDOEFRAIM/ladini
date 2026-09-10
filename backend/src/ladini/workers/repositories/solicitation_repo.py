"""Repository ``solicitations`` — état métier + idempotence des sollicitations.

L'idempotence repose sur les index uniques ``(auction_id, target_producer_id)``
et ``(market_offer_id, target_buyer_id)`` : l'upsert ``ON CONFLICT DO NOTHING``
ne réinsère jamais une sollicitation déjà émise, et ne retourne QUE les lignes
réellement créées → seules celles-ci génèrent un message dans l'outbox.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Sequence

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ladini.domain.models import Auction, Solicitation

# Enchères éligibles à la sollicitation : ouvertes et non expirées.
_OPEN_STATUSES = ("OPEN",)


async def fetch_auctions_to_solicit(
    session: AsyncSession, *, limit: int = 100
) -> List[Auction]:
    """Récupère un lot borné d'enchères ouvertes à solliciter.

    Rejouable : les producteurs déjà sollicités sont écartés par l'upsert. On
    ordonne par ``created_at`` décroissant pour prioriser les plus récentes.
    """
    now = datetime.utcnow()
    stmt = (
        select(Auction)
        .where(
            Auction.status.in_(_OPEN_STATUSES),
            Auction.deadline > now,
        )
        .order_by(Auction.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def upsert_auction_solicitations(
    session: AsyncSession,
    *,
    auction: Auction,
    producers: Sequence[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Insère les sollicitations manquantes pour une enchère (idempotent).

    Retourne ``{"created": [...], "skipped": [...]}``. Chaque producteur
    écarté porte une raison explicite (``missing_producer_id`` ou
    ``already_solicited``) — sans ça, un lot ``producers_targeted=2,
    solicitations_created=0`` est indistinguable entre "tout le monde était
    déjà sollicité" et "les données producteur sont corrompues" (incident du
    2026-08-24 : le rapport agrégé seul ne permettait pas de trancher).
    """
    skipped: List[Dict[str, Any]] = []
    if not producers:
        return {"created": [], "skipped": skipped}

    rows: List[Dict[str, Any]] = []
    for p in producers:
        producer_id = p.get("producer_id")
        if not producer_id:
            skipped.append({"producer_id": None, "reason": "missing_producer_id"})
            continue
        rows.append(
            {
                "kind": "AUCTION_INVITE",
                "auction_id": auction.id,
                "target_producer_id": producer_id,
                "sub_category_id": auction.sub_category_id,
                "zone_id": auction.target_zone_id,
                "status": "PENDING",
            }
        )
    if not rows:
        return {"created": [], "skipped": skipped}

    stmt = (
        pg_insert(Solicitation)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["auction_id", "target_producer_id"])
        .returning(Solicitation.id, Solicitation.target_producer_id)
    )
    result = await session.execute(stmt)
    created = [
        {"solicitation_id": r_id, "producer_id": prod_id}
        for r_id, prod_id in result.all()
    ]
    created_ids = {c["producer_id"] for c in created}
    for row in rows:
        producer_id = row["target_producer_id"]
        if producer_id not in created_ids:
            # ON CONFLICT DO NOTHING n'a rien inséré pour ce producteur :
            # une sollicitation (auction_id, target_producer_id) existe déjà.
            skipped.append({"producer_id": producer_id, "reason": "already_solicited"})

    return {"created": created, "skipped": skipped}


async def upsert_offer_solicitations(
    session: AsyncSession,
    *,
    market_offer_id: Any,
    sub_category_id: Any,
    zone_id: Any,
    buyers: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Insère les alertes « nouveau produit » manquantes pour les acheteurs locaux."""
    if not buyers:
        return []

    rows = [
        {
            "kind": "NEW_PRODUCT_ALERT",
            "market_offer_id": market_offer_id,
            "target_buyer_id": b["buyer_id"],
            "sub_category_id": sub_category_id,
            "zone_id": zone_id,
            "status": "PENDING",
        }
        for b in buyers
        if b.get("buyer_id")
    ]
    if not rows:
        return []

    stmt = (
        pg_insert(Solicitation)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["market_offer_id", "target_buyer_id"])
        .returning(Solicitation.id, Solicitation.target_buyer_id)
    )
    result = await session.execute(stmt)
    return [{"solicitation_id": r_id, "buyer_id": b_id} for r_id, b_id in result.all()]


async def mark_notified(session: AsyncSession, solicitation_ids: Sequence[Any]) -> None:
    """Passe les sollicitations à NOTIFIED une fois l'outbox alimenté."""
    if not solicitation_ids:
        return
    await session.execute(
        update(Solicitation)
        .where(Solicitation.id.in_(list(solicitation_ids)))
        .values(status="NOTIFIED", notified_at=datetime.utcnow())
    )
