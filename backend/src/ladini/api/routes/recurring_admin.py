"""Internal RECURRING OPERATIONS API — server-to-server, gated by `X-Internal-Token`.

Même posture que `commercial_admin.py` : l'appelant est l'adaptateur Next.js admin, qui impose d'abord la session
ADMIN ; le navigateur n'atteint jamais ce routeur. Défense en profondeur : l'écriture du réglage re-vérifie en base
que `actor_id` est un ADMIN.

OPERATIONS (ici) ≠ ANALYTICS (`/internal/analytics/buyers/recurring`) : navigation admin recommandée —
    Recurring › Needs (liste + fiche)   -> GET /needs, GET /needs/{id}
    Recurring › Settings                -> GET/PUT /settings
    Recurring › Analytics               -> /internal/analytics/buyers/recurring (inchangé, agrégats)

Aucune route ici n'écrit sur un besoin : toute mutation d'un besoin passe par le service métier
(`update_recurring_need`), jamais par la console.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ladini.api.security import require_internal_token
from ladini.core.database import get_sessionmaker
from ladini.services.recurring_admin import api

logger = logging.getLogger("Ladini.API.RecurringAdmin")

router = APIRouter(
    prefix="/internal/recurring-admin",
    tags=["Recurring operations (internal)"],
    dependencies=[Depends(require_internal_token)],
)


@router.get("/settings")
async def get_settings() -> dict[str, Any]:
    try:
        async with get_sessionmaker()() as session:
            return await api.read_settings(session)
    except api.ApiError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    except Exception:  # noqa: BLE001 - never leak internals
        logger.exception("recurring settings read failed")
        raise HTTPException(status_code=500, detail="Erreur lors du chargement des réglages.") from None


@router.put("/settings")
async def put_settings(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Corps de requête invalide.") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Corps de requête invalide.")
    try:
        async with get_sessionmaker()() as session:
            result = await api.write_settings(
                session,
                actor_id=body.get("actor_id"),
                minimum_start_lead_days=body.get("minimum_start_lead_days"),
                expected_version=body.get("expected_version"),
                ip_address=request.client.host if request.client else None,
            )
            await session.commit()
            return result
    except api.ApiError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("recurring settings update failed")
        raise HTTPException(status_code=500, detail="Erreur lors de la mise à jour du réglage.") from None


@router.get("/needs")
async def list_needs(request: Request) -> dict[str, Any]:
    try:
        params = api.parse_list_params(request.query_params)
        async with get_sessionmaker()() as session:
            return await api.list_needs(session, params)
    except api.ApiError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("recurring needs list failed")
        raise HTTPException(status_code=500, detail="Erreur lors du chargement des besoins.") from None


@router.get("/needs/{need_id}")
async def need_detail(need_id: str) -> dict[str, Any]:
    try:
        async with get_sessionmaker()() as session:
            return await api.get_need_detail(session, need_id)
    except api.ApiError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("recurring need detail failed | need=%s", need_id)
        raise HTTPException(status_code=500, detail="Erreur lors du chargement du besoin.") from None
