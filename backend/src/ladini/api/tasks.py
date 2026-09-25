import asyncio
import logging
import time
from typing import Any, Dict, Optional

from celery.signals import worker_process_init, worker_process_shutdown

from ladini.api.celery_app import celery_app
from ladini.api.response_dispatch import ResponsePlan, get_dispatcher
from ladini.core.database import close_db, get_engine
from ladini.core.idempotency import claim_once, get_cached, release, set_cached
from ladini.core.reply_sink import active_sink
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


@worker_process_init.connect
def init_worker_process(**kwargs):
    """Exécuté une seule fois à l'initialisation du processus worker Celery."""
    global _loop, _orchestrator
    logger.info(
        "Initialisation de la boucle d'événements asyncio globale pour le Worker."
    )
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    # Télémétrie de tour : compteur Redis transversal (un seul wrapper de `Redis.execute_command`).
    try:
        from ladini.core import turn_telemetry

        turn_telemetry.install_redis_instrumentation()
    except Exception:  # pragma: no cover
        pass

    # L'orchestrateur est instancié UNE SEULE FOIS par worker et garde ses connexions chaudes
    _orchestrator = Orchestrator()

    # Télémétrie worker : initialise OTel/Prometheus/Langfuse + instrumente Celery
    try:
        from ladini.core import telemetry

        telemetry.init_telemetry(service_name="ladini-worker")
        telemetry.instrument_celery()
    except Exception as exc:
        logger.warning("Télémétrie worker non initialisée (non bloquant) : %s", exc)

    # Sentry (bug corrigé 2026-09-16, même correctif que api/main.py::lifespan) :
    # `SENTRY_DSN` était configuré en prod mais jamais initialisé — la seule
    # fonction qui appelait `sentry_sdk.init(...)` (`setup_logging()`) n'était
    # appelée nulle part. `init_sentry()` seul (pas `setup_logging()` complet,
    # qui ferait `logging.basicConfig(...)` par-dessus la config déjà posée
    # par le process maître Celery).
    try:
        from ladini.core.logger import init_sentry

        init_sentry()
    except Exception as exc:
        logger.warning("Initialisation Sentry ignorée (non bloquant) : %s", exc)

    # Amorce le pool DB sur la boucle persistante
    try:
        _loop.run_until_complete(_warmup_db())
    except Exception as exc:
        logger.warning("Warm-up DB au démarrage ignoré : %s", exc)

    try:  # base ciblée par la télémétrie + présence des tables (jamais bloquant)
        from ladini.core import turn_telemetry as _tt

        _loop.run_until_complete(_tt.startup_check())
    except Exception as exc:  # pragma: no cover
        logger.warning("Contrôle de télémétrie au démarrage ignoré : %s", exc)


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

# (2026-09-12) Dédoublonnage GLOBAL par `message_sid`, AU NIVEAU DE LA TÂCHE
# CELERY — distinct du dédoublonnage webhook existant (`msg:{MessageSid}`,
# `api/routes/twilio_webhook.py`, qui empêche seulement un DOUBLE ENQUEUE
# depuis un webhook redélivré) ET du cache LLM (`interpreter/routing.py`,
# qui empêche seulement un DOUBLE APPEL LLM). Ce troisième garde-fou
# empêche tout le PIPELINE MÉTIER (executor, écritures DB/MCP, envoi
# WhatsApp) de s'exécuter deux fois pour le même message — que la
# duplication vienne d'une redélivraison broker Celery, d'un double enqueue
# webhook ayant échappé à la garde SETNX, ou d'un appel direct de test.
#
# Deux clés, PAS une machine à 3 états (`PROCESSING`/`COMPLETED`/
# `FAILED_RETRYABLE`) : le comportement voulu s'obtient avec seulement
# `claim_once` (déjà existant, réutilisé tel quel — aucun second système)
# + un `release()` explicite dans le chemin d'échec :
#
#   - `task_claim:{sid}`  — posé en ENTRÉE via `claim_once` (SETNX atomique,
#     sûr entre workers). Un 2e worker qui recevrait CONCURREMMENT le même
#     message_sid échoue son claim → traitement sauté, jamais dupliqué.
#   - `task_done:{sid}`   — posé UNIQUEMENT après un succès COMPLET (executor
#     + dispatch WhatsApp). Une redélivraison qui arrive APRÈS coup trouve
#     ce marqueur et saute tout retraitement.
#
# Le point clé pour ne JAMAIS perdre un message retryable : `task_claim`
# est EXPLICITEMENT relâché (`release()`) dans le `except` avant que
# l'exception ne remonte à `autoretry_for` — sans ce relâchement, une
# erreur DB/MCP/WhatsApp transitoire laisserait la réclamation posée
# jusqu'à expiration de son TTL, et le retry Celery légitime (backoff
# ~5s+) la trouverait encore verrouillée et abandonnerait le message à
# tort. Avec le relâchement, le retry retrouve la clé absente, re-réclame
# normalement, et rejoue le pipeline (y compris, si besoin, un nouvel
# appel LLM si le cache d'interprétation avait lui-même expiré/manqué).
#
# Panne Redis : `claim_once`/`get_cached`/`set_cached`/`release` sont TOUS
# fail-open (voir `core/idempotency.py`) — une indisponibilité Redis
# désactive silencieusement ce dédoublonnage (traitement normal, risque
# know et accepté d'un doublon rarissime) plutôt que de bloquer tout
# traitement de messages.
_TASK_CLAIM_TTL_SECONDS = 300
_TASK_DONE_TTL_SECONDS = 86400


