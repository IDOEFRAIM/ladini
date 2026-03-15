"""
Routes API — Endpoints de l'API AgriConnect (production-grade).

L'orchestrateur est initialisé une seule fois (lazy singleton)
et injecté via FastAPI Depends().

Celery dispatch :
  - Fallback automatique vers sync si le broker est indisponible
  - Validation des task_id avant lookup
  - Rate limiting implicite via Celery task annotations
  - Résultats structurés cohérents (success/error/timeout)
"""

import asyncio
import logging
import re
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends, Request
from fastapi.responses import FileResponse

from agriconnect.core.database import check_connection
from agriconnect.graphs.orchestrateur.message_flow import MessageResponseFlow
from agriconnect.workers.tasks.ai import generate_response
from agriconnect.core.settings import settings
from agriconnect.graphs.state import GlobalAgriState
from .schemas import UserRequest, SuccessResponse, AsyncQueuedResponse, TaskStatusResponse
from fastapi import Body

# MCP ShieldHub / Bridge
from agriconnect.protocols.mcp.security.shield_hub import ShieldHub


logger = logging.getLogger("AgriConnect.API")

router = APIRouter()


# ── Dependency : Orchestrator (lazy singleton, thread-safe) ──

_orchestrator_instance = None
_orchestrator_lock = threading.Lock()


def _get_orchestrator():
    """Instancié une seule fois, au premier appel — thread-safe, retryable."""
    global _orchestrator_instance
    if _orchestrator_instance is not None:
        return _orchestrator_instance

    with _orchestrator_lock:
        # Double-check after acquiring lock
        if _orchestrator_instance is not None:
            return _orchestrator_instance
        
        try:
            _orchestrator_instance = MessageResponseFlow()
            logger.info("Orchestrator loaded successfully.")
            return _orchestrator_instance
        except Exception as e:
            logger.warning("Failed to load Orchestrator: %s", e, exc_info=True)
            # Do NOT cache None — next call will retry
            return None


def get_orchestrator():
    """FastAPI dependency — retourne le singleton ou lève 503."""
    orch = _get_orchestrator()
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator unavailable. Retrying on next request.")
    return orch


# ── ShieldHub singleton (lazy, thread-safe) ─────────────────────────────
_shield_instances = {}
_shield_lock = threading.Lock()


def _get_shield(session_id: str = "web_user"):
    """Instantiate a single ShieldHub for the app. Session_id is a default
    placeholder; callers may override by passing X-Session-Id header.
    """
    global _shield_instances
    if session_id in _shield_instances:
        return _shield_instances[session_id]
    with _shield_lock:
        if session_id in _shield_instances:
            return _shield_instances[session_id]
        _shield_instances[session_id] = ShieldHub(session_id=session_id)
        logger.info("ShieldHub initialized for session=%s", session_id)
        return _shield_instances[session_id]



# ── Helpers ──

# Regex stricte pour valider un task_id Celery (UUID)
_TASK_ID_PATTERN = re.compile(
    r"^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$"
)


def _validate_task_id(task_id: str) -> str:
    """Valide le format du task_id pour éviter les injections."""
    if not _TASK_ID_PATTERN.match(task_id):
        raise HTTPException(status_code=400, detail="Invalid task ID format")
    return task_id


def _dispatch_to_celery(req: UserRequest) -> AsyncQueuedResponse:
    """
    Dispatch une requête vers Celery avec gestion d'erreur robuste.
    Lève HTTPException(503) si le broker est indisponible.
    """
    valid_levels = ("debutant", "intermediaire", "expert")
    user_level = req.user_level if req.user_level in valid_levels else "debutant"

    try:
        task = generate_response.apply_async(
            kwargs={
                "user_query": req.query,
                "user_id": req.user_id,
                "zone_id": req.zone_id,
                "crop": req.crop,
                "voice_enabled": True,
                "user_level": user_level,
            },
            # Options de dispatch
            queue="ai",
            priority=5,                    # priorité moyenne
            expires=600,                   # expire après 10 min si pas traitée
            retry=True,                    # retry si le broker est temporairement down
            retry_policy={
                "max_retries": 3,
                "interval_start": 0.2,
                "interval_step": 0.5,
                "interval_max": 3.0,
            },
        )
        return AsyncQueuedResponse(
            task_id=task.id,
            check_status_at=f"/api/v1/task/{task.id}",
        )
    except Exception as e:
        logger.error("Failed to dispatch task to Celery: %s", e)
        raise HTTPException(
            status_code=503,
            detail="Task queue unavailable. Please try again.",
        )


