"""Étape de CONFORMITÉ en amont du graphe (comme la garde de maintenance) — pas un routeur conversationnel.

Traite de façon DÉTERMINISTE et idempotente, sans appeler le LLM :
* arrêt (« stop », « arrêtez de m'envoyer… ») -> `OPTED_OUT` immédiat + confirmation ;
* demande explicite de recevoir les disponibilités -> `OPTED_IN` (preuve : version du texte + empreinte du message) ;
* « oui » à la question de consentement envoyée récemment -> `OPTED_IN`.

Tout le reste retourne None et suit le graphe normal. Rejouer le même message (retry Celery) est sans effet : les
écritures de consentement sont idempotentes.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy import text

from ladini.services.availability_campaigns import consent_service, consent_text
from ladini.services.database.common import normalize_phone

logger = logging.getLogger("Ladini.Campaigns.ComplianceGate")

CONSENT_REQUEST_TEMPLATE = "CONSENT_REQUEST_BUYER"
CONSENT_QUESTION_TTL_HOURS = 48


async def _user_id_for(session: Any, phone: str) -> Optional[str]:
    row = (await session.execute(text("select id from auth.users where phone = :p limit 1"), {"p": phone})).first()
    return str(row[0]) if row else None


async def _consent_question_pending(session: Any, phone: str) -> bool:
    row = (
        await session.execute(
            text(
                "select 1 from intelligence.notification_outbox where recipient_phone = :p and template_key = :t "
                "and status = 'SENT' and sent_at > (now() at time zone 'UTC') - make_interval(hours => :h) limit 1"
            ),
            {"p": phone, "t": CONSENT_REQUEST_TEMPLATE, "h": CONSENT_QUESTION_TTL_HOURS},
        )
    ).first()
    return row is not None


async def handle_inbound(
    phone: str, user_query: str, *, message_sid: Optional[str] = None, interactive_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Retourne un résultat de tour `{"final_response": ...}` si ce message est traité ici, sinon None."""
    if interactive_id:
        return None
    kind = consent_text.classify(user_query)
    norm = normalize_phone(phone, required=False)
    if kind is None or not norm:
        return None

    from ladini.core.database import get_sessionmaker
    from ladini.workers.runtime import worker_session

    if get_sessionmaker() is None:
        # Aucun accès base (tests sans base, init_db non exécuté) : la garde ne peut rien enregistrer, le tour suit
        # le graphe. En production le sessionmaker existe toujours ; une erreur de base REMONTE (retry Celery).
        return None

    async with worker_session() as session:
        user_id = await _user_id_for(session, norm)
        if kind == consent_text.OPT_OUT:
            changed = await consent_service.opt_out(
                session, norm, source="CONVERSATION_STOP", user_id=user_id,
                proof=consent_service.build_proof(source="CONVERSATION_STOP", message_ref=message_sid),
            )
            logger.info("CAMPAIGN_OPT_OUT | phone=***%s | changed=%s", norm[-4:], changed)
            return {"final_response": consent_text.OPT_OUT_CONFIRMATION, "compliance": "OPT_OUT"}
        if kind == consent_text.OPT_IN_REQUEST:
            await consent_service.opt_in(
                session, norm, source="CONVERSATION_EXPLICIT", user_id=user_id, override_opt_out=True,
                proof=consent_service.build_proof(source="CONVERSATION_EXPLICIT", message_ref=message_sid),
            )
            return {"final_response": consent_text.OPT_IN_CONFIRMATION, "compliance": "OPT_IN"}
        # « oui » nu : un consentement SEULEMENT si la question vient d'être posée ; sinon c'est une réponse ordinaire.
        if kind == consent_text.YES and await _consent_question_pending(session, norm):
            if await consent_service.get_status(session, norm) == consent_service.OPTED_IN:
                return None
            await consent_service.opt_in(
                session, norm, source="CONSENT_QUESTION_YES", user_id=user_id, override_opt_out=True,
                proof=consent_service.build_proof(source="CONSENT_QUESTION_YES", message_ref=message_sid),
            )
            return {"final_response": consent_text.OPT_IN_CONFIRMATION, "compliance": "OPT_IN"}
    return None
