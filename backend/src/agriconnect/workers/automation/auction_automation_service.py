"""AuctionAutomationService — engagement automatique sur les enchères.

Pipeline (aucun appel externe, DB uniquement) :

    1. Récupérer les enchères ouvertes à solliciter (lot borné).
    2. Identifier les producteurs cibles (sous-catégorie × zone).
    3. Créer les sollicitations manquantes (idempotent, ON CONFLICT DO NOTHING).
    4. Pour chaque NOUVELLE sollicitation → écrire un message dans l'outbox.

Rejouable sans doublon : les producteurs déjà sollicités sont ignorés, et le
``dedupe_key`` garantit qu'aucun message n'est enfilé deux fois. Si ce service
plante, AUCUN message n'est parti — on relance sans effet de bord.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agriconnect.domain.models import Auction, SubCategory
from agriconnect.workers.automation.targeting import producers_for_auction
from agriconnect.workers.outbox import templates
from agriconnect.workers.repositories import outbox_repo, solicitation_repo

logger = logging.getLogger("AgriConnect.Workers.AuctionAutomation")


@dataclass
class AutomationReport:
    auctions: int = 0
    producers_targeted: int = 0
    solicitations_created: int = 0
    outbox_enqueued: int = 0
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "auctions": self.auctions,
            "producers_targeted": self.producers_targeted,
            "solicitations_created": self.solicitations_created,
            "outbox_enqueued": self.outbox_enqueued,
            "errors": self.errors,
        }


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class AuctionAutomationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def run(
        self, *, batch_size: int = 100, targeting_limit: int = 200
    ) -> AutomationReport:
        report = AutomationReport()
        auctions = await solicitation_repo.fetch_auctions_to_solicit(
            self.session, limit=batch_size
        )
        report.auctions = len(auctions)

        for auction in auctions:
            try:
                await self._process_auction(auction, report, targeting_limit)
            except Exception as exc:  # une enchère qui casse n'arrête pas le lot
                logger.exception("Sollicitation enchère %s échouée", auction.id)
                report.errors.append(f"{auction.id}: {exc}")

        logger.info("AuctionAutomation | %s", report.as_dict())
        return report

    async def _process_auction(
        self, auction: Auction, report: AutomationReport, targeting_limit: int
    ) -> None:
        producers = await producers_for_auction(
            self.session,
            sub_category_id=auction.sub_category_id,
            target_zone_id=auction.target_zone_id,
            limit=targeting_limit,
        )
        report.producers_targeted += len(producers)

        created = await solicitation_repo.upsert_auction_solicitations(
            self.session, auction=auction, producers=producers
        )
        if not created:
            return
        report.solicitations_created += len(created)

        product_label = await self._sub_category_name(auction.sub_category_id)
        ref = str(auction.id)[:8].upper()
        producer_by_id = {p["producer_id"]: p for p in producers}

        entries: List[Dict[str, Any]] = []
        notified_ids: List[Any] = []
        for row in created:
            producer = producer_by_id.get(row["producer_id"])
            if not producer or not producer.get("phone"):
                continue
            entries.append(
                {
                    "solicitation_id": row["solicitation_id"],
                    "channel": "WHATSAPP",
                    "recipient_user_id": producer.get("user_id"),
                    "recipient_phone": producer["phone"],
                    "template_key": templates.AUCTION_INVITE_PRODUCER,
                    "payload": {
                        "product": product_label,
                        "quantity": _num(auction.quantity),
                        "unit": (auction.unit or "").upper(),
                        "max_price": _num(auction.max_price_per_unit),
                        "ref": ref,
                    },
                    "dedupe_key": f"AUCTION_INVITE:{auction.id}:{row['producer_id']}:v1",
                }
            )
            notified_ids.append(row["solicitation_id"])

        if entries:
            report.outbox_enqueued += await outbox_repo.enqueue(self.session, entries)
            await solicitation_repo.mark_notified(self.session, notified_ids)

    async def _sub_category_name(self, sub_category_id: Any) -> str:
        if not sub_category_id:
            return "un produit"
        name = await self.session.scalar(
            select(SubCategory.name).where(SubCategory.id == sub_category_id)
        )
        return name or "un produit"
