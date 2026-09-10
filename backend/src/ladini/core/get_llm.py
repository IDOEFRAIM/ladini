import logging
import os
import re
import time
from typing import Any, Optional

logger = logging.getLogger("ladini.core.get_llm")

# Incident réel (2026-08-31) : AGENT_TIMEOUT à 163s sur une simple réponse
# de menu ("le deuxieme"), alors que le site d'appel encapsule déjà l'appel
# LLM dans `asyncio.wait_for(..., timeout=15.0)`. Cause racine : le client
# HTTP (Groq ou passerelle Bedrock compatible OpenAI) était construit avec
# `timeout=20.0` — PLUS LONG que le wait_for englobant (15.0). `wait_for`
# annule le COROUTINE appelant à 15s, mais `asyncio.to_thread` exécute
# l'appel bloquant dans un thread du ThreadPoolExecutor PAR DÉFAUT (partagé,
# tout le process) — Python n'a AUCUNE API pour tuer un thread en cours.
# Le thread continue de bloquer jusqu'à SON PROPRE timeout (20s), 5s après
# que l'appelant a déjà abandonné — un thread "orphelin" fuité à chaque
# appel plus lent que 15s. Sous dégradation soutenue de la passerelle
# (plusieurs nœuds du graphe appellent le LLM dans le même tour), ces fuites
# s'accumulent plus vite qu'elles ne se libèrent, jusqu'à saturer le pool de
# threads partagé — TOUT nouvel `asyncio.to_thread(...)` du process, même
# sans rapport, doit alors attendre en file un thread libre avant de
# seulement démarrer. C'est ainsi qu'un point d'accès lent devient un gel de
# 163s au lieu d'un échec propre à 15s.
#
# Fix : le timeout HTTP DOIT être strictement plus court que le
# `asyncio.wait_for` englobant, avec de la marge — pour que le thread se
# termine PROPREMENT via SA PROPRE exception de timeout (httpx/openai)
# AVANT que `wait_for` n'ait besoin d'annuler quoi que ce soit. Timeouts
# distincts connect/read/write/pool (jamais une seule valeur uniforme) :
# une poignée de main TCP bloquée doit échouer vite, pas consommer tout le
# budget destiné à la lecture de la réponse.
_HTTP_CONNECT_TIMEOUT = 5.0
_HTTP_READ_TIMEOUT = 10.0
_HTTP_WRITE_TIMEOUT = 10.0
_HTTP_POOL_TIMEOUT = 5.0


def _build_http_timeout() -> Any:
    """`httpx.Timeout` avec connect/read/write/pool distincts — voir le
    commentaire d'incident ci-dessus. Import local : `httpx` est déjà une
    dépendance transitive de `openai`/`groq`, mais on évite de l'imposer au
    niveau module pour tout le reste du codebase qui importe get_llm.py."""
    import httpx

    return httpx.Timeout(
        connect=_HTTP_CONNECT_TIMEOUT,
        read=_HTTP_READ_TIMEOUT,
        write=_HTTP_WRITE_TIMEOUT,
        pool=_HTTP_POOL_TIMEOUT,
    )


class _CircuitBreaker:
    """Disjoncteur simple, en mémoire, par client LLM — évite de continuer à
    marteler une passerelle dégradée/indisponible (chaque tentative peut
    encore fuiter un thread, voir l'incident ci-dessus) et de consommer le
    budget timeout de CHAQUE nœud du graphe qui appelle le LLM dans le même
    tour. Après `failure_threshold` échecs consécutifs (timeout, erreur
    réseau, 5xx), le circuit s'ouvre pour `cooldown_s` : tout appel durant
    cette fenêtre échoue IMMÉDIATEMENT (pas de tentative réseau), laissant
    l'appelant basculer sur son repli (voir `_GroqAdapter.fallback_client`)
    sans attendre un nouveau timeout complet."""

    def __init__(self, failure_threshold: int = 3, cooldown_s: float = 30.0):
        self._failure_threshold = failure_threshold
        self._cooldown_s = cooldown_s
        self._consecutive_failures = 0
        self._opened_until = 0.0

    @property
    def is_open(self) -> bool:
        return time.monotonic() < self._opened_until

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_until = 0.0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._opened_until = time.monotonic() + self._cooldown_s
            logger.warning(
                "CIRCUIT_BREAKER_OPEN | %d échecs consécutifs — appels court-circuités pendant %.0fs",
                self._consecutive_failures,
                self._cooldown_s,
            )


