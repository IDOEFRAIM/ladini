"""Sanitisation centralisée des erreurs — barrière anti-fuite d'informations.

Axe SÉCURITÉ (défense en profondeur) : aucune trace technique (nom de table,
de contrainte, dialecte SQL, stack) ne doit atteindre l'agent IA — qui peut
la relayer mot pour mot à l'utilisateur final via WhatsApp.

Deux surfaces de fuite identifiées à l'audit :
  1. Les mixins qui font `return {"status": "error", "message": str(e)}` sur un
     `except Exception` large (≈40 occurrences) — `e` peut être une
     IntegrityError PostgreSQL exposant « violates unique constraint
     'bids_auction_producer_unique' ».
  2. Les exceptions RE-LEVÉES par le dispatcher (`d.py`) puis interpolées par
     le wrapper MCP (`f"...: {exc}"`) → renvoyées telles quelles à l'agent.

Ce module fournit le point de contrôle unique pour neutraliser les deux.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("Ladini.DatabaseService.Errors")

# Message générique renvoyé à l'agent pour toute erreur NON métier (technique).
_GENERIC_SAFE_MESSAGE = (
    "Une erreur technique est survenue lors de l'accès aux données. "
    "Veuillez réessayer dans un instant."
)


class BusinessRuleException(Exception):
    """Échec métier explicite, levé volontairement par les mixins.

    Remplace le pattern `return {"status": "error", "message": ...}` : au lieu
    de faire porter au décorateur `@transactional` la connaissance du domaine
    (parser un dict de retour pour savoir s'il faut rollback), la couche
    métier lève CETTE exception. `@transactional` n'a besoin de rien savoir
    d'autre que "une exception a été levée" — rollback automatique via le flux
    d'exécution Python normal, sans inspection de contenu applicatif.

    `reason` est un code machine optionnel (ex: "insufficient_stock",
    "not_draft") et `**extra` transporte des données structurées additionnelles
    (ex: `details=[...]`, `fallback=[...]`) que l'appelant (agent/API) peut
    exploiter pour enrichir sa réponse, sans que `@transactional` y touche.
    """

    def __init__(self, message: str, *, reason: str | None = None, **extra) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.extra = extra


# Exceptions dont le message est du CONTENU MÉTIER volontaire, sûr à exposer
# ET dont le TYPE doit traverser intact (l'agent/l'appelant peut vouloir
# discriminer dessus, ex: `except ValueError` pour une réparation de slot).
#   - BusinessRuleException : échecs métier explicites levés par les mixins
#     (stock insuffisant, statut invalide, ressource introuvable...).
#   - ValueError : validations d'entrée (clean_text, positive_float, contrôles
#     d'appartenance/quantité négative/prix nul...).
#   - KeyError   : accès à un champ métier attendu mais absent d'un payload
#     agent (ex: dict de réponse MCP mal formé côté appelant).
_SAFE_BUSINESS_EXCEPTIONS: tuple[type[BaseException], ...] = (
    BusinessRuleException,
    ValueError,
    KeyError,
)

# Fragments techniques qui ne doivent JAMAIS transiter vers l'agent, même
# nichés dans un message par ailleurs anodin (filet de sécurité défensif).
_LEAK_MARKERS: tuple[str, ...] = (
    "constraint",
    "violates",
    "psycopg",
    "asyncpg",
    "sqlalchemy",
    "traceback",
    "relation ",
    "column ",
    "duplicate key",
    "syntax error",
    "foreign key",
    "null value",
    "select ",
    "insert ",
    "update ",
    "schema",
    "pg_",
    "detail:",
    "hint:",
    "line ",
    "  file ",
)


class SafeDatabaseError(Exception):
    """Exception dont la représentation textuelle est TOUJOURS sûre à exposer.

    Le dispatcher lève CELLE-CI (jamais l'exception DB brute) : la vraie cause
    est journalisée côté serveur avec `exc_info`, l'agent ne voit que
    `safe_message`.
    """

    def __init__(self, safe_message: str = _GENERIC_SAFE_MESSAGE) -> None:
        self.safe_message = safe_message
        super().__init__(safe_message)


def _looks_technical(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _LEAK_MARKERS)


def is_safe_business_exception(exc: BaseException) -> bool:
    """True si `exc` est une exception métier/validation dont le TYPE et le
    MESSAGE peuvent traverser intacts jusqu'à l'agent — filet ultime :
    même une `ValueError`/`KeyError` est disqualifiée si son message contient
    malgré tout un marqueur technique (ex: message d'erreur ORM re-levé tel
    quel dans un `raise ValueError(str(orm_exc))` maladroit).
    """
    if isinstance(exc, SafeDatabaseError):
        return True
    if isinstance(exc, _SAFE_BUSINESS_EXCEPTIONS):
        return not _looks_technical(str(exc))
    return False


def sanitize_error_message(exc: BaseException | str, *, context: str = "") -> str:
    """Retourne un message d'erreur SÛR à renvoyer à l'agent.

    - Message métier volontaire (ValueError, KeyError, SafeDatabaseError) →
      conservé tel quel — l'agent doit comprendre PRÉCISÉMENT pourquoi l'action
      a échoué (quantité négative, prix nul, produit introuvable...).
    - Tout le reste (erreurs DB/ORM/techniques : IntegrityError, asyncpg,
      timeouts...) → message générique, la vraie cause étant journalisée
      côté serveur avec la stack complète.
    - Filet ultime : même un message « métier » contenant un marqueur technique
      est remplacé par le générique (cf. `is_safe_business_exception`).
    """
    if isinstance(exc, SafeDatabaseError):
        return exc.safe_message

    if isinstance(exc, _SAFE_BUSINESS_EXCEPTIONS):
        msg = str(exc).strip()
        if msg and not _looks_technical(msg):
            return msg
        # Un ValueError/KeyError qui contient malgré tout du technique → log + générique.
        logger.warning(
            "Message métier contenant un marqueur technique masqué (%s).", context
        )
        return _GENERIC_SAFE_MESSAGE

    # Exception technique (driver/ORM/infrastructure) : on ne divulgue JAMAIS
    # str(exc) — seule la stack serveur (exc_info) porte le détail réel.
    logger.error(
        "Erreur technique masquée pour l'agent (%s): %r",
        context or "?",
        exc,
        exc_info=True,
    )
    return _GENERIC_SAFE_MESSAGE


def safe_error_dict(exc: BaseException | str, *, context: str = "", **extra) -> dict:
    """Construit un dict d'erreur standard, message garanti sûr.

    Remplace le pattern à risque `return {"status": "error", "message": str(e)}`.
    """
    payload = {
        "status": "error",
        "message": sanitize_error_message(exc, context=context),
    }
    payload.update(extra)
    return payload


def scrub_error_result(result: dict) -> dict:
    """Passe finale sur un dict d'erreur déjà construit par un mixin.

    Utilisé par le dispatcher comme filet : si un mixin a laissé filer un
    `message` technique, on le neutralise AVANT de le renvoyer. Idempotent et
    non destructif sur les messages sûrs.
    """
    if not isinstance(result, dict):
        return result
    msg = result.get("message")
    if isinstance(msg, str) and msg and _looks_technical(msg):
        logger.warning(
            "Message d'erreur technique neutralisé par le dispatcher: %r", msg
        )
        result = dict(result)
        result["message"] = _GENERIC_SAFE_MESSAGE
    return result


__all__ = [
    "SafeDatabaseError",
    "BusinessRuleException",
    "is_safe_business_exception",
    "sanitize_error_message",
    "safe_error_dict",
    "scrub_error_result",
]
