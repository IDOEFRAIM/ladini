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

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ladini.api.security import require_internal_token
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


class WebChatRequest(BaseModel):
    message: str = Field(
        ..., min_length=1, max_length=4000, description="Message de l'utilisateur"
    )
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


async def _run(request: WebChatRequest, workspace_type: str) -> WebChatResponse:
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