class _CircuitOpenError(RuntimeError):
    """Levée par `_GroqAdapter` quand le disjoncteur est ouvert et qu'aucun
    `fallback_client` n'est configuré — jamais une tentative réseau."""

# Incident réel (2026-08-27) : le repli automatique vers un modèle de
# raisonnement (ex: qwen/qwen3.6-27b sur rate-limit du modèle principal,
# voir `_fallback_model_for`) a laissé fuir tel quel un bloc <think>...</think>
# dans un message WhatsApp ("[3/3] <think>\nHere's a thinking process...").
# Filtré ici, au point d'interception UNIQUE de tous les appels Groq, pour
# couvrir tous les sites d'appel (routing.py, response_handlers.py,
# clarification.py, slot_enrichment.py, utils.py) sans les patcher un par un.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think>", re.IGNORECASE)


def _strip_think_block(text: Optional[str]) -> Optional[str]:
    """Retire tout raisonnement `<think>...</think>` d'une complétion.

    Si le modèle a épuisé son budget de tokens EN PLEIN raisonnement (tag
    ouvrant jamais refermé), tout ce qui suit `<think>` est retiré aussi —
    exposer un raisonnement partiel n'est jamais préférable à une réponse
    vide (les appelants ont déjà leur propre repli sur contenu vide/invalide,
    voir ex. `nodes/rendering/ask.py::generate_llm_question`)."""
    if not text or "<think" not in text.lower():
        return text
    stripped = _THINK_BLOCK_RE.sub("", text)
    stripped = _THINK_OPEN_RE.split(stripped)[0]
    return stripped.strip()

# Module-level singleton cache
_LLM_SINGLETON: Optional[Any] = None
_GROQ_SDK_SINGLETON: Optional[Any] = None
_BEDROCK_CLIENT_SINGLETON: Optional[Any] = None
_OPENAI_COMPATIBLE_SDK_SINGLETON: Optional[Any] = None


