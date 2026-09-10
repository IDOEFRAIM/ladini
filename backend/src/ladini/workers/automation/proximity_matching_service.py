"""ProximityMatchingService — notifie les acheteurs locaux des nouveaux produits.

Même contrat que ``AuctionAutomationService`` : logique pure, idempotente,
n'écrit que ``solicitations`` + ``notification_outbox``.

    1. Récupérer les offres publiques récentes (lot borné).
    2. Identifier les acheteurs de la même zone.
    3. Upsert des alertes (idempotent) → outbox.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ladini.domain.models import MarketOffer, Producer
from ladini.workers.automation.targeting import buyers_in_zone_for_category
from ladini.workers.outbox import templates
from ladini.workers.repositories import outbox_repo, solicitation_repo

logger = logging.getLogger("Ladini.Workers.ProximityMatching")


@dataclass
class ProximityReport:
    offers: int = 0
    buyers_targeted: int = 0
    solicitations_created: int = 0
    outbox_enqueued: int = 0
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "offers": self.offers,
            "buyers_targeted": self.buyers_targeted,
            "solicitations_created": self.solicitations_created,
            "outbox_enqueued": self.outbox_enqueued,
            "errors": self.errors,
        }


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ProximityMatchingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def run(
        self, *, batch_size: int = 100, recent_days: int = 3, targeting_limit: int = 200
    ) -> ProximityReport:
        report = ProximityReport()
        cutoff = datetime.utcnow() - timedelta(days=recent_days)

        stmt = (
            select(MarketOffer, Producer.zone_id, Producer.business_name)
            .join(Producer, Producer.id == MarketOffer.producer_id)
            .where(
                MarketOffer.is_public.is_(True),
                MarketOffer.status == "PUBLISHED",
                MarketOffer.created_at >= cutoff,
            )
            .order_by(MarketOffer.created_at.desc())
            .limit(batch_size)
        )
        rows = (await self.session.execute(stmt)).all()
        report.offers = len(rows)

        for offer, zone_id, producer_name in rows:
            try:
                await self._process_offer(
                    offer, zone_id, producer_name, report, targeting_limit
                )
            except Exception as exc:
                logger.exception("Alerte offre %s échouée", offer.id)
                report.errors.append(f"{offer.id}: {exc}")

        logger.info("ProximityMatching | %s", report.as_dict())
        return report

    async def _process_offer(
        self,
        offer: MarketOffer,
        zone_id: Any,
        producer_name: Optional[str],
        report: ProximityReport,
        targeting_limit: int,
    ) -> None:
        buyers = await buyers_in_zone_for_category(
            self.session, zone_id=zone_id, limit=targeting_limit
        )
        report.buyers_targeted += len(buyers)

        created = await solicitation_repo.upsert_offer_solicitations(
            self.session,
            market_offer_id=offer.id,
            sub_category_id=offer.sub_category_id,
            zone_id=zone_id,
            buyers=buyers,
        )
        if not created:
            return
        report.solicitations_created += len(created)

        ref = str(offer.id)[:8].upper()
        buyer_by_id = {b["buyer_id"]: b for b in buyers}

        entries: List[Dict[str, Any]] = []
        notified_ids: List[Any] = []
        for row in created:
            buyer = buyer_by_id.get(row["buyer_id"])
            if not buyer or not buyer.get("phone"):
                continue
            entries.append(
                {
                    "solicitation_id": row["solicitation_id"],
                    "channel": "WHATSAPP",
                    "recipient_user_id": buyer.get("user_id"),
                    "recipient_phone": buyer["phone"],
                    "template_key": templates.NEW_PRODUCT_ALERT_BUYER,
                    "payload": {
                        "product": offer.product_label,
                        "price": _num(offer.price_per_unit),
                        "unit": (offer.unit or "").upper(),
                        "producer_name": producer_name or "un producteur local",
                        "ref": ref,
                    },
                    "dedupe_key": f"NEW_PRODUCT_ALERT:{offer.id}:{row['buyer_id']}:v1",
                }
            )
            notified_ids.append(row["solicitation_id"])

        if entries:
            report.outbox_enqueued += await outbox_repo.enqueue(self.session, entries)
            await solicitation_repo.mark_notified(self.session, notified_ids)
