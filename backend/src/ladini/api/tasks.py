import asyncio
import logging
from typing import Any, Dict, Optional

from celery.signals import worker_process_init, worker_process_shutdown

from ladini.api.celery_app import celery_app
from ladini.api.response_dispatch import ResponsePlan, get_dispatcher
from ladini.core.database import close_db, get_engine
from ladini.orchestrator import Orchestrator

logger = logging.getLogger("Ladini.Worker")

# Variables globales initialisées au démarrage du worker
_loop: Optional[asyncio.AbstractEventLoop] = None
_orchestrator: Optional[Orchestrator] = None


# --- Initialisation de la boucle d'événements au démarrage du Worker ---


async def _warmup_db() -> None:
    """Ouvre une connexion réelle pour amorcer le pool + handshake SSL.

    Amorce le pool une fois au démarrage du worker pour éviter que la 1ère
    tâche paie la connexion à froid vers la base de données.
    """
    from sqlalchemy import text as _sql_text

    try:
        engine = get_engine()
        if engine is None:
            logger.warning(
                "Warm-up DB ignoré : moteur indisponible (DATABASE_URL manquante ?)."
            )
            return
        async with engine.connect() as conn:
            await conn.execute(_sql_text("SELECT 1"))
        logger.info("🔥 Pool DB amorcé (warm-up) au démarrage du worker.")
    except Exception as exc:
        logger.warning("Warm-up DB échoué (non bloquant) : %s", exc)


async def _ensure_schema() -> None:
    """Applique les DDL idempotents (index + colonnes ajoutées) au démarrage.

    Best-effort, jamais bloquant : un échec ici (droits insuffisants,
    connexion transitoire) ne doit jamais empêcher le worker de démarrer —
    voir services/database/common.py::SCHEMA_COLUMN_DDL (ex: location_updated_at).
    """
    try:
        from ladini.services.database.d import AgriDatabaseService

        await AgriDatabaseService().ensure_performance_indexes()
        logger.info("🔧 Schéma DB vérifié (index + colonnes) au démarrage du worker.")
    except Exception as exc:
        logger.warning("Vérification du schéma DB ignorée (non bloquant) : %s", exc)


@worker_process_init.connect
def init_worker_process(**kwargs):
    """Exécuté une seule fois à l'initialisation du processus worker Celery."""
    global _loop, _orchestrator
    logger.info(
        "Initialisation de la boucle d'événements asyncio globale pour le Worker."
    )
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    # L'orchestrateur est instancié UNE SEULE FOIS par worker et garde ses connexions chaudes
    _orchestrator = Orchestrator()

    # Télémétrie worker : initialise OTel/Prometheus/Langfuse + instrumente Celery
    try:
        from ladini.core import telemetry

        telemetry.init_telemetry(service_name="ladini-worker")
        telemetry.instrument_celery()
    except Exception as exc:
        logger.warning("Télémétrie worker non initialisée (non bloquant) : %s", exc)

    # Amorce le pool DB sur la boucle persistante
    try:
        _loop.run_until_complete(_warmup_db())
    except Exception as exc:
        logger.warning("Warm-up DB au démarrage ignoré : %s", exc)

    # Applique les DDL idempotents (nouvelles colonnes/index) — best-effort.
    try:
        _loop.run_until_complete(_ensure_schema())
    except Exception as exc:
        logger.warning("Vérification du schéma au démarrage ignorée : %s", exc)


@worker_process_shutdown.connect
def shutdown_worker_process(**kwargs):
    """Exécuté à la fermeture du worker. Nettoyage propre des ressources."""
    global _loop
    if _loop and not _loop.is_closed():
        try:
            _loop.run_until_complete(close_db())
        except Exception as exc:
            logger.warning("close_db au shutdown échoué : %s", exc)
        finally:
            _loop.close()
            logger.info("Boucle d'événements asyncio globale fermée.")


# --- Tâche Celery ---