class _MockGroqClient:
    """Client Groq factice — bascule sandbox (`settings.MOCK_EXTERNAL_APIS`,
    2026-08-27) : permet de faire tourner l'agent (et les tests) sans clé API
    réelle ni appel réseau. Renvoie un JSON vide `{}` pour les appels en mode
    JSON structuré (chaque appelant applique déjà ses propres valeurs par
    défaut sur des champs absents — voir ex. `interpreter/routing.py`), et
    une phrase générique pour les complétions texte libre."""

    class chat:  # noqa: N801 — imite la forme `client.chat.completions.create`
        class completions:  # noqa: N801
            @staticmethod
            def create(**kwargs: Any) -> Any:
                is_json_mode = (kwargs.get("response_format") or {}).get(
                    "type"
                ) == "json_object"
                text = "{}" if is_json_mode else "[SANDBOX] Réponse simulée (MOCK_EXTERNAL_APIS=true)."
                return _NormalizedChatResponse(text)


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

    from ladini.core.settings import settings

    if getattr(settings, "MOCK_EXTERNAL_APIS", False):
        logger.warning(
            "MOCK_EXTERNAL_APIS=true — SDK Groq REMPLACÉ par un client factice "
            "(aucun appel réseau réel, aucune clé API requise)."
        )
        _GROQ_SDK_SINGLETON = _MockGroqClient()
        return _GROQ_SDK_SINGLETON

    # Incident réel (2026-09-05) : ce garde comparait `LLM_PROVIDER` (qui ne
    # gouverne QUE le client LEGACY unique construit par `get_llm()` — voir
    # sa docstring) avant de construire le client Groq — alors que
    # `llm_gateway/gateway.py::_default_client_for_provider` appelle CETTE
    # fonction directement pour n'importe quel candidat `groq:*` de la chaîne
    # de repli, INDÉPENDAMMENT de `LLM_PROVIDER` (Modèle A, chaque candidat
    # porte son propre provider — voir `llm_gateway/registry.py`). Avec
    # `LLM_PROVIDER=bedrock`, ce garde levait pour la moindre tentative du
    # candidat de repli `groq:llama-3.3-70b-versatile`, alors même que
    # `GROQ_API_KEY` était valide — la seule vraie question. Résultat observé
    # en prod : "je veux voir les enchères" → 2 candidats bedrock_gateway en
    # 401 (token expiré) PUIS le repli groq refusé par CE garde, classé
    # `application_error` (pas de fragment CONFIG reconnu dans son message) →
    # tous les candidats "épuisés" → UNKNOWN générique, alors qu'un repli
    # groq valide existait et n'a jamais été essayé.
    #
    # Second appelant touché par le même garde, silencieux celui-là : la
    # branche `LLM_PROVIDER=bedrock` de `get_llm()` (~ligne 826) construit
    # déjà un `fallback_client = get_groq_sdk()` pour son propre repli
    # inter-provider — ce garde le faisait échouer AUSSI, avalé par son
    # `except Exception` local, rendant ce fallback documenté totalement
    # inopérant dès que `LLM_PROVIDER != groq`.
    #
    # La seule vraie condition d'utilisabilité de Groq est l'identifiant —
    # vérifiée juste en dessous. `LLM_PROVIDER` n'a plus voix ici.
    api_key = settings.llm_api_key
    if not api_key:
        raise RuntimeError(
            "Aucune clé GROQ_API_KEY/LADINI_APIKEY n'est définie pour initialiser le SDK Groq."
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
    # `_build_http_timeout()` (2026-08-31, incident AGENT_TIMEOUT 163s) :
    # remplace l'ancien `timeout=20.0` uniforme — plus LONG que les
    # `asyncio.wait_for` englobants (8-15s), ce qui fuitait un thread à
    # chaque appel plus lent que le wait_for. Voir le commentaire détaillé
    # en tête de fichier.
    _GROQ_SDK_SINGLETON = Groq(
        api_key=api_key, max_retries=0, timeout=_build_http_timeout()
    )
    logger.info(
        "Groq SDK initialisé et mis en cache (max_retries=0, read_timeout=%ss)",
        _HTTP_READ_TIMEOUT,
    )
    return _GROQ_SDK_SINGLETON


def get_bedrock_client(force_refresh: bool = False) -> Any:
    """Construit (et met en cache) le client boto3 `bedrock-runtime`.

    Même pattern que `get_groq_sdk()` : cache module-level, court-circuit
    `MOCK_EXTERNAL_APIS` vers le même client factice (déjà agnostique du
    fournisseur — il renvoie juste `{}`/une phrase générique). Les
    identifiants AWS explicites (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/
    `AWS_SESSION_TOKEN`) sont optionnels : quand ils sont absents, boto3
    retombe sur sa propre chaîne de résolution de credentials par défaut
    (rôle IAM, `~/.aws/credentials`, variables d'env standard AWS) — c'est
    une configuration légitime, pas une erreur, donc jamais de fail-fast sur
    leur seule absence ici (contrairement à `GROQ_API_KEY`, qui n'a pas
    d'équivalent "chaîne de résolution implicite").
    """
    global _BEDROCK_CLIENT_SINGLETON
    if not force_refresh and _BEDROCK_CLIENT_SINGLETON is not None:
        return _BEDROCK_CLIENT_SINGLETON

    from ladini.core.settings import settings

    if getattr(settings, "MOCK_EXTERNAL_APIS", False):
        logger.warning(
            "MOCK_EXTERNAL_APIS=true — client Bedrock REMPLACÉ par un client "
            "factice (aucun appel réseau réel, aucun identifiant AWS requis)."
        )
        _BEDROCK_CLIENT_SINGLETON = _MockGroqClient()
        return _BEDROCK_CLIENT_SINGLETON

    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError(
            "Le package python 'boto3' est requis pour instancier le client "
            "Bedrock.\nInstallez-le via `pip install boto3`."
        ) from exc

    client_kwargs: dict = {}
    if settings.AWS_REGION:
        client_kwargs["region_name"] = settings.AWS_REGION
    if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
        client_kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
        client_kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
        if settings.AWS_SESSION_TOKEN:
            client_kwargs["aws_session_token"] = settings.AWS_SESSION_TOKEN

    _BEDROCK_CLIENT_SINGLETON = boto3.client("bedrock-runtime", **client_kwargs)
    logger.info(
        "Client Bedrock (bedrock-runtime) initialisé et mis en cache (region=%s)",
        settings.AWS_REGION or "<chaîne de résolution boto3 par défaut>",
    )
    return _BEDROCK_CLIENT_SINGLETON


def get_openai_compatible_sdk(force_refresh: bool = False) -> Any:
    """Construit (et met en cache) un client `openai` pointé sur une
    passerelle compatible OpenAI (ex: AWS Bedrock API keys — voir
    `settings.OPENAI_BASE_URL`).

    C'est le chemin PRÉFÉRÉ pour LLM_PROVIDER=bedrock quand
    `OPENAI_BASE_URL` est défini : Groq lui-même est déjà un client
    compatible OpenAI (`client.chat.completions.create(...)` ->
    `.choices[0].message.content`), donc le client `openai` officiel expose
    EXACTEMENT la même interface — `_GroqAdapter` (déjà générique malgré son
    nom) peut l'envelopper tel quel, sans aucune traduction de requête.
    """
    global _OPENAI_COMPATIBLE_SDK_SINGLETON
    if not force_refresh and _OPENAI_COMPATIBLE_SDK_SINGLETON is not None:
        return _OPENAI_COMPATIBLE_SDK_SINGLETON

    from ladini.core.settings import settings

    if getattr(settings, "MOCK_EXTERNAL_APIS", False):
        logger.warning(
            "MOCK_EXTERNAL_APIS=true — client OpenAI-compatible REMPLACÉ par "
            "un client factice (aucun appel réseau réel, aucune clé requise)."
        )
        _OPENAI_COMPATIBLE_SDK_SINGLETON = _MockGroqClient()
        return _OPENAI_COMPATIBLE_SDK_SINGLETON

    if not settings.OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY est requis pour initialiser la passerelle "
            "compatible OpenAI (settings.OPENAI_BASE_URL est défini)."
        )

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "Le package python 'openai' est requis pour instancier ce client.\n"
            "Installez-le via `pip install openai`."
        ) from exc

    # max_retries=0 : même discipline que get_groq_sdk() — un retry masqué à
    # l'intérieur du SDK peut dépasser les `asyncio.wait_for` posés aux
    # sites d'appel, convertissant un appel qui aurait fini par réussir en
    # UNKNOWN forcé. `_build_http_timeout()` (2026-08-31, incident
    # AGENT_TIMEOUT 163s sur la passerelle Bedrock) : voir le commentaire
    # détaillé en tête de fichier — remplace un ancien `timeout=20.0`
    # uniforme, plus LONG que les `asyncio.wait_for` englobants (8-15s), qui
    # fuitait un thread à chaque appel plus lent que le wait_for et a fini
    # par saturer le pool de threads partagé sous dégradation soutenue de la
    # passerelle.
    _OPENAI_COMPATIBLE_SDK_SINGLETON = OpenAI(
        api_key=settings.OPENAI_API_KEY,
        base_url=settings.OPENAI_BASE_URL or None,
        max_retries=0,
        timeout=_build_http_timeout(),
    )
    logger.info(
        "Client OpenAI-compatible initialisé et mis en cache (base_url=%s, read_timeout=%ss)",
        settings.OPENAI_BASE_URL or "<défaut OpenAI>",
        _HTTP_READ_TIMEOUT,
    )
    return _OPENAI_COMPATIBLE_SDK_SINGLETON


