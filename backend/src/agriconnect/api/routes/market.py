from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict
import logging

# On importe la tâche Celery définie dans api.tasks
from agriconnect.api.tasks import process_agent_task
from agriconnect.api.celery_app import celery_app

logger = logging.getLogger("AgriConnect.MarketRouter")
router = APIRouter(prefix="/market", tags=["Market Coach"])

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
        return {"status": "failed", "error": str(task_result.result)}
        
    return {
        "task_id": task_id,
        "status": task_result.status, # 'PENDING', 'SUCCESS', 'FAILURE'
        "result": task_result.result if task_result.ready() else None
    }