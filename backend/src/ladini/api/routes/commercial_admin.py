"""Internal COMMERCIAL API — server-to-server, gated by `X-Internal-Token`.

Same posture as `analytics_admin.py`: the only caller is the Next.js admin
adapter (`app/api/admin/commercial/*`), which enforces the COMMERCIAL/ADMIN
session first (`requireCommercial`). The browser never reaches this router.

See `ladini.services.commercial` for the non-negotiable invariant this
router exists to protect: a manual follow-up is a plain outbound WhatsApp
send — it never becomes a LangGraph input.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ladini.api.security import require_internal_token
from ladini.core.database import get_sessionmaker
from ladini.services.commercial import admin_api as api

logger = logging.getLogger("Ladini.API.CommercialAdmin")

router = APIRouter(prefix="/internal/commercial", tags=["Commercial (internal)"], dependencies=[Depends(require_internal_token)])


@router.get("/conversations")
async def list_conversations(request: Request) -> dict[str, Any]:
    try:
        params = api.parse_list_params(request.query_params)
        async with get_sessionmaker()() as session:
            return await api.list_conversations(session, params)
    except api.ApiError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - never leak internals
        logger.exception("commercial conversations list failed")
        raise HTTPException(status_code=500, detail="Erreur lors du chargement des conversations.") from None


@router.get("/conversations/{user_id}")
async def conversation_detail(user_id: str) -> dict[str, Any]:
    try:
        async with get_sessionmaker()() as session:
            return await api.get_conversation_detail(session, user_id)
    except api.ApiError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("commercial conversation detail failed | user=%s", user_id)
        raise HTTPException(status_code=500, detail="Erreur lors du chargement de la conversation.") from None


@router.post("/conversations/{user_id}/follow-up")
async def follow_up(user_id: str, request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Corps de requête invalide.") from None
    actor_id = body.get("actor_id")
    message = body.get("message")
    if not actor_id:
        raise HTTPException(status_code=400, detail="actor_id manquant (identité du commercial émetteur).")
    try:
        async with get_sessionmaker()() as session:
            return await api.send_follow_up(session, actor_id=actor_id, user_id_raw=user_id, message=message)
    except api.ApiError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("commercial follow-up send failed | user=%s", user_id)
        raise HTTPException(status_code=500, detail="Erreur lors de l'envoi de la relance.") from None


@router.patch("/conversations/{user_id}/status")
async def update_status(user_id: str, request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Corps de requête invalide.") from None
    actor_id = body.get("actor_id")
    status = body.get("status")
    assigned_commercial_id = body.get("assigned_commercial_id")
    if not actor_id:
        raise HTTPException(status_code=400, detail="actor_id manquant (identité du commercial émetteur).")
    try:
        async with get_sessionmaker()() as session:
            return await api.update_status(
                session,
                actor_id=actor_id,
                user_id_raw=user_id,
                status=status,
                assigned_commercial_id=assigned_commercial_id,
            )
    except api.ApiError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("commercial status update failed | user=%s", user_id)
        raise HTTPException(status_code=500, detail="Erreur lors de la mise à jour du statut.") from None