# ── Routes ──────────────────────────────────────────────────

@router.post("/api/v1/ask", response_model=None)
async def ask_agent(
    req: UserRequest,
    background_tasks: BackgroundTasks,
    orchestrator=Depends(get_orchestrator),
):
    """
    Endpoint principal — interagit avec l'Orchestrateur AgConnect.

    - async_mode=False : Traitement synchrone (réponse immédiate)
    - async_mode=True  : Queue vers Celery (TTS, tasks longues)

    Si le broker Celery est indisponible en mode async,
    on tombe automatiquement en mode synchrone (graceful degradation).
    """
    logger.info(
        "Request received: user=%s zone=%s async=%s level=%s",
        req.user_id, req.zone_id, req.async_mode, req.user_level,
    )

    # --- Mode asynchrone (Celery) ---
    if req.async_mode:
        try:
            return _dispatch_to_celery(req)
        except HTTPException:
            # Broker down → fallback vers sync avec warning
            logger.warning(
                "Celery broker unavailable, falling back to sync mode for user=%s",
                req.user_id,
            )
            # On continue vers le mode sync ci-dessous

    # --- Mode synchrone (non-blocking: run in thread pool) ---
    valid_levels = ("debutant", "intermediaire", "expert")
    user_level = req.user_level if req.user_level in valid_levels else "debutant"

    initial_state: GlobalAgriState = {
        "requete_utilisateur": req.query,
        "zone_id": req.zone_id,
        "user_id": req.user_id,
        "crop": req.crop,
        "user_reliability_score": 1.0,
        "is_sms_mode": False,
        "flow_type": req.flow_type,
        "user_level": user_level,
        "execution_path": [],
        "expert_responses": [],
        "meteo_data": {},
        "market_data": {},
        "global_alerts": [],
        "audio_url": None,
    }

    try:
        # Run blocking orchestrator in a thread pool to avoid blocking the event loop
        result = await asyncio.to_thread(orchestrator.run, initial_state)
        final_text = result.get("final_response", "Je n'ai pas pu générer de réponse.")
        audio_url = result.get("audio_url")
        audio_download = None
        if audio_url:
            audio_id = Path(audio_url).stem
            audio_download = f"/api/v1/audio/{audio_id}"

        logger.info("Response generated (audio=%s)", "yes" if audio_url else "no")
        return SuccessResponse(
            response=final_text,
            audio_url=audio_download,
            trace=result.get("execution_path", []),
        )
    except Exception as e:
        logger.warning("Orchestrator execution failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Processing failed. Please try again.")


@router.get("/api/v1/task/{task_id}")
async def get_task_status(task_id: str):
    """
    Vérifier le statut d'une tâche Celery async.

    Retourne un résultat structuré :
      - processing : tâche en cours
      - completed  : tâche terminée avec résultat
      - failed     : tâche échouée (erreur dans le résultat ou exception)
      - unknown    : broker indisponible
    """
    task_id = _validate_task_id(task_id)

    try:
        from agriconnect.workers.celery_app import celery_app

        task_result = celery_app.AsyncResult(task_id)

        # Tâche pas encore terminée
        if not task_result.ready():
            state = task_result.state  # PENDING, STARTED, RETRY
            return {
                "status": "processing",
                "task_id": task_id,
                "state": state,
            }

        # Tâche terminée avec succès
        if task_result.successful():
            result = task_result.result

            # Vérifier si le résultat est lui-même un error_result
            if isinstance(result, dict) and result.get("status") == "error":
                return {
                    "status": "failed",
                    "task_id": task_id,
                    "error": result.get("error", "Unknown error"),
                    "retryable": result.get("retryable", False),
                }

            return {
                "status": "completed",
                "task_id": task_id,
                "result": result,
            }

        # Tâche échouée (exception non attrapée)
        error_info = str(task_result.result) if task_result.result else "Unknown error"
        return {
            "status": "failed",
            "task_id": task_id,
            "error": error_info,
        }

    except Exception as e:
        logger.warning("Task status check error: %s", e)
        return {
            "status": "unknown",
            "task_id": task_id,
            "error": "Task broker temporarily unavailable",
        }


