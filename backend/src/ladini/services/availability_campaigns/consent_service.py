"""Consentement / désinscription aux campagnes de disponibilités (base de données).

Règles :
* Avoir déjà écrit à LADINI ≠ être inscrit : seul un `OPTED_IN` explicite (preuve conservée) autorise l'envoi.
* Une désinscription (`OPTED_OUT`) est DÉFINITIVE tant que la personne n'a pas redemandé EXPLICITEMENT à recevoir :
  un import administrateur ne la renverse jamais.
* La preuve ne contient jamais le texte brut du message : version du texte + empreinte (sha256 tronqué) de la référence.
* Concurrence : `lock_status` pose un verrou de lecture (`FOR SHARE`) sur la ligne ; une désinscription concurrente
  attend la fin de la transaction qui met le message en file — et le recontrôle à l'envoi (`dispatcher`) a le dernier mot.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ladini.services.database.common import normalize_phone

logger = logging.getLogger("Ladini.Campaigns.Consent")

TOPIC = "AVAILABILITY_CAMPAIGNS"
OPTED_IN = "OPTED_IN"
OPTED_OUT = "OPTED_OUT"
CONSENT_TEXT_VERSION = 1

#: Sources légitimes d'un opt-in (preuve obligatoire pour chacune).
SOURCES = frozenset({"CONVERSATION_EXPLICIT", "CONSENT_QUESTION_YES", "ADMIN_IMPORT", "FORM"})


def ref_hash(ref: Optional[str]) -> Optional[str]:
    return hashlib.sha256(str(ref).encode("utf-8")).hexdigest()[:16] if ref else None


def build_proof(*, source: str, message_ref: Optional[str] = None, evidence: Optional[str] = None) -> Dict[str, Any]:
    proof: Dict[str, Any] = {"text_version": CONSENT_TEXT_VERSION, "source": source}
    if message_ref:
        proof["message_ref_hash"] = ref_hash(message_ref)
    if evidence:  # description courte fournie par l'administrateur (ex. « formulaire papier du 12/09 »), pas un message
        proof["evidence"] = str(evidence)[:300]
    return proof


async def get_status(session: AsyncSession, phone: str, *, topic: str = TOPIC) -> Optional[str]:
    norm = normalize_phone(phone, required=False)
    if not norm:
        return None
    row = (
        await session.execute(
            text("select status from intelligence.communication_consents where phone = :p and topic = :t"),
            {"p": norm, "t": topic},
        )
    ).first()
    return str(row[0]) if row else None


async def lock_status(session: AsyncSession, phone: str, *, topic: str = TOPIC) -> Optional[str]:
    """Statut avec verrou `FOR SHARE` : une désinscription concurrente est sérialisée avec notre transaction."""
    norm = normalize_phone(phone, required=False)
    if not norm:
        return None
    row = (
        await session.execute(
            text(
                "select status from intelligence.communication_consents "
                "where phone = :p and topic = :t for share"
            ),
            {"p": norm, "t": topic},
        )
    ).first()
    return str(row[0]) if row else None


async def opt_in(
    session: AsyncSession,
    phone: str,
    *,
    source: str,
    proof: Dict[str, Any],
    user_id: Optional[Any] = None,
    override_opt_out: bool = False,
    topic: str = TOPIC,
) -> bool:
    """Enregistre un consentement. Retourne True si l'état a changé / été créé.

    `override_opt_out=False` (import admin) : une désinscription existante n'est JAMAIS renversée.
    `override_opt_out=True` : demande EXPLICITE de la personne elle-même.
    """
    if source not in SOURCES:
        raise ValueError(f"source de consentement inconnue : {source}")
    if not proof:
        raise ValueError("un consentement sans preuve est refusé")
    norm = normalize_phone(phone, required=True)
    guard = "" if override_opt_out else "where intelligence.communication_consents.status <> 'OPTED_OUT'"
    res = await session.execute(
        text(
            "insert into intelligence.communication_consents "
            "(phone, user_id, topic, status, source, proof, consented_at) "
            "values (:p, :u, :t, 'OPTED_IN', :s, cast(:proof as jsonb), :now) "
            "on conflict (phone, topic) do update set "
            "status = 'OPTED_IN', source = excluded.source, proof = excluded.proof, "
            "consented_at = excluded.consented_at, revoked_at = null, "
            "user_id = coalesce(excluded.user_id, intelligence.communication_consents.user_id), "
            "version = intelligence.communication_consents.version + 1, updated_at = now() "
            f"{guard} "
            "returning id"
        ),
        {"p": norm, "u": user_id, "t": topic, "s": source, "proof": _json(proof), "now": datetime.utcnow()},
    )
    return res.first() is not None


async def opt_out(
    session: AsyncSession,
    phone: str,
    *,
    source: str,
    proof: Dict[str, Any],
    user_id: Optional[Any] = None,
    topic: str = TOPIC,
) -> bool:
    """Désinscription immédiate et idempotente (rejouer ne change rien : retourne False si déjà désinscrit)."""
    norm = normalize_phone(phone, required=True)
    res = await session.execute(
        text(
            "insert into intelligence.communication_consents "
            "(phone, user_id, topic, status, source, proof, revoked_at) "
            "values (:p, :u, :t, 'OPTED_OUT', :s, cast(:proof as jsonb), :now) "
            "on conflict (phone, topic) do update set "
            "status = 'OPTED_OUT', source = excluded.source, proof = excluded.proof, "
            "revoked_at = excluded.revoked_at, "
            "user_id = coalesce(excluded.user_id, intelligence.communication_consents.user_id), "
            "version = intelligence.communication_consents.version + 1, updated_at = now() "
            "where intelligence.communication_consents.status <> 'OPTED_OUT' "
            "returning id"
        ),
        {"p": norm, "u": user_id, "t": topic, "s": source, "proof": _json(proof), "now": datetime.utcnow()},
    )
    return res.first() is not None


def _json(value: Dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
