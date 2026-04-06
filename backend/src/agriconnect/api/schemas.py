"""
Schémas Pydantic - Modèles Request/Response pour l'API AgriConnect
"""

from pydantic import BaseModel
from typing import Optional, Dict, Any, List


# ============================================
# REQUEST MODELS
# ============================================

class UserRequest(BaseModel):
    """Requête principale vers l'assistant AgriConnect."""
    user_id: str
    zone_id: str = "Centre"
    query: str
    crop: Optional[str] = "Inconnue"
    flow_type: str = "MESSAGE"
    async_mode: bool = False  # Si True, queue vers Celery
    user_level: str = "debutant"  # "debutant" | "intermediaire" | "expert"


# ============================================
# RESPONSE MODELS
# ============================================

class SuccessResponse(BaseModel):
    """Réponse standard en cas de succès."""
    status: str = "success"
    response: str
    audio_url: Optional[str] = None
    trace: List[str] = []


class AsyncQueuedResponse(BaseModel):
    """Réponse quand une tâche est mise en file d'attente."""
    status: str = "queued"
    task_id: str
    message: str = "Votre demande est en cours de traitement..."
    check_status_at: str


class TaskStatusResponse(BaseModel):
    """Réponse pour le statut d'une tâche async."""
    status: str
    task_id: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
