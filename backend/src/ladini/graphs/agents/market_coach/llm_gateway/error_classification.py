"""Classification d'erreur — §15 du brief.

Étend (ne duplique pas) `core/get_llm.py::_is_rate_limit_error` /
`_is_bedrock_throttling_error`, qui restent la source de vérité pour la
détection 429/throttling. Ce module ajoute la détection CONFIG (auth, modèle
inexistant/non supporté) et le repli par défaut sur APPLICATION — jamais
l'inverse (un bug applicatif ne doit jamais compter comme une panne LLM,
§15 "APPLICATION → NE PAS marquer automatiquement le modèle comme malade").
"""

from __future__ import annotations

import random
from typing import Optional

from ladini.graphs.agents.market_coach.llm_gateway.types import (
    ErrorClass,
    LLMFailureKind,
)

# Fragments observés en direct cette session (2026-09-02) contre la
# passerelle bedrock-mantle — ex: `anthropic.claude-haiku-4-5` renvoyait
# HTTP 400 "does not support the '/v1/chat/completions' API". Complété au
# fil des incidents réels, jamais deviné à l'avance.
_CONFIG_ERROR_MESSAGE_FRAGMENTS = (
    "does not support",
    "model not found",
    "unsupported model",
    "invalid model",
    "invalid_request_error",
    "unknown model",
    "no such model",
)


def classify_llm_error(exc: BaseException) -> ErrorClass:
    """Décide si `exc` doit compter pour le disjoncteur (TRANSIENT), désactiver
    le candidat (CONFIG), ou n'affecter aucune santé (APPLICATION)."""
    # 1. Erreurs de programmation / parsing — jamais une panne LLM.
    if isinstance(exc, (ValueError, TypeError, KeyError, AttributeError)):
        # Une ValueError peut aussi venir d'un SDK (rare) — mais dans ce repo,
        # les erreurs SDK exposent toutes un `status_code`/type dédié (voir
        # ci-dessous) ; une ValueError/TypeError nue vient du code appelant.
        return ErrorClass.APPLICATION
    try:
        import json

        if isinstance(exc, json.JSONDecodeError):
            return ErrorClass.APPLICATION
    except Exception:
        pass
    try:
        from pydantic import ValidationError as _PydanticValidationError

        if isinstance(exc, _PydanticValidationError):
            return ErrorClass.APPLICATION
    except ImportError:
        pass

    # 2. Timeout — toujours TRANSIENT, quel que soit le SDK.
    if isinstance(exc, (TimeoutError,)):
        return ErrorClass.TRANSIENT
    try:
        import asyncio

        if isinstance(exc, asyncio.TimeoutError):
            return ErrorClass.TRANSIENT
    except Exception:
        pass
    try:
        import httpx

        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError)):
            return ErrorClass.TRANSIENT
    except ImportError:
        pass

    # 3. SDK OpenAI/Groq (même protocole) — status_code explicite.
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        if status_code in (401, 403):
            return ErrorClass.CONFIG
        if status_code == 404 or status_code == 400:
            # 400/404 est ambigu par défaut (peut être un bug de payload OU un
            # modèle inconnu) — on affine avec le message avant de conclure.
            if _looks_like_config_error(exc):
                return ErrorClass.CONFIG
            return ErrorClass.APPLICATION
        if status_code == 429 or 500 <= status_code < 600:
            return ErrorClass.TRANSIENT

    try:
        from openai import APIConnectionError as _OpenAIConn
        from openai import APITimeoutError as _OpenAITimeout

        if isinstance(exc, (_OpenAITimeout, _OpenAIConn)):
            return ErrorClass.TRANSIENT
    except ImportError:
        pass
    try:
        from openai import AuthenticationError as _OpenAIAuth
        from openai import NotFoundError as _OpenAINotFound
        from openai import PermissionDeniedError as _OpenAIPerm

        if isinstance(exc, (_OpenAIAuth, _OpenAIPerm)):
            return ErrorClass.CONFIG
        if isinstance(exc, _OpenAINotFound):
            return ErrorClass.CONFIG
    except ImportError:
        pass
    try:
        from groq import AuthenticationError as _GroqAuth
        from groq import PermissionDeniedError as _GroqPerm

        if isinstance(exc, (_GroqAuth, _GroqPerm)):
            return ErrorClass.CONFIG
    except ImportError:
        pass

    # 4. Bedrock natif (botocore) — ThrottlingException/AccessDenied.
    try:
        from botocore.exceptions import ClientError as _BotoClientError

        if isinstance(exc, _BotoClientError):
            code = (exc.response or {}).get("Error", {}).get("Code", "")
            if code == "ThrottlingException":
                return ErrorClass.TRANSIENT
            if code in ("AccessDeniedException", "UnrecognizedClientException", "ValidationException"):
                return ErrorClass.CONFIG
    except ImportError:
        pass

    # 5. Réseau générique — connexion réinitialisée, DNS, etc.
    if isinstance(exc, (ConnectionError, OSError)):
        return ErrorClass.TRANSIENT

    if _looks_like_config_error(exc):
        return ErrorClass.CONFIG

    # Défaut prudent : ne compte PAS comme une panne LLM tant que ce n'est
    # pas prouvé — évite qu'un bug applicatif fasse croire à une panne
    # provider (§15). Le disjoncteur ne s'ouvre donc jamais sur une erreur
    # non reconnue ; elle reste visible en log/Langfuse pour investigation.
    return ErrorClass.APPLICATION


