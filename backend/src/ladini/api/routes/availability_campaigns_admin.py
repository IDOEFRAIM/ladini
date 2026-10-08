"""Internal AVAILABILITY CAMPAIGNS API — server-to-server, gated by `X-Internal-Token`.

Même posture que `recurring_admin.py` : l'appelant est l'adaptateur Next.js admin (session ADMIN imposée en amont) ;
les écritures re-vérifient en base que `actor_id` est un ADMIN et sont auditées. Aucune UI backend : ce routeur +
`docs/PROACTIVE_AVAILABILITY_CAMPAIGNS.md` (exemples curl) tiennent lieu de console.

    GET  /campaigns                      liste (filtre ?status=)
    POST /campaigns                      crée un brouillon
    GET  /campaigns/{id}                 fiche : campagne, destinataires par statut, métriques, offres exclues
    PATCH /campaigns/{id}                modifie un brouillon (expected_version obligatoire)
    GET  /campaigns/{id}/preview         aperçu vivant : offres retenues, exclues + raisons, message, audience
    POST /campaigns/{id}/validate        fige et programme (content_hash = empreinte vue dans l'aperçu)
    POST /campaigns/{id}/cancel          annule (idempotent)
    GET  /campaigns/{id}/recipients      suivi par destinataire (téléphone masqué)
    GET  /campaigns/{id}/results         métriques avec dénominateurs + échecs
    POST /offers/eligible                offres éligibles/exclues pour un filtre, sans créer de campagne
    POST /consents/import                import de consentements AVEC preuve
    POST /consents/request               met en file la question de consentement
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Request

from ladini.api.security import require_internal_token
from ladini.core.database import get_sessionmaker
from ladini.services.availability_campaigns import api
from ladini.services.availability_campaigns.campaign_service import CampaignError

logger = logging.getLogger("Ladini.API.AvailabilityCampaigns")

router = APIRouter(
    prefix="/internal/availability-campaigns",
    tags=["Availability campaigns (internal)"],
    dependencies=[Depends(require_internal_token)],
)


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Corps de requête invalide.") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Corps de requête invalide.")
    return body


async def _run(op: str, fn: Callable[[Any], Awaitable[Any]], *, write: bool = False) -> Any:
    try:
        async with get_sessionmaker()() as session:
            result = await fn(session)
            if write:
                await session.commit()
            return result
    except CampaignError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - never leak internals
        logger.exception("availability campaigns | %s failed", op)
        raise HTTPException(status_code=500, detail="Erreur interne.") from None


@router.get("/campaigns")
async def list_campaigns(request: Request, status: str | None = None, limit: int = 50, offset: int = 0) -> Any:
    return await _run("list", lambda s: api.list_campaigns(s, status=status, limit=limit, offset=offset))


@router.post("/campaigns")
async def create_campaign(request: Request) -> Any:
    body = await _body(request)
    return await _run("create", lambda s: api.create(s, body), write=True)


@router.get("/campaigns/{campaign_id}")
async def campaign_detail(campaign_id: str) -> Any:
    return await _run("detail", lambda s: api.campaign_detail(s, campaign_id))


@router.patch("/campaigns/{campaign_id}")
async def update_campaign(campaign_id: str, request: Request) -> Any:
    body = await _body(request)
    return await _run("update", lambda s: api.update(s, campaign_id, body), write=True)


@router.get("/campaigns/{campaign_id}/preview")
async def preview_campaign(campaign_id: str) -> Any:
    return await _run("preview", lambda s: api.preview(s, campaign_id))


@router.post("/campaigns/{campaign_id}/validate")
async def validate_campaign(campaign_id: str, request: Request) -> Any:
    body = await _body(request)
    return await _run("validate", lambda s: api.validate(s, campaign_id, body), write=True)


@router.post("/campaigns/{campaign_id}/cancel")
async def cancel_campaign(campaign_id: str, request: Request) -> Any:
    body = await _body(request)
    return await _run("cancel", lambda s: api.cancel(s, campaign_id, body), write=True)


@router.get("/campaigns/{campaign_id}/recipients")
async def list_recipients(campaign_id: str, status: str | None = None, limit: int = 100, offset: int = 0) -> Any:
    return await _run("recipients", lambda s: api.list_recipients(s, campaign_id, status=status, limit=limit, offset=offset))


@router.get("/campaigns/{campaign_id}/results")
async def campaign_results(campaign_id: str) -> Any:
    async def go(s: Any) -> Any:
        return {"metrics": await api.metrics(s, campaign_id), "failures": await api.failures(s, campaign_id)}

    return await _run("results", go)


@router.post("/offers/eligible")
async def eligible_offers(request: Request) -> Any:
    body = await _body(request)
    return await _run("eligible", lambda s: api.eligible_offers(s, body))


@router.post("/consents/import")
async def import_consents(request: Request) -> Any:
    body = await _body(request)
    return await _run("consent_import", lambda s: api.import_consents(s, body), write=True)


@router.post("/consents/request")
async def request_consent(request: Request) -> Any:
    body = await _body(request)
    return await _run("consent_request", lambda s: api.request_consent(s, body), write=True)
