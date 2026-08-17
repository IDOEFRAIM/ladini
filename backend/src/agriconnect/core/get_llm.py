import logging
import os
from typing import Any, Optional

logger = logging.getLogger("agriconnect.core.get_llm")

# Module-level singleton cache
_LLM_SINGLETON: Optional[Any] = None
_GROQ_SDK_SINGLETON: Optional[Any] = None


def get_groq_sdk(force_refresh: bool = False) -> Any:
    """Construit (et met en cache) le SDK Groq brut — sans dépendance RAG.

    Historiquement délégué à ``futur.rag.components.get_groq_sdk``, ce qui
    tirait ``faiss`` + ``llama_index`` (imports niveau module, plusieurs
    secondes) dans le chemin LLM et provoquait le cold start « 1er message
    échoue, 2ᵉ marche ». Le RAG n'étant plus utilisé, on instancie le client
    Groq directement ici : l'import devient trivial.
    """
    global _GROQ_SDK_SINGLETON
    if not force_refresh and _GROQ_SDK_SINGLETON is not None:
        return _GROQ_SDK_SINGLETON

    from agriconnect.core.settings import settings

    provider = (getattr(settings, "LLM_PROVIDER", "groq") or "groq").strip().lower()
    if provider not in {"groq", "default", "auto"}:
        raise RuntimeError(
            "get_groq_sdk() n'est disponible que lorsque LLM_PROVIDER=groq (ou auto)."
        )

    api_key = settings.llm_api_key
    if not api_key:
        raise RuntimeError(
            "Aucune clé GROQ_API_KEY/AGRICONNECT_APIKEY n'est définie pour initialiser le SDK Groq."
        )

    try:
        from groq import Groq
    except ImportError as exc:
        raise RuntimeError(
            "Le package python 'groq' est requis pour instancier le SDK Groq.\n"
            "Installez-le via `pip install groq`."
        ) from exc

    # max_retries=0 (Phase 4) : le SDK Groq retry par défaut (2×) en honorant
    # le header Retry-After d'un 429 — vu en prod : 13s d'attente SDK à
    # l'INTÉRIEUR d'un seul essai, invisible pour asyncio.to_thread (le thread
    # bloquant ne peut pas être annulé une fois lancé). Avec 2 retries, le
    # pire cas dépasse 40s — bien au-delà des `asyncio.wait_for` (8-15s) posés
    # aux sites d'appel (routing.py, clarification.py, utils.py) : le timeout
    # abandonnait la coroutine pendant que le thread continuait à tourner
    # pour rien, ET convertissait un appel qui aurait fini par réussir en
    # UNKNOWN forcé. Un 429/5xx doit remonter IMMÉDIATEMENT comme exception —
    # les `except` de chaque site gèrent déjà le fallback proprement (log +
    # dégradation), plus vite et plus prévisible qu'un retry masqué.
    # `timeout=20.0` : filet réseau (connexion pendue) indépendant du retry.
    _GROQ_SDK_SINGLETON = Groq(api_key=api_key, max_retries=0, timeout=20.0)
    logger.info("Groq SDK initialisé et mis en cache (max_retries=0, timeout=20s)")
    return _GROQ_SDK_SINGLETON


def _is_rate_limit_error(exc: Exception) -> bool:
    try:
        from groq import RateLimitError

        if isinstance(exc, RateLimitError):
            return True
    except ImportError:
        pass
    status_code = getattr(exc, "status_code", None)
    return status_code == 429


def _fallback_model_for(requested_model: str) -> Optional[str]:
    """Modèle de repli à quota TPD séparé, ou None si aucun repli pertinent
    (déjà sur le modèle rapide, ou modèle demandé inconnu)."""
    try:
        from agriconnect.core.settings import settings

        fast_model = str(getattr(settings, "LLM_MODEL", "") or "")
    except Exception:
        fast_model = ""
    if not fast_model or requested_model == fast_model:
        return None
    return fast_model


class _NormalizedChatResponse:
    """Minimal wrapper to expose `.choices[0].message.content` as expected
    by the callers in the codebase.
    """

    def __init__(self, text: str):
        class _Msg:
            def __init__(self, content: str):
                self.content = content

        class _Choice:
            def __init__(self, content: str):
                self.message = _Msg(content)

        self.choices = [_Choice(text)]