def _looks_like_config_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(fragment in message for fragment in _CONFIG_ERROR_MESSAGE_FRAGMENTS)


def _looks_like_context_too_large(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(fragment in message for fragment in _CONTEXT_TOO_LARGE_FRAGMENTS)


# =====================================================================
# Incrément G (2026-09-13, "Production LLM Hardening") — classification
# FINE (spec §4) + politique de retry same-candidate (spec §5/§6).
#
# `classify_llm_failure_kind` réutilise EXACTEMENT les mêmes signaux que
# `classify_llm_error` ci-dessus (status_code, types SDK, fragments de
# message) — pas une seconde détection divergente — mais retourne une
# valeur plus fine, nécessaire pour décider POURQUOI retenter (backoff 429
# différent d'un simple timeout) plutôt que SI le disjoncteur doit compter
# l'échec (question à laquelle `ErrorClass`/`classify_llm_error` répond déjà
# et continue de répondre, inchangé — voir `error_class_for_kind` pour la
# correspondance).
# =====================================================================

_CONTEXT_TOO_LARGE_FRAGMENTS = (
    "context_length_exceeded",
    "maximum context length",
    "context too large",
    "request too large",
    "reduce the length",
)

#: Kinds qui méritent UNE tentative supplémentaire sur le MÊME candidat
#: avant de basculer sur le suivant (spec §5) — jamais BAD_REQUEST/AUTH/
#: CONFIG/CONTEXT_TOO_LARGE (spec §5 : "pas de retry, pas de fallback
#: aveugle" pour ces derniers — un problème de prompt/contrat/credentials ne
#: se résout jamais en réessayant identique).
_RETRYABLE_SAME_CANDIDATE_KINDS = frozenset(
    {
        LLMFailureKind.RATE_LIMIT,
        LLMFailureKind.TIMEOUT,
        LLMFailureKind.PROVIDER_5XX,
        LLMFailureKind.CONNECTION,
    }
)

#: Correspondance FINE → 3 catégories du disjoncteur (spec §12 : UNKNOWN
#: sémantique et repair JSON n'entrent JAMAIS dans cette fonction — ils ne
#: sont pas des exceptions levées par le Gateway, voir la docstring de
#: `new_task_micro.py`/`structured_action_micro.py` : le disjoncteur ne les
#: voit structurellement jamais).
_ERROR_CLASS_BY_KIND = {
    LLMFailureKind.RATE_LIMIT: ErrorClass.TRANSIENT,
    LLMFailureKind.TIMEOUT: ErrorClass.TRANSIENT,
    LLMFailureKind.PROVIDER_5XX: ErrorClass.TRANSIENT,
    LLMFailureKind.CONNECTION: ErrorClass.TRANSIENT,
    LLMFailureKind.AUTH: ErrorClass.CONFIG,
    LLMFailureKind.CONFIG: ErrorClass.CONFIG,
    LLMFailureKind.BAD_REQUEST: ErrorClass.APPLICATION,
    LLMFailureKind.CONTEXT_TOO_LARGE: ErrorClass.APPLICATION,
    LLMFailureKind.INVALID_RESPONSE: ErrorClass.APPLICATION,
    LLMFailureKind.CIRCUIT_OPEN: ErrorClass.APPLICATION,
    LLMFailureKind.APPLICATION: ErrorClass.APPLICATION,
    LLMFailureKind.UNKNOWN: ErrorClass.APPLICATION,
}


def classify_llm_failure_kind(exc: BaseException) -> LLMFailureKind:
    """Classification fine — voir le module docstring. Ne décide PAS seule
    du comportement disjoncteur (`error_class_for_kind` fait ce pont) ; sert
    la politique de retry (`should_retry_same_candidate`/`backoff_seconds`)
    et l'observabilité Langfuse (`failure_kind`)."""
    # JSONDecodeError/ValidationError AVANT le catch-all ValueError ci-dessous
    # — `json.JSONDecodeError` EST une sous-classe de `ValueError` en Python,
    # donc l'ordre inverse ferait toujours gagner APPLICATION avant même
    # d'atteindre ces branches plus spécifiques (bug réel détecté par test).
    try:
        import json

        if isinstance(exc, json.JSONDecodeError):
            return LLMFailureKind.INVALID_RESPONSE
    except Exception:
        pass
    try:
        from pydantic import ValidationError as _PydanticValidationError

        if isinstance(exc, _PydanticValidationError):
            return LLMFailureKind.INVALID_RESPONSE
    except ImportError:
        pass

    if isinstance(exc, (ValueError, TypeError, KeyError, AttributeError)):
        return LLMFailureKind.APPLICATION

    if isinstance(exc, (TimeoutError,)):
        return LLMFailureKind.TIMEOUT
    try:
        import asyncio

        if isinstance(exc, asyncio.TimeoutError):
            return LLMFailureKind.TIMEOUT
    except Exception:
        pass
    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return LLMFailureKind.TIMEOUT
        if isinstance(exc, (httpx.ConnectError, httpx.ReadError)):
            return LLMFailureKind.CONNECTION
    except ImportError:
        pass

    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        if status_code == 429:
            return LLMFailureKind.RATE_LIMIT
        if status_code in (401, 403):
            return LLMFailureKind.AUTH
        if status_code == 413:
            return LLMFailureKind.CONTEXT_TOO_LARGE
        if status_code in (404, 400):
            if _looks_like_context_too_large(exc):
                return LLMFailureKind.CONTEXT_TOO_LARGE
            if _looks_like_config_error(exc):
                return LLMFailureKind.CONFIG
            return LLMFailureKind.BAD_REQUEST
        if 500 <= status_code < 600:
            return LLMFailureKind.PROVIDER_5XX

    try:
        from openai import APIConnectionError as _OpenAIConn
        from openai import APITimeoutError as _OpenAITimeout

        if isinstance(exc, _OpenAITimeout):
            return LLMFailureKind.TIMEOUT
        if isinstance(exc, _OpenAIConn):
            return LLMFailureKind.CONNECTION
    except ImportError:
        pass
    try:
        from openai import AuthenticationError as _OpenAIAuth
        from openai import NotFoundError as _OpenAINotFound
        from openai import PermissionDeniedError as _OpenAIPerm

        if isinstance(exc, (_OpenAIAuth, _OpenAIPerm)):
            return LLMFailureKind.AUTH
        if isinstance(exc, _OpenAINotFound):
            return LLMFailureKind.CONFIG
    except ImportError:
        pass
    try:
        from groq import AuthenticationError as _GroqAuth
        from groq import PermissionDeniedError as _GroqPerm

        if isinstance(exc, (_GroqAuth, _GroqPerm)):
            return LLMFailureKind.AUTH
    except ImportError:
        pass

    try:
        from botocore.exceptions import ClientError as _BotoClientError

        if isinstance(exc, _BotoClientError):
            code = (exc.response or {}).get("Error", {}).get("Code", "")
            if code == "ThrottlingException":
                return LLMFailureKind.RATE_LIMIT
            if code in ("AccessDeniedException", "UnrecognizedClientException"):
                return LLMFailureKind.AUTH
            if code == "ValidationException":
                return LLMFailureKind.CONFIG
    except ImportError:
        pass

    if isinstance(exc, (ConnectionError, OSError)):
        return LLMFailureKind.CONNECTION

    if _looks_like_context_too_large(exc):
        return LLMFailureKind.CONTEXT_TOO_LARGE
    if _looks_like_config_error(exc):
        return LLMFailureKind.CONFIG

    return LLMFailureKind.UNKNOWN


def error_class_for_kind(kind: LLMFailureKind) -> ErrorClass:
    return _ERROR_CLASS_BY_KIND.get(kind, ErrorClass.APPLICATION)


def should_retry_same_candidate(kind: LLMFailureKind) -> bool:
    """Spec §5 : timeout/5xx/connexion/429 méritent UNE tentative de plus
    sur le MÊME candidat avant de basculer — jamais BAD_REQUEST/AUTH/
    CONFIG/CONTEXT_TOO_LARGE (un problème de prompt/contrat/credentials ne
    se résout jamais en réessayant identique, spec §5)."""
    return kind in _RETRYABLE_SAME_CANDIDATE_KINDS


def retry_after_seconds(exc: BaseException) -> Optional[float]:
    """Extrait un `Retry-After` HTTP quand le SDK l'expose — jamais deviné.
    Groq/OpenAI exposent `exc.response.headers` sur les erreurs API ; on
    reste défensif (tout accès peut échouer selon la version du SDK)."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        headers = getattr(exc, "headers", None)
    if not headers:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def backoff_seconds(
    kind: LLMFailureKind, attempt: int, *, retry_after: Optional[float] = None
) -> float:
    """Délai avant de retenter le MÊME candidat (spec §5/§38) — jamais un
    `time.sleep` bloquant : l'appelant (`gateway.py`) doit `await
    asyncio.sleep(...)` avec cette valeur, jamais bloquer le thread SDK plus
    longtemps que nécessaire (voir `core/get_llm.py::get_groq_sdk`,
    `max_retries=0` — incident documenté : un retry bloquant DANS le thread
    SDK ne peut pas être annulé par `asyncio.wait_for`). RATE_LIMIT honore
    `Retry-After` quand le provider le fournit (capé à 10s pour ne jamais
    consommer tout le budget d'un profil sur une seule attente) ; sinon
    backoff exponentiel court + jitter plein (0 à la valeur calculée) pour
    désynchroniser les workers Celery concurrents (spec §38 : "pas de retry
    storm")."""
    if kind == LLMFailureKind.RATE_LIMIT and retry_after is not None:
        return min(10.0, retry_after)
    base = 0.3 * (2 ** max(0, attempt - 1))
    capped = min(2.0, base)
    return random.uniform(0.0, capped)