def _is_rate_limit_error(exc: Exception) -> bool:
    try:
        from groq import RateLimitError

        if isinstance(exc, RateLimitError):
            return True
    except ImportError:
        pass
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True
    return _is_bedrock_throttling_error(exc)


def _is_bedrock_throttling_error(exc: Exception) -> bool:
    """Équivalent Bedrock de `RateLimitError` — `botocore.exceptions.ClientError`
    avec le code `ThrottlingException`."""
    try:
        from botocore.exceptions import ClientError
    except ImportError:
        return False
    if not isinstance(exc, ClientError):
        return False
    error_code = (exc.response or {}).get("Error", {}).get("Code")
    return error_code == "ThrottlingException"


def _fallback_model_for(requested_model: str) -> Optional[str]:
    """Modèle de repli à quota TPD séparé sur CE MÊME client Groq, ou None si
    aucun repli pertinent (déjà sur le modèle rapide, modèle demandé inconnu,
    ou repli structurellement invalide pour Groq).

    Incident réel (2026-09-07) : `settings.LLM_MODEL` est un réglage legacy,
    partagé avec la config multi-provider du LLM Gateway
    (`llm_gateway/gateway.py`, candidats `provider:model` indépendants —
    voir `docs/LLM_GATEWAY_FAILURE_RECOVERY_2026-09-05.md`). Un `.env` de
    production avait fini par y stocker un ID au format Bedrock
    ("qwen.qwen3-32b", notation par POINTS — voir le catalogue Bedrock,
    jamais par slash) au lieu d'un ID Groq ("qwen/qwen3.6-27b", notation par
    SLASH) — ce repli, exécuté directement sur le SDK Groq, tentait alors ce
    modèle inexistant côté Groq (404 `model_not_found`), cassant tout appel
    LLM du tour (onboarding, interprétation) dès qu'un simple 429 survenait.
    Un ID contenant un point mais aucun slash est structurellement un modèle
    Bedrock, jamais un modèle Groq valide — on n'y tente jamais de repli.
    """
    try:
        from ladini.core.settings import settings

        fast_model = str(getattr(settings, "LLM_MODEL", "") or "")
    except Exception:
        fast_model = ""
    if not fast_model or requested_model == fast_model:
        return None
    if "." in fast_model and "/" not in fast_model:
        logger.warning(
            "GROQ_FALLBACK_MODEL_REJECTED | requested=%s configured_fallback=%s "
            "reason=looks_like_bedrock_model_id (notation par points, "
            "jamais valide sur Groq) — repli ignoré, LLM_MODEL doit être "
            "un ID Groq (notation slash)",
            requested_model,
            fast_model,
        )
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

    def __init__(self, raw_client: Any, fallback_client: Optional[Any] = None):
        self._raw = raw_client
        # Repli propre inter-provider (2026-08-31, incident AGENT_TIMEOUT) :
        # un client SDK brut (ex: Groq) à essayer si `raw_client` (ex: la
        # passerelle Bedrock) est en disjoncteur ouvert ou échoue. `None` =
        # pas de repli configuré, comportement inchangé.
        self._fallback_client = fallback_client
        self._circuit = _CircuitBreaker()

    class _Chat:
        def __init__(self, parent: "_GroqAdapter"):
            self._parent = parent

        class _Completions:
            def __init__(self, parent: "_GroqAdapter"):
                self._parent = parent

            def create(self, **kwargs):
                """Point d'entrée public — disjoncteur + repli inter-provider
                (2026-08-31) autour de `_create_via_primary` (logique
                existante, inchangée). Voir le commentaire d'incident en
                tête de fichier : ce disjoncteur évite de re-tenter un appel
                réseau complet vers une passerelle déjà connue dégradée
                (chaque tentative peut encore fuiter un thread) et bascule
                immédiatement sur `fallback_client` quand il est configuré."""
                parent = self._parent
                circuit = parent._circuit
                fallback_client = parent._fallback_client

                if circuit.is_open:
                    if fallback_client is None:
                        raise _CircuitOpenError(
                            "LLM circuit breaker ouvert (passerelle dégradée) — "
                            "aucun fallback_client configuré."
                        )
                    logger.warning(
                        "CIRCUIT_OPEN_FALLBACK | model=%s — appel direct au client de repli, "
                        "passerelle primaire court-circuitée",
                        kwargs.get("model"),
                    )
                    return fallback_client.chat.completions.create(**kwargs)

                try:
                    resp = self._create_via_primary(**kwargs)
                    circuit.record_success()
                    return resp
                except Exception as primary_exc:
                    circuit.record_failure()
                    if fallback_client is None:
                        raise
                    logger.warning(
                        "PRIMARY_LLM_FAILED_FALLBACK | model=%s | %s — bascule sur le client de repli",
                        kwargs.get("model"),
                        primary_exc,
                    )
                    return fallback_client.chat.completions.create(**kwargs)

            def _create_via_primary(self, **kwargs):
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
                        from ladini.core.telemetry import record_generation

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

                def _strip_think_in_place(resp):
                    """Sanitize `resp.choices[0].message.content` en place si
                    possible ; sinon reconstruit un wrapper normalisé avec le
                    texte nettoyé (voir `_strip_think_block`)."""
                    try:
                        content = resp.choices[0].message.content
                    except Exception:
                        return resp
                    if not content or "<think" not in str(content).lower():
                        return resp
                    cleaned = _strip_think_block(content)
                    try:
                        resp.choices[0].message.content = cleaned
                        return resp
                    except Exception:
                        return _NormalizedChatResponse(cleaned)

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
                            resp = _strip_think_in_place(resp)
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
                            return _strip_think_in_place(resp)
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


