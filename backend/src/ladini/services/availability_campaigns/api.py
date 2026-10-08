"""API opérateur (interne) des campagnes de disponibilités : orchestration au-dessus de `campaign_service`.

Chaque ÉCRITURE re-vérifie en base que `actor_id` est un ADMIN (défense en profondeur : l'appelant est l'adaptateur
Next.js admin, qui impose déjà la session ADMIN) et laisse une trace dans `intelligence.audit_logs`.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ladini.services.availability_campaigns import (
    campaign_service,
    consent_service,
    consent_text,
)
from ladini.services.availability_campaigns.campaign_service import CampaignError
from ladini.services.database.common import normalize_phone

ApiError = CampaignError
_MAX_IMPORT = 500


async def require_admin(session: AsyncSession, actor_id: Any) -> uuid.UUID:
    try:
        uid = uuid.UUID(str(actor_id))
    except (ValueError, TypeError):
        raise CampaignError("Identifiant d'acteur invalide.", status=400) from None
    role = (await session.execute(text("select role from auth.users where id = :u"), {"u": str(uid)})).scalar()
    if str(role or "").upper() != "ADMIN":
        raise CampaignError("Action réservée aux administrateurs.", status=403)
    return uid


async def _audit(session: AsyncSession, actor: uuid.UUID, action: str, entity_id: str, metadata: Dict[str, Any]) -> None:
    await session.execute(
        text(
            "insert into intelligence.audit_logs (actor_id, action, entity_type, entity_id, new_value) "
            "values (:a, :act, 'AVAILABILITY_CAMPAIGN', :e, cast(:m as jsonb))"
        ),
        {"a": str(actor), "act": action, "e": entity_id, "m": json.dumps(metadata, default=str)},
    )


def _public(c: Dict[str, Any]) -> Dict[str, Any]:
    """Les dates/UUID sont sérialisés par FastAPI (`jsonable_encoder`) ; rien à transformer ici."""
    return dict(c)


async def list_campaigns(session: AsyncSession, *, status: Optional[str] = None, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    rows = (
        await session.execute(
            text(
                f"select {campaign_service._COLS} from intelligence.availability_campaigns "  # noqa: SLF001
                "where (cast(:s as text) is null or status = :s) order by created_at desc limit :l offset :o"
            ),
            {"s": status, "l": max(1, min(int(limit), 200)), "o": max(0, int(offset))},
        )
    ).mappings().all()
    return {"items": [_public(dict(r)) for r in rows]}


async def campaign_detail(session: AsyncSession, campaign_id: str) -> Dict[str, Any]:
    c = await campaign_service.get_campaign(session, campaign_id)
    metrics = await campaign_service.campaign_metrics(session, campaign_id)
    by_status = (
        await session.execute(
            text("select status, count(*) from intelligence.availability_campaign_recipients where campaign_id=:c group by status"),
            {"c": campaign_id},
        )
    ).all()
    return {"campaign": _public(c), "recipients_by_status": {k: int(v) for k, v in by_status}, "metrics": metrics,
            "excluded_offers": (c.get("preview") or {}).get("excluded", [])}


async def list_recipients(session: AsyncSession, campaign_id: str, *, status: Optional[str] = None, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    await campaign_service.get_campaign(session, campaign_id)
    rows = (
        await session.execute(
            text(
                "select id, run_key, right(phone, 4) as phone_tail, status, skip_reason, last_error, provider_ref is not null as has_ref, "
                "queued_at, sent_at, delivered_at, read_at, replied_at from intelligence.availability_campaign_recipients "
                "where campaign_id=:c and (cast(:s as text) is null or status=:s) order by phone limit :l offset :o"
            ),
            {"c": campaign_id, "s": status, "l": max(1, min(int(limit), 500)), "o": max(0, int(offset))},
        )
    ).mappings().all()
    return {"items": [_public(dict(r)) for r in rows]}


async def create(session: AsyncSession, body: Dict[str, Any]) -> Dict[str, Any]:
    actor = await require_admin(session, body.get("actor_id"))
    c = await campaign_service.create_campaign(session, body, actor_id=actor)
    await _audit(session, actor, "CAMPAIGN_CREATE", str(c["id"]), {"name": c["name"]})
    return _public(c)


async def update(session: AsyncSession, campaign_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    actor = await require_admin(session, body.get("actor_id"))
    if body.get("expected_version") is None:
        raise CampaignError("expected_version est obligatoire.")
    c = await campaign_service.update_draft(session, campaign_id, body, expected_version=int(body["expected_version"]))
    await _audit(session, actor, "CAMPAIGN_UPDATE", campaign_id, {"content_version": c["content_version"]})
    return _public(c)


async def preview(session: AsyncSession, campaign_id: str) -> Dict[str, Any]:
    result: Dict[str, Any] = await campaign_service.preview_campaign(session, campaign_id)
    return result


async def validate(session: AsyncSession, campaign_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    actor = await require_admin(session, body.get("actor_id"))
    c = await campaign_service.validate_campaign(session, campaign_id, actor_id=actor, expected_hash=body.get("content_hash"))
    await _audit(session, actor, "CAMPAIGN_VALIDATE", campaign_id, {"content_hash": c["content_hash"], "next_run_at": c["next_run_at"]})
    return _public(c)


async def cancel(session: AsyncSession, campaign_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    actor = await require_admin(session, body.get("actor_id"))
    c = await campaign_service.cancel_campaign(session, campaign_id, actor_id=actor)
    await _audit(session, actor, "CAMPAIGN_CANCEL", campaign_id, {})
    return _public(c)


async def import_consents(session: AsyncSession, body: Dict[str, Any]) -> Dict[str, Any]:
    """Import de consentements AVEC preuve (ex. formulaire papier). Ne renverse jamais une désinscription."""
    actor = await require_admin(session, body.get("actor_id"))
    evidence = str(body.get("evidence") or "").strip()
    phones = body.get("phones")
    if not evidence:
        raise CampaignError("Une preuve de consentement (evidence) est obligatoire.")
    if not isinstance(phones, list) or not phones or len(phones) > _MAX_IMPORT:
        raise CampaignError(f"phones : liste de 1 à {_MAX_IMPORT} numéros.")
    imported, ignored = 0, []
    for raw in phones:
        norm = normalize_phone(raw, required=False)
        if not norm or not norm.lstrip("+").isdigit() or not 8 <= len(norm.lstrip("+")) <= 15:
            ignored.append({"phone_tail": str(raw)[-4:], "reason": "INVALID_PHONE"})
            continue
        user_id = (await session.execute(text("select id from auth.users where phone=:p limit 1"), {"p": norm})).scalar()
        changed = await consent_service.opt_in(
            session, norm, source="ADMIN_IMPORT", user_id=user_id, override_opt_out=False,
            proof=consent_service.build_proof(source="ADMIN_IMPORT", evidence=evidence),
        )
        if changed:
            imported += 1
        else:
            ignored.append({"phone_tail": norm[-4:], "reason": "OPTED_OUT_RESPECTED"})
    await _audit(session, actor, "CONSENT_IMPORT", "consents", {"imported": imported, "ignored": len(ignored)})
    return {"imported": imported, "ignored": ignored}


async def request_consent(session: AsyncSession, body: Dict[str, Any]) -> Dict[str, Any]:
    """Met en file la QUESTION de consentement (la réponse « oui » est traitée par la garde de conformité).
    Jamais envoyée à une personne désinscrite ou déjà inscrite ; idempotente par numéro et par jour."""
    actor = await require_admin(session, body.get("actor_id"))
    phones = body.get("phones")
    if not isinstance(phones, list) or not phones or len(phones) > _MAX_IMPORT:
        raise CampaignError(f"phones : liste de 1 à {_MAX_IMPORT} numéros.")
    queued, skipped = 0, []
    day = datetime.utcnow().strftime("%Y%m%d")
    for raw in phones:
        norm = normalize_phone(raw, required=False)
        if not norm:
            skipped.append({"phone_tail": str(raw)[-4:], "reason": "INVALID_PHONE"})
            continue
        if await consent_service.get_status(session, norm) is not None:
            skipped.append({"phone_tail": norm[-4:], "reason": "CONSENT_ALREADY_RECORDED"})
            continue
        res = await session.execute(
            text(
                "insert into intelligence.notification_outbox (channel, recipient_phone, template_key, payload, dedupe_key) "
                "values ('WHATSAPP', :p, 'CONSENT_REQUEST_BUYER', cast(:pl as jsonb), :d) on conflict (dedupe_key) do nothing returning id"
            ),
            {"p": norm, "pl": json.dumps({"body": consent_text.CONSENT_QUESTION}), "d": f"consent_request:{norm}:{day}"},
        )
        queued += 1 if res.first() is not None else 0
    await _audit(session, actor, "CONSENT_REQUEST", "consents", {"queued": queued})
    return {"queued": queued, "skipped": skipped}


async def eligible_offers(session: AsyncSession, params: Dict[str, Any]) -> Dict[str, Any]:
    """Offres éligibles ET exclues (avec raisons) pour un filtre donné : lecture seule, sert à préparer une campagne."""
    definition = campaign_service.normalize_definition({"name": "preview", **params})
    out = await campaign_service.compute_preview(session, definition, datetime.utcnow())
    return {"offers": out["offers"], "excluded": out["excluded"], "message": out["message"]}


async def metrics(session: AsyncSession, campaign_id: str) -> Dict[str, Any]:
    await campaign_service.get_campaign(session, campaign_id)
    result: Dict[str, Any] = await campaign_service.campaign_metrics(session, campaign_id)
    return result


async def failures(session: AsyncSession, campaign_id: str) -> List[Dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                "select right(phone, 4) as phone_tail, status, coalesce(last_error, skip_reason) as reason, count(*) as n "
                "from intelligence.availability_campaign_recipients where campaign_id=:c and status in ('FAILED','SKIPPED') "
                "group by 1,2,3 order by n desc"
            ),
            {"c": campaign_id},
        )
    ).mappings().all()
    return [dict(r) for r in rows]
