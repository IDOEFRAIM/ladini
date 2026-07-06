"""
AI Tasks — Traitement asynchrone via l'Orchestrateur (production-grade).

PRINCIPES :
  - Hérite de AgriTask (structured logging, backoff, error classification)
  - L'import de l'orchestrateur est LAZY (dans la fonction, pas au top)
  - Singleton orchestrateur par worker process (évite la réinstanciation)
  - SoftTimeLimitExceeded géré proprement → résultat partiel retourné
  - Erreurs classifiées : fatale → pas de retry / transitoire → backoff
  - Memory guard : un worker qui consomme trop est recyclé
  - Request ID tracé de bout en bout (API → Celery → logs)
"""

import logging
import os
import threading
from typing import Any, Dict

from celery.exceptions import SoftTimeLimitExceeded

from agriconnect.workers.celery_app import celery_app
from agriconnect.workers.celery_config import TIME_LIMITS
from agriconnect.workers.task_base import (
    AgriTask,
    FatalTaskError,
    RetryableError,
    error_result,
    success_result,
)

logger = logging.getLogger("AgriConnect.tasks.ai")

# ── Limites de temps pour les tâches IA ──
_AI_LIMITS = TIME_LIMITS["ai"]

# ── Singleton orchestrateur (thread-safe, par worker process) ──
_orchestrator_instance = None
_orchestrator_lock = threading.Lock()


def _get_orchestrator():
    """
    Retourne un singleton de MessageResponseFlow, lazy-loaded + thread-safe.
    Si l'instanciation échoue, on lève RetryableError (le worker retente).
    """
    global _orchestrator_instance
    if _orchestrator_instance is not None:
        return _orchestrator_instance

    with _orchestrator_lock:
        if _orchestrator_instance is not None:
            return _orchestrator_instance
        try:
            from backend._legacy.orchestrateur.message_flow import MessageResponseFlow
            _orchestrator_instance = MessageResponseFlow()
            logger.info(
                "✅ Orchestrator loaded in worker PID=%s", os.getpid()
            )
            return _orchestrator_instance
        except ImportError as e:
            raise FatalTaskError(f"Orchestrator import failed: {e}") from e
        except Exception as e:
            raise RetryableError(f"Orchestrator init failed: {e}") from e


# ── Validation ──

VALID_LEVELS = frozenset(("debutant", "intermediaire", "expert"))
MAX_QUERY_LENGTH = 5000  # 5000 chars max pour une requête


def _validate_inputs(user_query: str, user_level: str, user_id: str) -> str:
    """Valide et sanitize les inputs. Retourne le user_level corrigé."""
    if not user_query or not user_query.strip():
        raise FatalTaskError("Empty query — nothing to process")
    if len(user_query) > MAX_QUERY_LENGTH:
        raise FatalTaskError(
            f"Query too long ({len(user_query)} chars, max {MAX_QUERY_LENGTH})"
        )
    if not user_id:
        raise FatalTaskError("Missing user_id")
    return user_level if user_level in VALID_LEVELS else "debutant"


@celery_app.task(
    base=AgriTask,
    name="agriconnect.workers.tasks.ai.generate_response",
    bind=True,
    max_retries=2,
    soft_time_limit=_AI_LIMITS["soft"],
    time_limit=_AI_LIMITS["hard"],
    acks_late=True,
    reject_on_worker_lost=True,
    track_started=True,
)
def generate_response(
    self,
    user_query: str,
    user_id: str = "anonymous",
    zone_id: str = "Centre",
    crop: str = "Inconnue",
    voice_enabled: bool = False,
    user_level: str = "debutant",
) -> Dict[str, Any]:
    """
    Traite une requête utilisateur via l'orchestrateur complet.

    Même pipeline que /api/v1/ask en mode synchrone, mais exécuté
    dans un worker Celery pour ne pas bloquer le backend FastAPI.

    Error handling:
      - FatalTaskError → pas de retry, résultat d'erreur immédiat
      - RetryableError → exponential backoff
      - SoftTimeLimitExceeded → résultat partiel retourné proprement
      - Toute autre exception → classifiée automatiquement
    """
    try:
        # ── Validate inputs ──
        user_level = _validate_inputs(user_query, user_level, user_id)

        # ── Load orchestrator (lazy singleton) ──
        orchestrator = _get_orchestrator()

        # ── Build initial state (identique à api/routes.py) ──
        initial_state = {
            "requete_utilisateur": user_query,
            "zone_id": zone_id,
            "user_id": user_id,
            "crop": crop,
            "user_level": user_level,
            "user_reliability_score": 1.0,
            "is_sms_mode": False,
            "flow_type": "MESSAGE",
            "execution_path": [],
            "expert_responses": [],
            "meteo_data": {},
            "market_data": {},
            "global_alerts": [],
            "audio_url": None,
        }

        # ── Execute orchestrator ──
        result = orchestrator.run(initial_state)

        response_text = result.get("final_response", "Pas de réponse générée.")
        audio_url = result.get("audio_url")

        # ── TTS optionnel (si non déjà généré par l'orchestrateur) ──
        if voice_enabled and not audio_url and response_text:
            audio_url = _safe_tts(response_text, user_id)

        return success_result(
            data={
                "response": response_text,
                "audio_url": audio_url,
                "trace": result.get("execution_path", []),
                "user_id": user_id,
            },
            task_name=self.name,
        )

    except FatalTaskError as exc:
        # Erreur permanente → ne pas retenter
        logger.error("❌ Fatal error (no retry): %s", exc)
        return error_result(
            error=str(exc),
            task_name=self.name,
            retryable=False,
        )

    except SoftTimeLimitExceeded:
        # Timeout doux → retourner ce qu'on a
        return self.handle_timeout(partial_result={
            "user_id": user_id,
            "query_preview": user_query[:100],
        })

    except Exception as exc:
        # Classifier l'erreur
        classification = self.classify_error(exc)

        if classification == "fatal":
            logger.error("❌ Classified as FATAL: %s", exc)
            return error_result(
                error=str(exc), task_name=self.name, retryable=False,
            )

        if classification == "rate_limit":
            # Backoff long pour les rate limits (60s base)
            self.retry_with_backoff(exc, base_delay=60.0, max_delay=300.0)

        # Retryable → exponential backoff standard
        self.retry_with_backoff(exc, base_delay=10.0, max_delay=120.0)


def _safe_tts(text: str, user_id: str) -> str | None:
    """Tente le TTS sans faire planter la tâche IA principale."""
    try:
        from agriconnect.workers.tasks.voice import generate_tts
        tts_result = generate_tts.apply(args=[text, user_id]).result
        if isinstance(tts_result, dict) and tts_result.get("status") == "success":
            return tts_result.get("audio_path")
    except Exception as tts_err:
        logger.warning("TTS additionnel échoué (non-bloquant): %s", tts_err)
    return None