class _BedrockAdapter:
    """Adapter normalisant un client boto3 `bedrock-runtime` vers la même
    interface que `_GroqAdapter` : `client.chat.completions.create(...)`
    renvoyant `.choices[0].message.content`. Utilise l'API Converse de
    Bedrock (uniforme entre familles de modèles — Anthropic, Llama,
    Mistral...), plutôt que le format de requête natif propre à chaque
    modèle.
    """

    def __init__(self, raw_client: Any):
        self._raw = raw_client

    class _Chat:
        def __init__(self, parent: "_BedrockAdapter"):
            self._parent = parent

        class _Completions:
            def __init__(self, parent: "_BedrockAdapter"):
                self._parent = parent

            @staticmethod
            def _split_messages(messages: Any) -> tuple:
                """Convertit les messages OpenAI-style (`role`+`content` str)
                vers le format Converse : `system` séparé en top-level,
                le reste en `content: [{"text": ...}]` par message."""
                system_blocks = []
                converse_messages = []
                for msg in messages or []:
                    role = msg.get("role")
                    content = str(msg.get("content") or "")
                    if role == "system":
                        system_blocks.append({"text": content})
                    else:
                        converse_messages.append(
                            {
                                "role": role if role in ("user", "assistant") else "user",
                                "content": [{"text": content}],
                            }
                        )
                return system_blocks, converse_messages

            def create(self, **kwargs):
                # ── LLMOps : même point d'interception unique que Groq ──
                import time as _t

                _model = str(kwargs.get("model") or "unknown")
                _messages = kwargs.get("messages")
                _t0 = _t.perf_counter()
                _err = None

                def _emit(output, usage):
                    try:
                        from ladini.core.telemetry import record_generation

                        record_generation(
                            model=_model,
                            messages=_messages,
                            output=output,
                            latency_s=_t.perf_counter() - _t0,
                            usage=usage,
                            error=_err,
                            name="bedrock_completion",
                        )
                    except Exception:
                        pass

                client = self._parent._raw
                system_blocks, converse_messages = self._split_messages(_messages)
                inference_config = {}
                if kwargs.get("temperature") is not None:
                    inference_config["temperature"] = kwargs["temperature"]
                # `response_format` (mode JSON) n'a pas d'équivalent Converse
                # universel — chaque prompt système exige déjà un JSON strict
                # en texte ("Tu réponds UNIQUEMENT le JSON"), donc ignoré sans
                # que ce soit une erreur.
                if kwargs.get("response_format"):
                    logger.debug(
                        "Bedrock Converse: response_format ignoré (pas d'équivalent "
                        "natif) — le prompt système impose déjà un JSON strict."
                    )

                converse_kwargs: dict = {
                    "modelId": _model,
                    "messages": converse_messages,
                }
                if system_blocks:
                    converse_kwargs["system"] = system_blocks
                if inference_config:
                    converse_kwargs["inferenceConfig"] = inference_config

                def _call(model_id: str):
                    call_kwargs = dict(converse_kwargs)
                    call_kwargs["modelId"] = model_id
                    return client.converse(**call_kwargs)

                try:
                    response = _call(_model)
                except Exception as call_exc:
                    is_throttled = _is_bedrock_throttling_error(call_exc)
                    fallback_model = _fallback_model_for(_model)
                    if is_throttled and fallback_model:
                        logger.warning(
                            "BEDROCK_THROTTLE_FALLBACK | model=%s -> %s | %s",
                            _model,
                            fallback_model,
                            call_exc,
                        )
                        try:
                            response = _call(fallback_model)
                            _model = fallback_model
                        except Exception as fallback_exc:
                            _err = f"{type(fallback_exc).__name__}: {fallback_exc}"
                            _emit(None, None)
                            raise fallback_exc
                    else:
                        _err = f"{type(call_exc).__name__}: {call_exc}"
                        _emit(None, None)
                        raise

                try:
                    text = response["output"]["message"]["content"][0]["text"]
                except (KeyError, IndexError, TypeError):
                    _err = "malformed Bedrock Converse response"
                    _emit(None, None)
                    return _NormalizedChatResponse(
                        "<llm-error: malformed bedrock response>"
                    )

                text = _strip_think_block(text)
                usage_raw = response.get("usage") or {}
                usage = {
                    "prompt_tokens": usage_raw.get("inputTokens"),
                    "completion_tokens": usage_raw.get("outputTokens"),
                }
                _emit(text, usage)
                return _NormalizedChatResponse(text)

        @property
        def completions(self):
            return _BedrockAdapter._Chat._Completions(self._parent)

    @property
    def chat(self):
        return _BedrockAdapter._Chat(self)


