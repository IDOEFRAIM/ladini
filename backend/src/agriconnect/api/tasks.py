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

# On définit ces variables au niveau global, elles seront initialisées par le signal
_loop: Optional[asyncio.AbstractEventLoop] = None
_orchestrator: Optional[Orchestrator] = None

_TWILIO_SOFT_LIMIT = 1500
_TWILIO_DISCLAIMER = " (Détails complets disponibles sur votre dashboard)"


# --- Initialisation de la boucle d'événements au démarrage du Worker ---

async def _warmup_db() -> None:
    """Ouvre une connexion réelle pour amorcer le pool + handshake SSL.

    Sans ce warm-up (et avec l'ancien ``close_db()`` par tâche), le TOUT premier
    message payait la connexion à froid vers DigitalOcean (SSL + DNS + pool),
    ce qui pouvait dépasser le timeout agent → réponse d'échec. Le 2ᵉ message
    trouvait le pool tiède et réussissait. On amorce donc le pool une fois au
    démarrage du worker, et on le garde chaud (voir suppression du close_db
    par tâche).
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
        # Ne pas bloquer le démarrage du worker : la 1ʳᵉ tâche ré-essaiera.
        logger.warning("Warm-up DB échoué (non bloquant) : %s", exc)


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

    # Amorce le pool DB SUR la boucle persistante du worker pour que la 1ʳᵉ
    # tâche ne paie pas la connexion à froid (cause du bug "1er message échoue").
    try:
        _loop.run_until_complete(_warmup_db())
    except Exception as exc:
        logger.warning("Warm-up DB au démarrage ignoré : %s", exc)


@worker_process_shutdown.connect
def shutdown_worker_process(**kwargs):
    """ Exécuté à la fermeture du worker. On nettoie proprement les ressources. """
    global _loop
    # Ferme le pool DB proprement UNE fois, à l'arrêt du worker (et non par tâche).
    try:
        if _loop and not _loop.is_closed():
            _loop.run_until_complete(close_db())
    except Exception as exc:
        logger.warning("close_db au shutdown échoué : %s", exc)
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


def send_whatsapp_message(
    client: Client,
    from_: str,
    to: str,
    body: str,
    *,
    content_sid: Optional[str] = None,
    content_vars: Optional[Dict[str, Any]] = None,
):
    """Envoi WhatsApp unifié : Content API (boutons/listes) OU texte simple.

    - ``content_sid`` fourni ET ``TWILIO_INTERACTIVE_ENABLED`` vrai → envoi
      interactif via la Content API Twilio (quick-reply / list-picker
      pré-créés). ``content_vars`` remplit les variables {{n}} du template.
    - Sinon → repli universel sur ``body`` texte (fonctionne partout, sandbox
      inclus). C'est le comportement historique, jamais cassé.

    Rappels Twilio (≠ Meta Cloud API) :
      * pas de JSON `interactive` inline — tout passe par un ContentSid ;
      * quick-reply = 3 boutons max ; list-picker = 10 lignes max, STRUCTURE
        FIGÉE dans le template (on ne paramètre que les valeurs, pas le nombre
        de lignes → convient aux tunnels binaires/fixes, pas aux catalogues
        dynamiques, qui restent en texte numéroté + bypass LLM).
    """
    interactive = bool(
        content_sid
        and getattr(settings, "TWILIO_INTERACTIVE_ENABLED", False)
    )
    if interactive:
        try:
            return client.messages.create(
                from_=from_,
                to=to,
                content_sid=content_sid,
                content_variables=json.dumps(content_vars or {}, ensure_ascii=False),
            )
        except Exception as exc:
            # Repli gracieux : un échec d'envoi interactif (template invalide,
            # sender non prod…) ne doit JAMAIS priver l'utilisateur de la
            # réponse — on renvoie le texte.
            logger.warning("Envoi interactif Twilio échoué (%s) → repli texte.", exc)

    return client.messages.create(from_=from_, to=to, body=body)


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
):
    """Point d'entrée worker : exécute la coroutine dans la boucle persistante.

    ``interactive_id`` : payload d'un clic WhatsApp (bouton/liste) transmis par
    le webhook. Non vide → l'agent court-circuite l'interprétation LLM.
    """
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
                interactive_id=interactive_id,
            )
        finally:
            # NE PAS fermer le pool DB ici : le worker garde ses connexions
            # chaudes entre les tâches (le close_db par tâche était la cause du
            # "1er message échoue, 2ᵉ marche"). Le pool est fermé au shutdown.
            pass

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
    to_addr = f"whatsapp:{phone_number}"

    try:
        # --- Rendu interactif (boutons de confirmation) si activé + template ---
        interactive = result.get("interactive") or {}
        confirm_sid = str(getattr(settings, "TWILIO_CONFIRM_CONTENT_SID", "") or "").strip()
        if (
            interactive.get("kind") == "confirm"
            and getattr(settings, "TWILIO_INTERACTIVE_ENABLED", False)
            and confirm_sid
        ):
            # Confirmation binaire → 2 boutons quick-reply (payloads CONFIRM /
            # REJECT côté template). Le récap passe en variable {{1}}. Un clic
            # revient via le webhook et court-circuite le LLM (voir étape A).
            message = send_whatsapp_message(
                client, from_number, to_addr,
                body=str(final_text),
                content_sid=confirm_sid,
                content_vars={"1": str(final_text)[:1500]},
            )
            return {"status": "message_sent", "sid": message.sid, "interactive": "confirm"}

        # --- Sinon : texte (chunké si long), via le même point d'envoi ---
        chunks = _chunk_whatsapp_body(str(final_text))
        last_sid = None
        for chunk in chunks:
            message = send_whatsapp_message(client, from_number, to_addr, body=chunk)
            last_sid = message.sid
        return {"status": "message_sent", "sid": last_sid, "chunks": len(chunks)}
    except Exception as e:
        logger.error("Erreur Twilio : %s", e)
        raise