def _task_claim_key(message_sid: Optional[str]) -> Optional[str]:
    return f"task_claim:{message_sid}" if message_sid else None


def _task_done_key(message_sid: Optional[str]) -> Optional[str]:
    return f"task_done:{message_sid}" if message_sid else None


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

    (2026-09-12) Désormais AUSSI transmis à `_orchestrator.handle(...)` et,
    de là, injecté dans l'état initial du graphe (`inputs["message_sid"]`,
    voir `orchestrator.py::_run_market`) — sert de clé au cache
    d'interprétation LLM (`interpreter/routing.py::_cached_llm_completion`) :
    ce `@celery_app.task(autoretry_for=(Exception,), max_retries=3, ...)`
    ci-dessus peut relancer TOUT le tour (y compris l'appel LLM déjà
    réussi) si une erreur survient PLUS TARD (DB, MCP, WhatsApp...) — sans
    ce cache, chaque retry repayait un appel LLM dont le résultat était
    déjà connu.

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

    # Télémétrie du tour (cockpit /admin/monitoring) — best-effort, jamais bloquante. Créé AVANT le dédoublonnage pour
    # que les commandes Redis de claim soient comptées ; abandonné (non écrit) si le message est un doublon.
    from ladini.core import turn_telemetry

    _rec = None
    try:
        _rec = turn_telemetry.TurnRecorder(
            phone=phone_number,
            message_sid=message_sid,
            task_retries=int(getattr(self.request, "retries", 0) or 0),
            user_message=user_query,
            enqueued_at=turn_telemetry.enqueued_at_from_request(self.request),
        )
        turn_telemetry.begin_turn(_rec)
    except Exception:  # pragma: no cover
        _rec = None

    # --- Dédoublonnage GLOBAL par message_sid (voir commentaire au-dessus
    # de la déclaration de la tâche) — AVANT tout traitement métier coûteux. ---
    claim_key = _task_claim_key(message_sid)
    done_key = _task_done_key(message_sid)

    if done_key and get_cached(done_key):
        logger.info(
            "MESSAGE_ALREADY_COMPLETED | message_sid=%s — traitement sauté "
            "(dédoublonnage, déjà traité avec succès)",
            message_sid,
        )
        turn_telemetry.end_turn()
        return [{"status": "duplicate_skipped", "reason": "already_completed"}]

    if claim_key and not claim_once(claim_key, ttl_seconds=_TASK_CLAIM_TTL_SECONDS):
        logger.info(
            "MESSAGE_ALREADY_IN_FLIGHT | message_sid=%s — traitement sauté "
            "(dédoublonnage, déjà en cours sur un autre worker/tentative)",
            message_sid,
        )
        turn_telemetry.end_turn()
        return [{"status": "duplicate_skipped", "reason": "already_processing"}]

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
            message_sid=message_sid,
            channel="WHATSAPP",
        )

    # --- Executor + dispatch sous LA MÊME frontière d'erreur : une
    # exception À N'IMPORTE QUEL POINT (orchestrateur OU envoi WhatsApp)
    # doit relâcher `claim_key` AVANT de remonter à `autoretry_for`, sinon
    # le retry légitime trouverait la réclamation encore posée et
    # abandonnerait le message à tort (voir commentaire au-dessus de la
    # tâche — c'est le point critique "ne jamais perdre un message
    # retryable"). `done_key` n'est posé QUE dans la branche de succès, en
    # bas de ce `try`, jamais dans le `except`.
    _stage = "agent"
    try:
        result = _loop.run_until_complete(_run())
        _stage = "dispatch"

        final_text = result.get("final_response") or "Je n'ai pas pu générer de réponse."
        interactive = result.get("interactive") or None
        plan = ResponsePlan.text(message_sid, final_text, interactive=interactive)

        # (2026-09-02, consolidation finale) : `process_agent_task` ne
        # contient plus AUCUNE logique de transport ni de garde
        # d'idempotence autonome — il construit un `ResponsePlan` (le
        # contrat de sortie du domaine, ici un seul `TextResponse`) et
        # délègue ENTIÈREMENT au `ResponseDispatcher`
        # (api/response_dispatch.py), SEUL point d'autorité pour l'envoi.
        _t_wa = time.perf_counter()
        dispatch_result = _loop.run_until_complete(
            get_dispatcher().dispatch(phone_number, plan)
        )
        if _rec is not None:
            _rec.whatsapp_ms = int((time.perf_counter() - _t_wa) * 1000)
            _rec.response_status = _response_status(dispatch_result)
            _rec.response_text = final_text
    except Exception as e:
        logger.error("Erreur orchestrateur: %s", e)
        if _rec is not None:
            try:
                turn_telemetry.note_error(
                    type(e).__name__, "WHATSAPP" if _stage == "dispatch" else "INTERNAL"
                )
                _rec.response_status = "FAILED" if _stage == "dispatch" else _rec.response_status
                _loop.run_until_complete(turn_telemetry.finish_and_persist(_rec))
            except Exception:  # pragma: no cover - la télémétrie ne masque jamais l'erreur métier
                pass
        if telemetry is not None:
            telemetry.flush()
        release(claim_key)
        raise

    if telemetry is not None:
        telemetry.flush()
    if done_key:
        set_cached(done_key, "1", ttl_seconds=_TASK_DONE_TTL_SECONDS)
    release(claim_key)
    if _rec is not None:
        try:  # après l'envoi et les marqueurs d'idempotence : la télémétrie ne retarde ni ne peut casser ce tour
            _loop.run_until_complete(turn_telemetry.finish_and_persist(_rec))
        except Exception:  # pragma: no cover
            pass
    return dispatch_result


def _response_status(dispatch_result: Any) -> str:
    """Statut d'envoi d'un tour d'après les items du `ResponseDispatcher` (`message_sent`, `message_skipped`,
    `duplicate_suppressed`, `collected` pour le webchat)."""
    items = [i for i in (dispatch_result if isinstance(dispatch_result, list) else [dispatch_result]) if isinstance(i, dict)]
    if any(i.get("status") == "message_skipped" and i.get("reason") == "send_failed" for i in items):
        return "FAILED"
    statuses = [str(i.get("status")) for i in items]
    if statuses and all(x == "duplicate_suppressed" for x in statuses):
        return "DUPLICATE"
    if any(x in {"message_sent", "collected"} for x in statuses):
        return "SENT"
    return "SKIPPED"


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
    # Canal synchrone (webchat) : la réponse revient en HTTP, jamais par
    # WhatsApp — voir `core/reply_sink.py`.
    sink = active_sink()
    if sink is not None:
        sink.append(text)
        return {"status": "collected"}
    plan = ResponsePlan.text(message_sid, text)
    results = await get_dispatcher().dispatch(phone_number, plan)
    return results[0] if results else {"status": "message_skipped", "reason": "no_items"}