class _GroqAdapter:
    """Adapter that normalizes a Groq SDK-like client to the interface
    expected by the rest of the codebase: `client.chat.completions.create(...)`
    returning an object with `.choices[0].message.content`.

    This adapter performs a lazy delegation and keeps imports local.
    """

    def __init__(self, raw_client: Any):
        self._raw = raw_client

    class _Chat:
        def __init__(self, parent: "_GroqAdapter"):
            self._parent = parent

        class _Completions:
            def __init__(self, parent: "_GroqAdapter"):
                self._parent = parent

            def create(self, **kwargs):
                # ── LLMOps : point d'interception UNIQUE de tous les appels Groq ──
                # Les 5 sites d'appel de production (routing.py, response_handlers.py,
                # clarification.py, slot_enrichment.py, utils.py onboarding) passent
                # TOUS par ici. On y capture modèle / prompt / réponse / latence /
                # tokens / erreurs pour Langfuse + Prometheus, sans toucher aux sites
                # d'appel (contrairement à l'idée de patcher cognitive.py, qui ne
                # fait AUCUN appel LLM). Import local + best-effort : jamais bloquant.
                import time as _t

                _model = str(kwargs.get("model") or "unknown")
                _messages = kwargs.get("messages")
                _t0 = _t.perf_counter()
                _err = None

                def _emit(output, usage):
                    try:
                        from agriconnect.core.telemetry import record_generation

                        record_generation(
                            model=_model,
                            messages=_messages,
                            output=output,
                            latency_s=_t.perf_counter() - _t0,
                            usage=usage,
                            error=_err,
                        )
                    except Exception:
                        pass

                def _usage_of(resp):
                    u = getattr(resp, "usage", None)
                    if u is None:
                        return None
                    return {
                        "prompt_tokens": getattr(u, "prompt_tokens", None),
                        "completion_tokens": getattr(u, "completion_tokens", None),
                    }

                def _text_of(resp):
                    try:
                        return resp.choices[0].message.content
                    except Exception:
                        return None

                # Try to call the underlying SDK with a couple of common shapes.
                client = self._parent._raw
                # Best-effort: try `client.chat.completions.create` if present
                try:
                    chat = getattr(client, "chat", None)
                    if (
                        chat
                        and hasattr(chat, "completions")
                        and hasattr(chat.completions, "create")
                    ):
                        try:
                            resp = chat.completions.create(**kwargs)
                        except Exception as call_exc:
                            # Repli automatique : un 429 (quota journalier épuisé
                            # côté Groq) sur le modèle de raisonnement ne doit PAS
                            # bloquer tout le pipeline (onboarding, interprétation
                            # d'intent) — chaque modèle Groq a son propre quota
                            # TPD séparé, donc un essai unique sur le modèle
                            # rapide (settings.LLM_MODEL) reste une dégradation
                            # fonctionnelle (moins fin, mais opérationnel) plutôt
                            # qu'un blocage total. On ne retente QUE sur
                            # RateLimitError, et seulement si un modèle
                            # différent est disponible — jamais sur les autres
                            # erreurs (auth, 5xx, timeout…) pour ne pas masquer
                            # un vrai incident ni doubler la latence pour rien.
                            is_rate_limit = _is_rate_limit_error(call_exc)
                            fallback_model = _fallback_model_for(_model)
                            if is_rate_limit and fallback_model:
                                logger.warning(
                                    "GROQ_RATE_LIMIT_FALLBACK | model=%s -> %s | %s",
                                    _model,
                                    fallback_model,
                                    call_exc,
                                )
                                fallback_kwargs = dict(kwargs)
                                fallback_kwargs["model"] = fallback_model
                                try:
                                    resp = chat.completions.create(**fallback_kwargs)
                                    _model = fallback_model
                                except Exception as fallback_exc:
                                    _err = (
                                        f"{type(fallback_exc).__name__}: {fallback_exc}"
                                    )
                                    _emit(None, None)
                                    raise fallback_exc
                            else:
                                _err = f"{type(call_exc).__name__}: {call_exc}"
                                _emit(None, None)
                                raise
                        # If the SDK already returns an object with choices[0].message.content,
                        # return it directly. Otherwise, try to coerce.
                        if hasattr(resp, "choices"):
                            _emit(_text_of(resp), _usage_of(resp))
                            return resp
                        # Fallback: coerce string-like responses
                        text = str(resp)
                        _emit(text, None)
                        return _NormalizedChatResponse(text)

                except Exception:
                    if _err is not None:
                        raise
                    pass

                # Next fallback: top-level `client.completions.create`
                try:
                    completions = getattr(client, "completions", None)
                    if completions and hasattr(completions, "create"):
                        resp = completions.create(**kwargs)
                        if hasattr(resp, "choices"):
                            return resp
                        return _NormalizedChatResponse(str(resp))
                except Exception:
                    pass

                # Last resort: return a normalized wrapper with an error message
                return _NormalizedChatResponse(
                    "<llm-error: adapter could not call underlying sdk>"
                )

        @property
        def completions(self):
            return _GroqAdapter._Chat._Completions(self._parent)

    @property
    def chat(self):
        return _GroqAdapter._Chat(self)


def get_llm(llm_client: Optional[Any] = None) -> Optional[Any]:
    """Return a normalized LLM client singleton.

    Resolution order:
    1. If `llm_client` provided, return it (but do not cache it globally).
    2. Return cached singleton if exists.
    3. If GROQ API key available (or settings indicate non-prod), try to build
       an adapter around the Groq SDK client obtained from
       `agriconnect.rag.components.get_groq_sdk()`.

    Behavior:
    - Lazy-loads the rag components module to avoid heavy imports at module
      import time.
    - In production (`ENV=production` or `SENTRY_ENVIRONMENT=production`),
      fail-fast when no GROQ key is present by raising RuntimeError.
    """
    global _LLM_SINGLETON

    if llm_client:
        return llm_client

    if _LLM_SINGLETON is not None:
        return _LLM_SINGLETON

    # Use central settings to determine API key (ensures .env is respected)
    try:
        from agriconnect.core.settings import settings

        groq_key = settings.llm_api_key
    except Exception:
        groq_key = os.getenv("GROQ_API_KEY") or os.getenv("AGRICONNECT_APIKEY")

    # Fail-fast: require an LLM API key to initialize the shared client.
    if not groq_key:
        raise RuntimeError(
            "GROQ_API_KEY (or AGRICONNECT_APIKEY) is required to initialize the LLM client."
        )

    try:
        raw = get_groq_sdk()
        if raw is None:
            raise RuntimeError("get_groq_sdk() returned None — Groq SDK not available")
        adapter = _GroqAdapter(raw)
        _LLM_SINGLETON = adapter
        logger.info("LLM groq adapter initialized and cached")
        return _LLM_SINGLETON
    except Exception as exc:
        logger.exception("Failed to initialize Groq SDK adapter: %s", exc)
        raise
