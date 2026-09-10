"""Classification d'erreur — §15 du brief.

Étend (ne duplique pas) `core/get_llm.py::_is_rate_limit_error` /
`_is_bedrock_throttling_error`, qui restent la source de vérité pour la
détection 429/throttling. Ce module ajoute la détection CONFIG (auth, modèle
inexistant/non supporté) et le repli par défaut sur APPLICATION — jamais
l'inverse (un bug applicatif ne doit jamais compter comme une panne LLM,
§15 "APPLICATION → NE PAS marquer automatiquement le modèle comme malade").
"""

from __future__ import annotations

from ladini.graphs.agents.market_coach.llm_gateway.types import ErrorClass

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
