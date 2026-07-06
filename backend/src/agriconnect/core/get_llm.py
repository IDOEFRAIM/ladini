import os
import logging
from typing import Any, Optional

logger = logging.getLogger("agriconnect.core.get_llm")

# Module-level singleton cache
_LLM_SINGLETON: Optional[Any] = None


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

        self.choices = [ _Choice(text) ]


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
                # Try to call the underlying SDK with a couple of common shapes.
                client = self._parent._raw
                # Best-effort: try `client.chat.completions.create` if present
                try:
                    chat = getattr(client, "chat", None)
                    if chat and hasattr(chat, "completions") and hasattr(chat.completions, "create"):
                        resp = chat.completions.create(**kwargs)
                        # If the SDK already returns an object with choices[0].message.content,
                        # return it directly. Otherwise, try to coerce.
                        if hasattr(resp, "choices"):
                            return resp
                        # Fallback: coerce string-like responses
                        text = str(resp)
                        return _NormalizedChatResponse(text)

                except Exception:
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
                return _NormalizedChatResponse("<llm-error: adapter could not call underlying sdk>")

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
        # Lazy import of rag components to avoid heavy SDK imports at module import
        from futur.rag.components import get_groq_sdk

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
