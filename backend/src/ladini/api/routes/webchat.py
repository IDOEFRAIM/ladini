"""Canal CHAT WEB (site NextJS) — SYNCHRONE, séparé des webhooks WhatsApp.

(2026-09-19, retour produit : "les producteurs/acheteurs connectés sur le
site doivent pouvoir discuter avec l'agent") — jusqu'ici, aucun endpoint HTTP
ne renvoyait le TEXTE de réponse de l'agent : `/api/market/producer|buyer`
(voir `market.py`) est asynchrone (Celery `.delay()`, renvoie un `task_id`) et
le texte finit par partir sur WhatsApp (`response_dispatch.py`), jamais en
HTTP. ICI, on appelle `Orchestrator.handle(...)` DIRECTEMENT dans le
PROCESSUS API (jamais via Celery/worker) et on renvoie le texte tel quel.

Pourquoi c'est sûr de le faire en process (pas besoin de dupliquer
`api/tasks.py::init_worker_process`) : le pool DB (`core/database.py`) est un
singleton PROCESS-GLOBAL déjà amorcé par le lifespan de `main.py`
(`_warmup_db()`) — l'agent le réutilise tel quel. `Orchestrator()` ne coûte
rien à l'instanciation ; `MarketRuntime`/le client MCP sont ouverts et fermés
à CHAQUE appel de toute façon (aucun warm pool, même côté worker Celery) — le
coût par requête est donc identique dans les deux process. Un handler
`async def` FastAPI tourne déjà dans la boucle uvicorn : pas besoin de la
boucle asyncio dédiée que `tasks.py` crée pour le worker.

SÉCURITÉ — IDENTIQUE au raisonnement de `market.py::require_internal_token` :
`phone_number` est la clé COMPLÈTE de la session de conversation (même
contexte que WhatsApp, par design produit — voir
docs/ONBOARDING_ZONE_REGION_LEVEL_2026-09-18.md pour un autre exemple de
décision produit documentée de la même façon). Ces routes ne doivent JAMAIS
être appelées directement depuis un navigateur : c'est le BACKEND du site
(jamais son frontend) qui doit détenir `INTERNAL_API_TOKEN` et transmettre un
`phone_number` dont il a déjà vérifié la possession (OTP SMS/WhatsApp au
login côté site — sans quoi n'importe qui pourrait lire/écrire la
conversation WhatsApp de n'importe quel autre utilisateur).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from ladini.api.security import require_internal_token
from ladini.core.reply_sink import collect_replies
from ladini.orchestrator.orchestrator import Orchestrator

logger = logging.getLogger("Ladini.WebChatRouter")

# Un seul orchestrateur pour tout le process API, comme `api/tasks.py::_orchestrator`
# pour le worker — instanciation sans coût réseau (voir docstring ci-dessus).
_orchestrator = Orchestrator()

router = APIRouter(
    prefix="/webchat",
    tags=["Web Chat"],
    dependencies=[Depends(require_internal_token)],
)


# ~8 Mo décodés (limite de `services/storage/supabase_storage.py`) en base64.
_MAX_IMAGE_B64_CHARS = 11_500_000


class WebChatRequest(BaseModel):
    # Vide autorisé UNIQUEMENT avec une image (photo envoyée seule).
    message: str = Field(
        default="", max_length=4000, description="Message de l'utilisateur"
    )
    image_base64: Optional[str] = Field(
        default=None,
        max_length=_MAX_IMAGE_B64_CHARS,
        description=(
            "Photo produit (JPEG/PNG/WebP) en base64, sans préfixe `data:`. "
            "Traitée par le pipeline photo — jamais par le LLM."
        ),
    )
    image_mime: Optional[str] = Field(
        default=None,
        description="Type MIME de l'image (image/jpeg, image/png, image/webp).",
    )

    @model_validator(mode="after")
    def _message_or_image(self) -> "WebChatRequest":
        if not self.message.strip() and not self.image_base64:
            raise ValueError("`message` ou `image_base64` est requis.")
        if self.image_base64 and not (self.image_mime or "").strip():
            raise ValueError("`image_mime` est requis avec `image_base64`.")
        return self

    phone_number: str = Field(
        ...,
        description=(
            "Numéro de téléphone DÉJÀ vérifié côté site (OTP) — jamais un "
            "champ texte libre non prouvé."
        ),
    )


class WebChatResponse(BaseModel):
    reply: str
    interactive: Optional[Dict[str, Any]] = None
    workspace_id: str


async def _run_photo_channel(request: WebChatRequest) -> Optional[WebChatResponse]:
    """Photo produit (ou réponse chiffrée au menu « quel produit ? ») — traitée
    par le pipeline photo, JAMAIS par le LLM (incident 2026-09-19 : « je ne
    peux pas voir les photos »). Renvoie None si le tour n'est pas concerné."""
    from ladini.workers.media import product_photo_task as photo

    text = request.message.strip()
    if request.image_base64:
        try:
            binary = base64.b64decode(request.image_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="image_base64 invalide."
            ) from exc
        with collect_replies() as replies:
            await photo.handle_inbound_photo_bytes(
                request.phone_number, binary, str(request.image_mime)
            )
    elif text.isdigit() and await asyncio.to_thread(
        photo.has_pending_photo_selection, request.phone_number
    ):
        with collect_replies() as replies:
            await photo.handle_pending_photo_selection(request.phone_number, text)
    else:
        return None
    return WebChatResponse(
        reply="\n\n".join(replies) or "Photo reçue.",
        workspace_id=request.phone_number,
    )


async def _run(request: WebChatRequest, workspace_type: str) -> WebChatResponse:
    photo_response = await _run_photo_channel(request)
    if photo_response is not None:
        return photo_response
    # `force_role=True` : même comportement que market.py — le rôle
    # (producteur/acheteur) est déterminé par LA ROUTE appelée, jamais par un
    # texte utilisateur ambigu, cohérent avec le fait que le site sait déjà
    # dans quelle section (buyer/producteur) l'utilisateur se trouve.
    result = await _orchestrator.handle(
        phone=request.phone_number,
        user_query=request.message,
        workspace_type=workspace_type,
        force_role=True,
    )
    return WebChatResponse(
        reply=result.get("final_response", ""),
        interactive=result.get("interactive"),
        workspace_id=result.get("workspace_id", request.phone_number),
    )


@router.post("/producer", response_model=WebChatResponse)
async def producer_webchat(request: WebChatRequest) -> WebChatResponse:
    """Un tour de conversation, côté producteur — texte renvoyé directement,
    aucun message WhatsApp envoyé pour ce tour."""
    return await _run(request, "producer")


@router.post("/buyer", response_model=WebChatResponse)
async def buyer_webchat(request: WebChatRequest) -> WebChatResponse:
    """Un tour de conversation, côté acheteur — texte renvoyé directement,
    aucun message WhatsApp envoyé pour ce tour."""
    return await _run(request, "buyer")