@router.delete("/api/v1/task/{task_id}")
async def cancel_task(task_id: str):
    """
    Annuler (révoquer) une tâche Celery en cours ou en attente.
    """
    task_id = _validate_task_id(task_id)

    try:
        from agriconnect.workers.celery_app import celery_app
        celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
        logger.info("Task %s revoked by user", task_id)
        return {"status": "revoked", "task_id": task_id}
    except Exception as e:
        logger.warning("Task revocation error: %s", e)
        raise HTTPException(status_code=503, detail="Task broker unavailable")


# ── Audio download ──────────────────────────────────────────

AUDIO_DIR = Path(settings.AUDIO_OUTPUT_DIR).resolve()
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# Regex: UUID only (hex + dashes), prevents path traversal
_AUDIO_ID_PATTERN = re.compile(r"^[a-fA-F0-9\-]{1,64}$")


@router.get("/api/v1/audio/{audio_id}")
async def download_audio(audio_id: str):
    """Telecharger un fichier audio genere par le TTS."""
    # Validate audio_id format (UUID only — no slashes, dots, etc.)
    if not _AUDIO_ID_PATTERN.match(audio_id):
        raise HTTPException(status_code=400, detail="Invalid audio ID format")

    file_path = (AUDIO_DIR / f"{audio_id}.wav").resolve()

    # Ensure resolved path is still within AUDIO_DIR (path traversal protection)
    if not str(file_path).startswith(str(AUDIO_DIR)):
        raise HTTPException(status_code=400, detail="Invalid audio ID")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")
    return FileResponse(path=str(file_path), media_type="audio/wav", filename=f"{audio_id}.wav")


# ── Health / Root ───────────────────────────────────────────

@router.get("/health")
def health_check():
    """Health check avec état DB et Celery broker."""
    db_ok = check_connection()

    # Quick broker check (ne bloque pas longtemps)
    broker_ok = False
    try:
        from agriconnect.workers.celery_app import celery_app
        conn = celery_app.connection()
        conn.ensure_connection(max_retries=1, interval_start=0.2)
        conn.close()
        broker_ok = True
    except Exception:
        pass

    status = "healthy"
    if not db_ok:
        status = "degraded"
    if not broker_ok:
        status = "degraded" if db_ok else "unhealthy"

    return {
        "status": status,
        "database": "connected" if db_ok else "disconnected",
        "broker": "connected" if broker_ok else "disconnected",
        "version": settings.APP_VERSION,
    }


@router.get("/")
def root():
    """Root endpoint."""
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "running",
        "docs": "/docs",
    }


# ── MCP Bridge endpoints (ShieldHub) ───────────────────────────────────
@router.post("/api/v1/mcp/call")
async def mcp_call(request: Request, payload: dict = Body(...)):
    """Call an MCP tool via ShieldHub. Payload: {"tool": str, "args": dict}.

    Optional header `X-Session-Id` overrides the default session id used
    when instantiating the ShieldHub (useful for per-user isolation).
    """
    tool = payload.get("tool")
    args = payload.get("args") or {}
    if not tool:
        raise HTTPException(status_code=400, detail="Missing 'tool' in payload")

    session_id = request.headers.get("X-Session-Id") or "web_user"
    shield = _get_shield(session_id=session_id)
    try:
        result = await shield.call(tool, args)
        return {"ok": True, "tool": tool, "result": result}
    except Exception as e:
        logger.exception("MCP call failed: %s", e)
        raise HTTPException(status_code=500, detail=f"MCP call error for '{tool}': {e}")


@router.get("/api/v1/mcp/tools")
async def mcp_list_tools():
    """Return list of tools visible through the ShieldHub (RAG + DB)."""
    shield = _get_shield()
    try:
        tools = await shield.list_tools()
        return {"ok": True, **tools}
    except Exception as e:
        logger.exception("List tools failed: %s", e)
        raise HTTPException(status_code=500, detail="Could not list tools")
