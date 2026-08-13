import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from twilio.rest import Client
from celery.signals import worker_process_init, worker_process_shutdown

from agriconnect.api.celery_app import celery_app
from agriconnect.core.database import close_db, get_engine
from agriconnect.core.settings import settings
from agriconnect.orchestrator import Orchestrator

logger = logging.getLogger("AgriConnect.Worker")

# Variables globales initialisées au démarrage du worker
_loop: Optional[asyncio.AbstractEventLoop] = None
_orchestrator: Optional[Orchestrator] = None

_TWILIO_SOFT_LIMIT = 1500
_TWILIO_DISCLAIMER = " (Détails complets disponibles sur votre dashboard)"


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
            logger.warning("Warm-up DB ignoré : moteur indisponible (DATABASE_URL manquante ?).")
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
        from agriconnect.services.database.d import AgriDatabaseService
        await AgriDatabaseService().ensure_performance_indexes()
        logger.info("🔧 Schéma DB vérifié (index + colonnes) au démarrage du worker.")
    except Exception as exc:
        logger.warning("Vérification du schéma DB ignorée (non bloquant) : %s", exc)


@worker_process_init.connect
def init_worker_process(**kwargs):
    """Exécuté une seule fois à l'initialisation du processus worker Celery."""
    global _loop, _orchestrator
    logger.info("Initialisation de la boucle d'événements asyncio globale pour le Worker.")
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    # L'orchestrateur est instancié UNE SEULE FOIS par worker et garde ses connexions chaudes
    _orchestrator = Orchestrator()

    # Télémétrie worker : initialise OTel/Prometheus/Langfuse + instrumente Celery
    try:
        from agriconnect.core import telemetry
        telemetry.init_telemetry(service_name="agriconnect-worker")
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


def _chunk_whatsapp_body(body: str, limit: int = _TWILIO_SOFT_LIMIT) -> List[str]:
    """Découpe le message en sous-messages conformes à la limite Twilio (1500 chars)."""
    if not body:
        return [""]

    remaining = body.strip()
    chunks: List[str] = []

    while len(remaining) > limit:
        split_idx = remaining.rfind("\n", 0, limit)
        if split_idx == -1 or split_idx < limit // 2:
            split_idx = limit
        chunk = remaining[:split_idx].rstrip()
        chunks.append(chunk)
        remaining = remaining[split_idx:].lstrip()

    if remaining:
        chunks.append(remaining)

    if len(chunks) > 4:
        kept = chunks[:3]
        kept.append(f"{chunks[3][:limit - len(_TWILIO_DISCLAIMER) - 5]} {_TWILIO_DISCLAIMER}")
        logger.warning("Response exceeded chunk limit; truncated with disclaimer")
        return kept

    return chunks


def send_whatsapp_message(
    client: Client,
    from_number: str,
    to_phone: str,
    body: Optional[str] = None,
    content_sid: Optional[str] = None,
    content_vars: Optional[Dict[str, Any]] = None,
) -> Optional[Any]:
    """Envoie un message WhatsApp via Twilio (texte brut ou Content Template interactif)."""
    clean_from = from_number.replace("whatsapp:", "").strip()
    clean_to = to_phone.replace("whatsapp:", "").strip()

    # Évite les erreurs si l'expéditeur et le destinataire sont identiques
    if clean_from == clean_to:
        logger.warning("Tentative d'envoi WhatsApp vers le même numéro (%s). Ignoré.", clean_to)
        return None

    from_formatted = f"whatsapp:{clean_from}"
    to_formatted = f"whatsapp:{clean_to}"

    kwargs: Dict[str, Any] = {
        "from_": from_formatted,
        "to": to_formatted,
    }

    if content_sid:
        kwargs["content_sid"] = content_sid
        if content_vars:
            kwargs["content_variables"] = json.dumps(content_vars)
        if body:
            kwargs["body"] = body
    elif body:
        kwargs["body"] = body
    else:
        logger.warning("Ni body ni content_sid fourni à send_whatsapp_message.")
        return None

    return client.messages.create(**kwargs)


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
):
    """Point d'entrée worker : exécute la coroutine dans la boucle persistante.

    ``location_shared`` : une position GPS a été reçue et déjà persistée
    (en tâche de fond, côté webhook) pour ce même tour — signal purement
    conversationnel pour que l'agent puisse acquitter la réception pendant
    l'étape finale de l'onboarding (voir agents/onboarding.py). La
    persistance elle-même ne dépend jamais de ce paramètre.
    """
    global _loop, _orchestrator

    if _loop is None or _orchestrator is None:
        raise RuntimeError("Le worker Celery n'a pas été initialisé correctement (boucle/orchestrateur manquant).")

    # Rattache ce tour à la trace infra reçue de l'API (contexte worker)
    try:
        from agriconnect.core import telemetry
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
    provider = str(getattr(settings, "MESSAGING_PROVIDER", "") or "whatsapp_cloud").strip().lower()

    if provider == "twilio":
        return _send_via_twilio(phone_number, final_text, result)

    try:
        return _loop.run_until_complete(_send_via_whatsapp_cloud(phone_number, final_text, result))
    except Exception as e:
        logger.error("Erreur envoi WhatsApp Cloud API : %s", e)
        raise