@celery_app.task(
    bind=True,
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=5,
    retry_jitter=True,
)
def process_agent_task(
    self,
    phone_number: str = "",
    user_query: str = "",
    workspace_type: Optional[str] = None,
    role: Optional[str] = None,
    force_role: bool = False,
    interactive_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    location_shared: bool = False,
    location_outcome: Optional[str] = None,
    location_lat: Optional[float] = None,
    location_lon: Optional[float] = None,
    message_sid: Optional[str] = None,
):
    """Point d'entrée worker : exécute la coroutine dans la boucle persistante.

    ``message_sid`` (2026-09-02) : identifiant STABLE de l'événement entrant
    (même valeur que la clé de dédoublonnage webhook, `msg:{MessageSid}`) —
    devient `ResponsePlan.event_id`, la clé d'idempotence de l'ENVOI
    (`ResponseDispatcher`, voir api/response_dispatch.py) pour qu'un retry
    Celery de CETTE tâche (après un envoi déjà réussi) ne déclenche jamais
    un second message.

    ``location_shared`` : une position GPS a été reçue et déjà persistée —
    SYNCHRONE, AVANT cet enqueue (voir core/location.py, refonte 2026-09-02) —
    signal purement conversationnel pour que l'agent puisse acquitter la
    réception pendant l'étape finale de l'onboarding (agents/onboarding.py).

    ``location_outcome``/``location_lat``/``location_lon`` : l'issue EXACTE
    de cette persistance (`core.location.LocationOutcome`, déjà résolue côté
    webhook) — l'agent n'a plus besoin de relire la DB pour la redécouvrir
    (élimine la course webhook/tâche ET le repli silencieux sur une position
    périmée, voir `flows/buyer/gps_delivery_gate.py::resolve_gps_stage`).
    """
    global _loop, _orchestrator

    if _loop is None or _orchestrator is None:
        raise RuntimeError(
            "Le worker Celery n'a pas été initialisé correctement (boucle/orchestrateur manquant)."
        )

    # Rattache ce tour à la trace infra reçue de l'API (contexte worker)
    try:
        from ladini.core import telemetry

        telemetry.set_trace_context(trace_id, user_phone=phone_number)
    except Exception:
        telemetry = None  # type: ignore

    async def _run():
        resolved_type = workspace_type
        forced = bool(force_role)
        if not resolved_type and role:
            resolved_type = "buyer" if role.upper() == "BUYER" else "producer"
            forced = True

        return await _orchestrator.handle(
            phone_number,
            user_query,
            workspace_type=resolved_type,
            force_role=forced,
            interactive_id=interactive_id,
            location_shared=location_shared,
            location_outcome=location_outcome,
            location_lat=location_lat,
            location_lon=location_lon,
        )

    try:
        result = _loop.run_until_complete(_run())
    except Exception as e:
        logger.error("Erreur orchestrateur: %s", e)
        if telemetry is not None:
            telemetry.flush()
        raise

    if telemetry is not None:
        telemetry.flush()

    final_text = result.get("final_response") or "Je n'ai pas pu générer de réponse."
    interactive = result.get("interactive") or None
    plan = ResponsePlan.text(message_sid, final_text, interactive=interactive)

    # (2026-09-02, consolidation finale) : `process_agent_task` ne contient
    # plus AUCUNE logique de transport ni de garde d'idempotence autonome —
    # il construit un `ResponsePlan` (le contrat de sortie du domaine, ici
    # un seul `TextResponse`) et délègue ENTIÈREMENT au `ResponseDispatcher`
    # (api/response_dispatch.py), SEUL point d'autorité pour l'envoi.
    return _loop.run_until_complete(
        get_dispatcher().dispatch(phone_number, plan)
    )


async def send_confirmation_text(
    phone_number: str, text: str, *, message_sid: Optional[str] = None
) -> Dict[str, Any]:
    """Constructeur de `ResponsePlan` (texte simple) + délégation complète
    au `ResponseDispatcher` — utilisé par les tâches Celery qui n'ont pas de
    tour de conversation LangGraph à leur origine (ex: confirmation d'ajout
    de photo produit, voir workers/media/product_photo_task.py).

    (2026-09-02, consolidation finale — mandat §2) : cette fonction n'est
    PLUS un second chokepoint d'envoi — elle ne fait qu'emballer `text` dans
    un `ResponsePlan` à un seul `TextResponse` et appeler EXACTEMENT le même
    `ResponseDispatcher` que `process_agent_task`. Aucune logique de
    provider, de claim, ou de transport ne vit plus ici."""
    plan = ResponsePlan.text(message_sid, text)
    results = await get_dispatcher().dispatch(phone_number, plan)
    return results[0] if results else {"status": "message_skipped", "reason": "no_items"}