def get_llm(llm_client: Optional[Any] = None) -> Optional[Any]:
    """Return a normalized LLM client singleton.

    Resolution order:
    1. If `llm_client` provided, return it (but do not cache it globally).
    2. Return cached singleton if exists.
    3. Dispatch on `settings.LLM_PROVIDER`:
       - "bedrock" + `OPENAI_BASE_URL` set → OpenAI-compatible gateway
         (`get_openai_compatible_sdk()`, wrapped in `_GroqAdapter` — it's
         already provider-agnostic, and this gateway speaks the exact same
         protocol Groq does).
       - "bedrock" without `OPENAI_BASE_URL` → native boto3/Converse
         (`get_bedrock_client()` + `_BedrockAdapter`).
       - anything else → Groq adapter via `get_groq_sdk()`, the historical
         default.
       All three expose the identical `client.chat.completions.create(...)`
       shape, so every call site is provider-agnostic — switching providers
       is a `.env` change, never a code change.

    Behavior:
    - Lazy-loads settings to avoid heavy imports at module import time.
    - Fail-fast when no usable credential is configured for the selected
      provider — sauf en mode sandbox (`MOCK_EXTERNAL_APIS=true`), où chaque
      provider retombe sur un client factice sans jamais toucher au réseau.
      Bedrock n'exige PAS que des clés AWS explicites soient présentes (boto3
      a sa propre chaîne de résolution par défaut — rôle IAM, etc.) ; seul un
      échec de construction du client lève.
    """
    global _LLM_SINGLETON

    if llm_client:
        return llm_client

    if _LLM_SINGLETON is not None:
        return _LLM_SINGLETON

    mock_external_apis = False
    provider = "groq"
    try:
        from ladini.core.settings import settings

        mock_external_apis = bool(getattr(settings, "MOCK_EXTERNAL_APIS", False))
        provider = (getattr(settings, "LLM_PROVIDER", "groq") or "groq").strip().lower()
    except Exception:
        pass

    if provider == "bedrock":
        use_openai_gateway = False
        try:
            from ladini.core.settings import settings

            use_openai_gateway = bool(settings.OPENAI_BASE_URL)
        except Exception:
            pass

        if use_openai_gateway:
            try:
                raw = get_openai_compatible_sdk()
                if raw is None:
                    raise RuntimeError(
                        "get_openai_compatible_sdk() returned None — client not available"
                    )
                # Repli propre inter-provider (2026-08-31, incident
                # AGENT_TIMEOUT) : si un GROQ_API_KEY réel est configuré (au-
                # delà de LLM_PROVIDER=bedrock), on le passe comme
                # `fallback_client` — le disjoncteur de `_GroqAdapter` bascule
                # dessus automatiquement quand la passerelle Bedrock est
                # dégradée/indisponible, au lieu de dégrader systématiquement
                # vers UNKNOWN. Jamais construit tant qu'il n'est pas
                # réellement nécessaire (lazy, best-effort).
                fallback_client = None
                try:
                    if settings.llm_api_key:
                        fallback_client = get_groq_sdk()
                except Exception:
                    logger.debug(
                        "Aucun client Groq de repli disponible pour la passerelle Bedrock "
                        "(GROQ_API_KEY absent ou init échouée) — pas de fallback configuré.",
                        exc_info=True,
                    )
                adapter = _GroqAdapter(raw, fallback_client=fallback_client)
                _LLM_SINGLETON = adapter
                logger.info(
                    "LLM bedrock adapter initialized (OpenAI-compatible gateway, "
                    "fallback=%s) and cached",
                    "groq" if fallback_client is not None else "none",
                )
                return _LLM_SINGLETON
            except Exception as exc:
                logger.exception(
                    "Failed to initialize OpenAI-compatible Bedrock gateway: %s", exc
                )
                raise

        try:
            raw = get_bedrock_client()
            if raw is None:
                raise RuntimeError(
                    "get_bedrock_client() returned None — Bedrock client not available"
                )
            adapter = _BedrockAdapter(raw)
            _LLM_SINGLETON = adapter
            logger.info("LLM bedrock adapter initialized (native boto3/Converse) and cached")
            return _LLM_SINGLETON
        except Exception as exc:
            logger.exception("Failed to initialize Bedrock adapter: %s", exc)
            raise

    # Use central settings to determine API key (ensures .env is respected)
    try:
        from ladini.core.settings import settings

        groq_key = settings.llm_api_key
    except Exception:
        groq_key = os.getenv("GROQ_API_KEY") or os.getenv("LADINI_APIKEY")

    # Fail-fast: require an LLM API key to initialize the shared client —
    # sauf en mode sandbox (`MOCK_EXTERNAL_APIS=true`, 2026-08-27), où
    # `get_groq_sdk()` renvoie un client factice sans jamais toucher au réseau.
    if not groq_key and not mock_external_apis:
        raise RuntimeError(
            "GROQ_API_KEY (or LADINI_APIKEY) is required to initialize the LLM client."
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