async def send_confirmation_text(phone_number: str, text: str) -> Dict[str, Any]:
    """Envoi d'un texte simple hors pipeline agent (même dispatch provider que
    ``process_agent_task``) — utilisé par les tâches Celery qui n'ont pas de
    tour de conversation LangGraph à leur origine (ex: confirmation d'ajout
    de photo produit, voir workers/media/product_photo_task.py)."""
    provider = str(getattr(settings, "MESSAGING_PROVIDER", "") or "whatsapp_cloud").strip().lower()
    if provider == "twilio":
        return _send_via_twilio(phone_number, text, {})
    return await _send_via_whatsapp_cloud(phone_number, text, {})


async def _send_via_whatsapp_cloud(
    phone_number: str,
    final_text: str,
    result: Dict[str, Any],
) -> Dict[str, Any]:
    """Envoi via l'API Cloud WhatsApp (Meta directe) — provider par défaut."""
    from agriconnect.services.whatsapp import cloud_api_client as wa

    if not wa.is_configured():
        logger.error("WhatsApp Cloud API configuration incomplete; cannot send response")
        raise RuntimeError("WhatsApp Cloud API configuration incomplete")

    logger.info(
        "WHATSAPP_CLOUD_SEND | to=%s | body_len=%d | body_preview=%r",
        phone_number, len(str(final_text)), str(final_text)[:120],
    )

    # --- Rendu interactif natif (boutons de confirmation) ---
    interactive = result.get("interactive") or {}
    if (
        interactive.get("kind") == "confirm"
        and getattr(settings, "WHATSAPP_NATIVE_INTERACTIVE_ENABLED", True)
    ):
        message_id = await wa.send_interactive_buttons(
            phone_number,
            str(final_text),
            buttons=[
                {"id": "CONFIRM", "title": "✅ Confirmer"},
                {"id": "REJECT", "title": "❌ Annuler"},
            ],
        )
        if message_id is None:
            return {"status": "message_skipped", "reason": "send_failed"}
        return {"status": "message_sent", "sid": message_id, "interactive": "confirm"}

    # --- Sinon : texte brut chunké ---
    message_ids = await wa.send_text(phone_number, str(final_text))
    if not message_ids:
        return {"status": "message_skipped", "reason": "send_failed"}
    return {"status": "message_sent", "sid": message_ids[-1], "chunks": len(message_ids)}


def _send_via_twilio(
    phone_number: str,
    final_text: str,
    result: Dict[str, Any],
) -> Dict[str, Any]:
    """Repli Twilio — conservé intact pour un rollback instantané
    (``MESSAGING_PROVIDER=twilio``) si l'API Cloud pose problème en prod."""
    account_sid = str(settings.TWILIO_ACCOUNT_SID or "").strip()
    auth_token = str(settings.TWILIO_AUTH_TOKEN or "").strip()
    from_number = str(settings.TWILIO_WHATSAPP_NUMBER or "").strip()

    if not account_sid or not auth_token or not from_number:
        logger.error("Twilio configuration incomplete; cannot send WhatsApp response")
        raise RuntimeError("Twilio configuration incomplete")

    client = Client(account_sid, auth_token)
    to_addr = f"whatsapp:{phone_number}"

    logger.info(
        "TWILIO_SEND | account=%s | from=%s | to=%s | body_len=%d | body_preview=%r",
        account_sid[:10], from_number, to_addr, len(str(final_text)), str(final_text)[:120],
    )

    try:
        # --- Rendu interactif (boutons de confirmation) ---
        interactive = result.get("interactive") or {}
        confirm_sid = str(getattr(settings, "TWILIO_CONFIRM_CONTENT_SID", "") or "").strip()
        if (
            interactive.get("kind") == "confirm"
            and getattr(settings, "TWILIO_INTERACTIVE_ENABLED", False)
            and confirm_sid
        ):
            message = send_whatsapp_message(
                client=client,
                from_number=from_number,
                to_phone=to_addr,
                body=str(final_text),
                content_sid=confirm_sid,
                content_vars={"1": str(final_text)[:1500]},
            )
            if message is None:
                return {"status": "message_skipped", "reason": "same_from_to"}
            return {"status": "message_sent", "sid": message.sid, "interactive": "confirm"}

        # --- Sinon : texte brut chunké ---
        chunks = _chunk_whatsapp_body(str(final_text))
        last_sid = None
        for chunk in chunks:
            message = send_whatsapp_message(
                client=client,
                from_number=from_number,
                to_phone=to_addr,
                body=chunk,
            )
            if message is None:
                return {"status": "message_skipped", "reason": "same_from_to"}
            last_sid = message.sid

        return {"status": "message_sent", "sid": last_sid, "chunks": len(chunks)}

    except Exception as e:
        logger.error("Erreur Twilio : %s", e)
        raise