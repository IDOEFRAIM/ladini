"""Points d'accroche du dispatcher d'outbox pour les messages de CAMPAGNE (`payload.campaign_recipient_id`).

* `before_send` : recontrôle, juste avant l'appel fournisseur, ce qui a pu changer depuis la mise en file —
  campagne annulée, destinataire désinscrit (la désinscription gagne toujours), destinataire déjà traité — et calcule
  la fenêtre de service WhatsApp (24 h) FRAÎCHE. Toute exception = on n'envoie pas (fail-closed) ; le message
  sera repris par le backoff normal de l'outbox.
* `skip` : écarte proprement (outbox `SKIPPED`, destinataire `SKIPPED`) sans compter comme un échec fournisseur.
* `after_send` : dans la MÊME transaction que `mark_sent`/`mark_failed`, reporte l'issue sur le destinataire
  (référence fournisseur, `SENT`, ou `FAILED` quand l'outbox est épuisée). Transitions gardées par le statut courant.

Les messages hors campagne ne passent jamais par ici (aucun changement de comportement).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import text

from ladini.services.availability_campaigns import campaign_service, consent_service
from ladini.workers.runtime import worker_session

logger = logging.getLogger("Ladini.Workers.Outbox.CampaignHooks")


def is_campaign_job(job: Dict[str, Any]) -> bool:
    return bool((job.get("payload") or {}).get("campaign_recipient_id"))


async def before_send(job: Dict[str, Any]) -> Optional[str]:
    """None = envoyer ; sinon la raison (stable) de ne pas envoyer."""
    payload = job["payload"]
    rid = payload["campaign_recipient_id"]
    now = datetime.utcnow()
    async with worker_session() as s:
        row = (
            await s.execute(
                text(
                    "select r.status, r.phone, c.status from intelligence.availability_campaign_recipients r "
                    "join intelligence.availability_campaigns c on c.id = r.campaign_id where r.id = :id for update of r"
                ),
                {"id": str(rid)},
            )
        ).first()
        if row is None:
            return "recipient_missing"
        r_status, phone, c_status = row
        if r_status != "QUEUED":
            return "already_processed"
        if not str(payload.get("body") or "").strip():
            return "empty_body"
        if c_status == "CANCELLED":
            return "campaign_cancelled"
        consent = await consent_service.lock_status(s, phone)
        if consent != consent_service.OPTED_IN:
            return "opted_out" if consent == consent_service.OPTED_OUT else "no_consent"
        payload["service_window_open"] = await campaign_service._window_open(s, phone, now)  # noqa: SLF001
    return None


async def skip(job: Dict[str, Any], reason: str) -> None:
    rid = job["payload"]["campaign_recipient_id"]
    async with worker_session() as s:
        await s.execute(
            text("update intelligence.notification_outbox set status='SKIPPED', last_error=:e, updated_at=now() "
                 "where id=:id and status='SENDING'"),
            {"e": reason[:300], "id": str(job["id"])},
        )
        if reason != "already_processed":
            await s.execute(
                text("update intelligence.availability_campaign_recipients set status='SKIPPED', skip_reason=:e, "
                     "updated_at=now() where id=:id and status='QUEUED'"),
                {"e": reason[:300], "id": str(rid)},
            )
    logger.info("CAMPAIGN_SEND_SKIPPED | recipient=%s | reason=%s", rid, reason)


async def after_send(session: Any, job: Dict[str, Any], result: Any) -> None:
    rid = job["payload"]["campaign_recipient_id"]
    if result.ok:
        await session.execute(
            text(
                "update intelligence.availability_campaign_recipients set status='SENT', provider_ref=:ref, "
                "sent_at=:now, last_error=null, updated_at=now() where id=:id and status='QUEUED'"
            ),
            {"ref": result.provider_ref, "now": datetime.utcnow(), "id": str(rid)},
        )
        return
    state = (
        await session.execute(
            text("select status from intelligence.notification_outbox where id=:id"), {"id": str(job["id"])}
        )
    ).scalar()
    await session.execute(
        text(
            "update intelligence.availability_campaign_recipients set last_error=:e, "
            "status=case when :dead then 'FAILED' else status end, updated_at=now() "
            "where id=:id and status='QUEUED'"
        ),
        {"e": (result.error or "unknown")[:300], "dead": state == "DEAD", "id": str(rid)},
    )
