from __future__ import annotations

import asyncio
import atexit
import logging
from asyncio import Runner
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Coroutine, Optional, TypeVar

logger = logging.getLogger("MCP.Core.Utils")

_T = TypeVar("_T")

_LIFECYCLE_POOL: ThreadPoolExecutor | None = None


# ---------------------------------------------------------------------------
# Masquage POUR LES LOGS uniquement
# ---------------------------------------------------------------------------
# N'affecte JAMAIS les données transmises aux outils ou renvoyées à l'agent :
# uniquement ce qui part dans les fichiers de log. C'est volontairement séparé
# de `security.SENSITIVE_COLUMNS` (qui masque des RÉSULTATS d'outils renvoyés à
# l'agent) — y ajouter les clés de téléphone masquerait le numéro dont l'agent
# a réellement besoin en aval (chargement de profil, etc.) et casserait le flux.
#
# Motif : `AgriDBMCPServer.call_tool` et `_log_call` journalisaient les
# arguments BRUTS en INFO — donc, pour `verify_delivery_otp`, le code de
# livraison à 4 chiffres qui débloque les fonds séquestrés (escrow Paydunya)
# se retrouvait en clair dans les logs, aux côtés du numéro de téléphone du
# producteur. Quiconque a accès aux logs pouvait valider une livraison.

# Redaction TOTALE : aucune de ces valeurs n'a d'intérêt de debug.
_LOG_REDACT_KEYS = frozenset(
    {
        "otp",
        "otp_code",
        "delivery_code",
        "delivery_otp",
        "password",
        "password_hash",
        "hashed_password",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "secret",
        "private_key",
        "credit_card",
        "ssn",
    }
)

# Masquage PARTIEL (4 derniers caractères conservés) : un numéro tronqué reste
# corrélable entre deux lignes de log pour le debug, sans exposer l'identité.
_LOG_PARTIAL_KEYS = frozenset(
    {
        "phone",
        "user_phone",
        "producer_phone",
        "buyer_phone",
        "customer_phone",
        "phone_number",
        "_caller_phone",
        "recipient_phone",
    }
)

_LOG_MAX_DEPTH = 4


def mask_log_value(key: str, value: Any) -> Any:
    """Masque une valeur selon la sensibilité de sa clé (usage LOG uniquement)."""
    key_norm = str(key).strip().lower()
    if key_norm in _LOG_REDACT_KEYS:
        return "***"
    if key_norm in _LOG_PARTIAL_KEYS:
        raw = str(value or "")
        return f"***{raw[-4:]}" if len(raw) >= 4 else "***"
    return value


def mask_log_args(payload: Any, _depth: int = 0) -> Any:
    """Copie défensive de *payload* avec les champs sensibles masqués.

    Récursif (dicts et listes imbriqués — les outils reçoivent souvent une
    enveloppe ``data={...}``), borné en profondeur pour qu'une structure
    cyclique ou anormalement profonde ne puisse jamais faire boucler le
    logging lui-même.
    """
    if _depth >= _LOG_MAX_DEPTH:
        return "..."
    if isinstance(payload, dict):
        masked: dict[str, Any] = {}
        for key, value in payload.items():
            replaced = mask_log_value(key, value)
            # Une clé sensible est déjà réduite à "***"/"***1234" : ne pas
            # redescendre dedans. Sinon on continue la récursion normalement.
            if replaced is value:
                replaced = mask_log_args(value, _depth + 1)
            masked[key] = replaced
        return masked
    if isinstance(payload, (list, tuple)):
        return [mask_log_args(item, _depth + 1) for item in payload]
    return payload


def _get_lifecycle_pool() -> ThreadPoolExecutor:
    global _LIFECYCLE_POOL
    if _LIFECYCLE_POOL is None:
        _LIFECYCLE_POOL = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="mcp-sync-bridge"
        )
        atexit.register(_LIFECYCLE_POOL.shutdown, wait=False)
    return _LIFECYCLE_POOL


def _run_with_runner(coro: Coroutine[Any, Any, _T]) -> _T:
    with Runner() as runner:
        return runner.run(coro)


def run_coro_blocking(
    coro: Coroutine[Any, Any, _T], *, timeout: Optional[float] = None
) -> _T:
    """Exécute une coroutine depuis du code SYNCHRONE, sans imbriquer d'event loop.

    ⚠️ DANGER D'AFFINITÉ DE LOOP (asyncpg) :
    Les connexions asyncpg sont liées à l'event loop qui les a créées. Si cette
    fonction est appelée DEPUIS un loop déjà actif, elle exécute la coroutine sur
    un NOUVEAU loop (dans un thread dédié) — toute connexion DB obtenue du pool
    partagé lèvera alors « Future attached to a different loop ».

    → RÈGLE : ne PAS router de travail DB via ce helper depuis un contexte async.
      Utilisez les méthodes `async` directement. Ce helper est réservé aux
      véritables appelants synchrones (pas de loop en cours), typiquement le
      démarrage/arrêt de serveur et les ponts sync legacy sans I/O DB.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run_with_runner(coro)

    logger.debug(
        "run_coro_blocking appelé depuis un event loop actif → offload thread "
        "(éviter tout I/O DB asyncpg par ce chemin)."
    )
    future = _get_lifecycle_pool().submit(_run_with_runner, coro)
    return future.result(timeout=timeout)
