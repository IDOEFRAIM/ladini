"""RecurringSupplyDigestService — Phase 4 : agrège le matching silencieux (Phase 3) en UN digest par
(buyer, date) et l'enfile dans `notification_outbox` (mandat §1 : aucun nouveau système de
notification, aucun broker/worker/queue supplémentaire — même pattern que `ProximityMatchingService`
+ `outbox_repo`/`solicitation_repo`, réutilisés tels quels).

## Pourquoi un digest par (buyer, date), pas un par occurrence (mandat §2)

Une requête UNIQUE rassemble TOUTES les occurrences pertinentes de TOUS les acheteurs pour la date
cible, groupées en mémoire par `buyer_id` — jamais une requête par acheteur ni par besoin (mandat
§8/§22, pas de N+1).

## `notified_at` (mandat §11)

Posé au moment où l'occurrence entre RÉELLEMENT dans un digest ENFILÉ (nouvellement inséré dans
l'outbox, pas rejoué par dédup) — même sémantique que `solicitation_repo.mark_notified`, appelé au
même instant que `outbox_repo.enqueue` dans `ProximityMatchingService`. Un digest recalculé mais
identique (même `dedupe_key`) n'avance jamais `notified_at`.

## `PROPOSED` (mandat §12, décision retenue : Option A)

Ce service NE modifie JAMAIS `recurring_need_occurrences.status` — il reste `OPEN`/`MATCHED`, décidé
exclusivement par `NeedMatchingService` (Phase 3). Exposer une occurrence dans un digest est une
information de PRÉSENTATION (`notified_at`), pas une transition d'état métier : la faire passer à
`PROPOSED` aurait exigé de modifier le moteur de matching pour qu'il continue à rematcher un état
qu'il ne connaît pas aujourd'hui — complexité inutile pour ce que `notified_at` suffit à exprimer.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ladini.domain.analytics.business_events import BusinessEventName
from ladini.domain.analytics.emitter import BusinessEventEmitter
from ladini.domain.analytics.metric_dictionary import Journey
from ladini.domain.recurring_supply.digest import (
    NeedAvailability,
    build_digest_text,
    dedupe_key_for_digest,
    digest_counts,
    digest_menu_actions,
    digest_signature,
)
from ladini.workers.outbox import templates

logger = logging.getLogger("ladini.workers.automation.recurring_supply_digest")

# Une seule requête pour TOUTES les occurrences pertinentes de TOUS les acheteurs (mandat §8/§22) —
# jamais une par acheteur. OPEN avec matched=0 est inclus délibérément (mandat §3) : le restaurant
# voit aussi ce qui manque, pas seulement ce qui est couvert.
_RELEVANT_OCCURRENCES_SQL = text(
    "SELECT o.id AS occurrence_id, n.id AS recurring_need_id, o.version, o.requested_quantity, o.quantity_matched, o.unit, "
    "n.buyer_id, sc.name AS product, u.phone AS buyer_phone "
    "FROM marketplace.recurring_need_occurrences o "
    "JOIN marketplace.recurring_needs n ON n.id = o.recurring_need_id "
    "JOIN governance.sub_categories sc ON sc.id = n.sub_category_id "
    "JOIN marketplace.buyer_profiles bp ON bp.id = n.buyer_id "
    "JOIN auth.users u ON u.id = bp.user_id "
    "WHERE n.status = 'ACTIVE' AND o.status = ANY(:statuses) AND o.occurrence_date = :target_date "
    "ORDER BY n.buyer_id, sc.name"
)

_MARK_NOTIFIED_SQL = text(
    "UPDATE marketplace.recurring_need_occurrences SET notified_at = :notified_at "
    "WHERE id = ANY(:occurrence_ids)"
)

_MATCHABLE_OCCURRENCE_STATUSES = ("OPEN", "MATCHED")


@dataclass
class DigestReport:
    buyer_id: str
    occurrence_date: str
    enqueued: bool
    deduplicated: bool = False
    occurrence_count: int = 0
    full_count: int = 0
    partial_count: int = 0
    unavailable_count: int = 0
    digest_version: Optional[str] = None
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class DigestBatchReport:
    trigger: str
    occurrence_date: str
    buyers_examined: int = 0
    reports: List[DigestReport] = field(default_factory=list)
    duration_ms: int = 0


class RecurringSupplyDigestService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def run(self, *, target_date: date, trigger: str = "scheduled") -> DigestBatchReport:
        started = time.monotonic()
        target_dt = datetime.combine(target_date, datetime.min.time())
        logger.info("recurring_digest.started | occurrence_date=%s | trigger=%s", target_date, trigger)

        rows = (
            await self.session.execute(
                _RELEVANT_OCCURRENCES_SQL,
                {"statuses": list(_MATCHABLE_OCCURRENCE_STATUSES), "target_date": target_dt},
            )
        ).mappings().all()

        by_buyer: Dict[Any, List[Any]] = {}
        for row in rows:
            by_buyer.setdefault(row["buyer_id"], []).append(row)

        batch = DigestBatchReport(trigger=trigger, occurrence_date=target_date.isoformat(), buyers_examined=len(by_buyer))
        notified_occurrence_ids: List[Any] = []
        entries: List[Dict[str, Any]] = []

        for buyer_id, buyer_rows in by_buyer.items():
            try:
                needs = [
                    NeedAvailability(
                        product=r["product"],
                        requested_quantity=Decimal(str(r["requested_quantity"])),
                        matched_quantity=Decimal(str(r["quantity_matched"])),
                        unit=r["unit"],
                    )
                    for r in buyer_rows
                ]
                counts = digest_counts(needs)
                signature = digest_signature([(str(r["occurrence_id"]), r["version"]) for r in buyer_rows])
                dedupe_key = dedupe_key_for_digest(buyer_id, target_date, signature)
                body = build_digest_text(needs)
                phone = buyer_rows[0]["buyer_phone"]

                entries.append(
                    {
                        "channel": "WHATSAPP",
                        "recipient_phone": phone,
                        "template_key": templates.RECURRING_SUPPLY_DIGEST_BUYER,
                        # Traçabilité message -> objets métier (B11-recurring) : le digest référence EXACTEMENT
                        # ces occurrences/versions. `render` n'utilise que `body` ; clés additionnelles inertes.
                        "payload": {
                            "body": body,
                            "digest_signature": signature,
                            # B20 : identité + actions du menu numéroté affiché (None : pas de menu) — lu par
                            # `get_last_interactive_outbound` pour attribuer « 1 »/« voir les détails » à CE digest.
                            "interactive": {"menu_id": signature, "actions": digest_menu_actions(needs)},
                            "occurrences": [
                                {
                                    "recurring_need_id": str(r.get("recurring_need_id")) if r.get("recurring_need_id") else None,
                                    "occurrence_id": str(r["occurrence_id"]),
                                    "version": r["version"],
                                }
                                for r in buyer_rows
                            ],
                        },
                        "dedupe_key": dedupe_key,
                    }
                )
                batch.reports.append(
                    DigestReport(
                        buyer_id=str(buyer_id),
                        occurrence_date=target_date.isoformat(),
                        enqueued=True,  # ajusté ci-dessous après le résultat réel de l'INSERT
                        digest_version=signature,
                        **counts,
                    )
                )
                notified_occurrence_ids.append((buyer_id, dedupe_key, [r["occurrence_id"] for r in buyer_rows]))
            except Exception as exc:  # noqa: BLE001 — un buyer en échec ne doit jamais bloquer les autres
                logger.exception("recurring_digest.error | buyer_id=%s", buyer_id)
                batch.reports.append(DigestReport(buyer_id=str(buyer_id), occurrence_date=target_date.isoformat(), enqueued=False, error=str(exc)))

        if entries:
            # `enqueue` retourne le nombre de lignes RÉELLEMENT insérées (ON CONFLICT DO NOTHING,
            # mandat §9) — on doit savoir PAR ENTRÉE si elle a été insérée ou dédupliquée pour ne
            # jamais avancer `notified_at` sur un digest qui n'a en réalité rien envoyé de nouveau.
            inserted_keys = await self._enqueue_and_get_inserted_keys(entries)
            all_occ_ids: List[Any] = []
            for report, (buyer_id, dedupe_key, occ_ids) in zip(
                (r for r in batch.reports if r.enqueued), notified_occurrence_ids, strict=True
            ):
                if dedupe_key in inserted_keys:
                    all_occ_ids.extend(occ_ids)
                    # Analytics Phase C — définition OFFICIELLE : "digest mis en file d'envoi avec
                    # succès" (ligne `notification_outbox` insérée), PAS "reçu par l'acheteur sur
                    # WhatsApp" (aucun accusé provider n'existe ici). Même transaction que l'INSERT
                    # outbox (commit unique plus bas). Émis seulement pour les clés RÉELLEMENT
                    # insérées : un cron rejoué (même dedupe_key) ne réémet rien. entity_id =
                    # uuid5(dedupe_key) : déterministe, la colonne `entity_id` étant un UUID.
                    digest_entity_id = uuid.uuid5(uuid.NAMESPACE_URL, dedupe_key)
                    await BusinessEventEmitter(self.session).emit(
                        event_name=BusinessEventName.RECURRING_DIGEST_SENT,
                        journey=Journey.RECURRING,
                        actor_type="SYSTEM",
                        buyer_id=buyer_id,
                        entity_type="RECURRING_DIGEST",
                        entity_id=digest_entity_id,
                        idempotency_key=f"RECURRING_DIGEST_SENT:{dedupe_key}",
                        metadata={
                            "occurrence_date": target_date.isoformat(),
                            "occurrence_count": report.occurrence_count,
                            "full_count": report.full_count,
                            "partial_count": report.partial_count,
                            "unavailable_count": report.unavailable_count,
                            "digest_version": report.digest_version,
                            "delivery_guarantee": "QUEUED_FOR_OUTBOUND_DELIVERY",
                        },
                    )
                    logger.info("recurring_digest.enqueued | %s", report.as_dict())
                else:
                    report.enqueued = False
                    report.deduplicated = True
                    logger.info("recurring_digest.deduplicated | buyer_id=%s | occurrence_date=%s", buyer_id, target_date)

            if all_occ_ids:
                await self.session.execute(
                    _MARK_NOTIFIED_SQL, {"notified_at": datetime.utcnow(), "occurrence_ids": all_occ_ids}
                )

        await self.session.commit()
        batch.duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "recurring_digest.generated | occurrence_date=%s | buyers=%s | duration_ms=%s",
            target_date, len(by_buyer), batch.duration_ms,
        )
        return batch

    async def _enqueue_and_get_inserted_keys(self, entries: List[Dict[str, Any]]) -> set:
        """`outbox_repo.enqueue` ne renvoie qu'un COMPTE — insuffisant ici où l'on doit savoir QUELLES
        entrées ont été réellement insérées (pour `notified_at`, par buyer). Requête `RETURNING
        dedupe_key` directe, même garde d'idempotence (`ON CONFLICT DO NOTHING`)."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from ladini.domain.models import NotificationOutbox

        if not entries:
            return set()
        stmt = (
            pg_insert(NotificationOutbox)
            .values(entries)
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(NotificationOutbox.dedupe_key)
        )
        result = await self.session.execute(stmt)
        return {row[0] for row in result.all()}


__all__ = ["RecurringSupplyDigestService", "DigestReport", "DigestBatchReport"]
