import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ladini.api.celery_app import celery_app
from ladini.api.security import require_internal_token

# On importe la tâche Celery définie dans api.tasks
from ladini.api.tasks import process_agent_task

logger = logging.getLogger("Ladini.MarketRouter")

# ⚠️ AUDIT SÉCURITÉ 2026-09-10 — ces routes étaient entièrement NON
# AUTHENTIFIÉES. `/producer` et `/buyer` font tourner l'agent pour un
# `phone_number` arbitraire avec `force_role=True` : quiconque atteignait
# l'API pouvait donc agir comme n'importe quel utilisateur (passer et
# confirmer des commandes, désigner un gagnant d'enchère, annuler, modifier
# le catalogue) sans jamais toucher WhatsApp ni présenter de signature.
# `/status/{task_id}` renvoyait en plus le résultat d'une tâche quelconque,
# donc la réponse conversationnelle d'un autre utilisateur.
#
# Secret partagé au niveau du ROUTER (fail-closed : `INTERNAL_API_TOKEN` non
# configuré -> 503 pour tout le monde), afin que toute route ajoutée ici plus
# tard en hérite automatiquement. Le trafic utilisateur réel n'emprunte JAMAIS
# ce chemin : il arrive par les webhooks signés (`webhook/twilio`,
# `webhook/whatsapp`).
router = APIRouter(
    prefix="/market",
    tags=["Market Coach"],
    dependencies=[Depends(require_internal_token)],
)


class AgentRequest(BaseModel):
    message: str = Field(..., description="Message utilisateur")
    phone_number: str = Field(..., description="Numéro WhatsApp")
    state_updates: Dict[str, Any] = Field(default_factory=dict)


# --- Routes Asynchrones ---


@router.post("/producer")
async def producer_agent(request: AgentRequest):
    """Envoie la tâche de l'agent PRODUCER au worker Celery."""
    task = process_agent_task.delay(
        phone_number=request.phone_number,
        user_query=request.message,
        workspace_type="producer",
        force_role=True,
    )
    return {"task_id": task.id, "status": "processing"}


@router.post("/buyer")
async def buyer_agent(request: AgentRequest):
    """Envoie la tâche de l'agent BUYER au worker Celery."""
    task = process_agent_task.delay(
        phone_number=request.phone_number,
        user_query=request.message,
        workspace_type="buyer",
        force_role=True,
    )
    return {"task_id": task.id, "status": "processing"}


@router.get("/status/{task_id}")
async def get_task_status(task_id: str):
    """Permet au client de vérifier si le travail est terminé et récupérer le résultat."""
    task_result = celery_app.AsyncResult(task_id)

    if task_result.failed():
        # Ne renvoie que le TYPE de l'exception, jamais son message : celui-ci
        # transporte régulièrement des fragments d'URL de connexion, de requête
        # SQL ou de payload utilisateur (audit 2026-09-10). Le détail complet
        # reste dans les logs serveur.
        logger.error(
            "MARKET_TASK_FAILED | task_id=%s", task_id, exc_info=task_result.result
        )
        return {
            "status": "failed",
            "error": type(task_result.result).__name__,
        }

    return {
        "task_id": task_id,
        "status": task_result.status,  # 'PENDING', 'SUCCESS', 'FAILURE'
        "result": task_result.result if task_result.ready() else None,
    }
