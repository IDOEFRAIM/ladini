import asyncio
import logging
from typing import List, Optional

from twilio.rest import Client
from celery.signals import worker_process_init, worker_process_shutdown

from agriconnect.api.celery_app import celery_app
from agriconnect.core.database import close_db
from agriconnect.core.settings import settings
from agriconnect.orchestrator import Orchestrator

logger = logging.getLogger("AgriConnect.Worker")

# On définit ces variables au niveau global, elles seront initialisées par le signal
_loop: Optional[asyncio.AbstractEventLoop] = None
_orchestrator: Optional[Orchestrator] = None

_TWILIO_SOFT_LIMIT = 1500
_TWILIO_DISCLAIMER = " (Détails complets disponibles sur votre dashboard)"


# --- Initialisation de la boucle d'événements au démarrage du Worker ---

@worker_process_init.connect
def init_worker_process(**kwargs):
    """ Exécuté une seule fois à l'initialisation du processus worker Celery.
    On crée une boucle d'événements unique et on instancie l'Orchestrateur.
    """
    global _loop, _orchestrator
    logger.info("Initialisation de la boucle d'événements asyncio globale pour le Worker.")
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    
    # L'orchestrateur est instancié UNE SEULE FOIS par worker et garde ses connexions chaudes
    _orchestrator = Orchestrator()


@worker_process_shutdown.connect
def shutdown_worker_process(**kwargs):
    """ Exécuté à la fermeture du worker. On nettoie proprement les ressources. """
    global _loop
    if _loop and _loop.is_running():
        _loop.close()
    logger.info("Boucle d'événements asyncio globale fermée.")


def _chunk_whatsapp_body(body: str, limit: int = _TWILIO_SOFT_LIMIT) -> List[str]:
    """Split outgoing message into Twilio-compliant chunks (1600 chars)."""
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
):
    """Point d'entrée worker : exécute la coroutine dans la boucle persistante."""
    global _loop, _orchestrator

    if _loop is None or _orchestrator is None:
        raise RuntimeError("Le worker Celery n'a pas été initialisé correctement (boucle/orchestrateur manquant).")

    async def _run():
        try:
            resolved_type = workspace_type
            forced = bool(force_role)
            if not resolved_type and role:
                resolved_type = "buyer" if role.upper() == "BUYER" else "producer"
                forced = True
            
            # Utilisation de l'instance d'orchestrateur partagée
            return await _orchestrator.handle(
                phone_number,
                user_query,
                workspace_type=resolved_type,
                force_role=forced,
            )
        finally:
            try:
                await close_db()
            except Exception as exc:
                logger.warning("close_db failed: %s", exc)

    try:
        # ◄ CORRECTION ICI : Au lieu de asyncio.run(), on pousse la coroutine dans la boucle existante
        result = _loop.run_until_complete(_run())
    except Exception as e:
        logger.error("Erreur orchestrateur: %s", e)
        raise

    final_text = result.get("final_response", "Je n'ai pas pu générer de réponse.")
    account_sid = str(settings.TWILIO_ACCOUNT_SID or "").strip()
    auth_token = str(settings.TWILIO_AUTH_TOKEN or "").strip()
    from_number = str(settings.TWILIO_WHATSAPP_NUMBER or "").strip()
    if not account_sid or not auth_token or not from_number:
        logger.error("Twilio configuration incomplete; cannot send WhatsApp response")
        raise RuntimeError("Twilio configuration incomplete")

    client = Client(account_sid, auth_token)
    chunks = _chunk_whatsapp_body(str(final_text))

    try:
        last_sid = None
        for chunk in chunks:
            message = client.messages.create(
                from_=from_number,
                to=f"whatsapp:{phone_number}",
                body=chunk,
            )
            last_sid = message.sid
        return {"status": "message_sent", "sid": last_sid, "chunks": len(chunks)}
    except Exception as e:
        logger.error("Erreur Twilio : %s", e)
        raise