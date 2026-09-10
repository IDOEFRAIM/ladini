from __future__ import annotations

import ast
import asyncio
import contextvars
import json
import logging
import re
import time
import unicodedata
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from ladini.core.llm import get_llm
from ladini.core.settings import settings
from ladini.graphs.agents.market_coach.core.slots import (
    build_canonical_field_aliases,
)
from ladini.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from ladini.graphs.agents.market_coach.llm_gateway import (
    resolve_gateway,
    resolve_profile,
)
from ladini.graphs.agents.market_coach.security import SecurityService
from ladini.infrastructure.mcp.client import AgriMCPClient, MCPTransportConfig
from ladini.infrastructure.mcp.context import (
    FarmerContext,
    get_mcp_context,
    mcp_context_scope,
)
from ladini.infrastructure.mcp.security import TOOL_SCOPE_MAP, PermissionScope

logger = logging.getLogger("Agent.MarketCoach")


def _ensure_intent_tool_scopes() -> None:
    missing_tools = []
    for intent_key, cfg in INTENT_CONFIG.items():
        tool_name = str((cfg or {}).get("tool_name") or "").strip()
        if not tool_name or tool_name in TOOL_SCOPE_MAP:
            continue
        action_type = str((cfg or {}).get("action_type") or "READ").upper()
        scope = (
            PermissionScope.DB_DATA_WRITE
            if action_type == "WRITE"
            else PermissionScope.DB_READ_ONLY
        )
        TOOL_SCOPE_MAP[tool_name] = scope
        missing_tools.append((intent_key, tool_name, scope.value))

    if missing_tools:
        logger.warning(
            "[ToolScopeWarmup] Enregistré %d outils manquants: %s",
            len(missing_tools),
            ", ".join(tool for _, tool, _ in missing_tools),
        )


_ensure_intent_tool_scopes()


@dataclass(frozen=True)
class ToolScope:
    """ACL definition for a node.

    ``allowed_tools`` being ``None`` keeps backward compatibility (no filtering)
    while ``allow_direct_db`` governs legacy ``ensure_db()`` usage.
    """

    name: str
    allowed_tools: Optional[Set[str]] = None
    allow_direct_db: bool = False


class ToolScopeManager:
    """Central registry for node→tool ACLs.

    This is a defence-in-depth layer: even if a prompt forces a node to
    attempt a privileged MCP call, the runtime checks this registry before
    executing anything dangerous.
    """

    _scope_var: contextvars.ContextVar[str] = contextvars.ContextVar(
        "market_tool_scope", default="default"
    )
    _scopes: Dict[str, ToolScope] = {
        "default": ToolScope(name="default", allowed_tools=None, allow_direct_db=True),
        "llm_only": ToolScope(
            name="llm_only", allowed_tools=set(), allow_direct_db=False
        ),
    }
    _node_to_scope: Dict[str, str] = {
        # Conversational nodes must never hit MCP/DB even if compromised.
        "clarification_node": "llm_only",
        "final_response": "llm_only",
        "response_strategy": "llm_only",
    }

    @classmethod
    def register_scope(cls, scope: ToolScope) -> None:
        cls._scopes[scope.name] = scope

    @classmethod
    def register_node_scope(cls, node_name: str, scope_name: str) -> None:
        cls._node_to_scope[node_name] = scope_name

    @classmethod
    def activate_for_node(cls, node_name: str):
        scope_name = cls._node_to_scope.get(node_name, "default")
        if scope_name not in cls._scopes:
            scope_name = "default"
        return cls._scope_var.set(scope_name)

    @classmethod
    def reset_scope(cls, token) -> None:
        cls._scope_var.reset(token)

    @classmethod
    def current_scope(cls) -> ToolScope:
        scope_name = cls._scope_var.get()
        return cls._scopes.get(scope_name, cls._scopes["default"])

    @classmethod
    def can_call_tool(cls, tool_name: str) -> bool:
        scope = cls.current_scope()
        allowed = scope.allowed_tools
        if allowed is None:
            return True
        tool = tool_name or ""
        for pattern in allowed:
            if cls._match(pattern, tool):
                return True
        return False

    @classmethod
    def can_use_direct_db(cls) -> bool:
        return cls.current_scope().allow_direct_db

    @staticmethod
    def _match(pattern: str, tool_name: str) -> bool:
        if pattern.endswith("*"):
            return tool_name.startswith(pattern[:-1])
        return pattern == tool_name


# ── ASCII folding (Postel's Law — MCP never receives accented strings) ──

_ASCII_LIGATURE_MAP = str.maketrans(
    {"œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE", "’": "'"}
)


def _ascii_fold_str(text: str) -> str:
    if not text:
        return text
    text = text.translate(_ASCII_LIGATURE_MAP)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _ascii_fold_value(value: Any) -> Any:
    if isinstance(value, str):
        return _ascii_fold_str(value)
    if isinstance(value, list):
        return [_ascii_fold_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_ascii_fold_value(v) for v in value)
    if isinstance(value, dict):
        return {k: _ascii_fold_value(v) for k, v in value.items()}
    return value


# Fields auto-resolvable from user identity (don't ask the user for them)
_AUTO_RESOLVABLE_FIELDS = frozenset({"farm_id", "phone"})

_EMPTY_SLOT_VALUES = (None, "", [], {})

# Source UNIQUE de vérité : le registre central des slots (core/slots.py).
# Ce module maintenait auparavant sa PROPRE table alias→canonique codée à la
# main, qui avait divergé du registre (il lui manquait culture, original_unit,
# original_quantity, quantity_kg, offered_price, montant_enchere, region,
# target_zone… tous déjà canonicalisés ailleurs via slots.py). Conséquence :
# `normalize_slot_keys` — utilisé par validation.py, response_handlers.py,
# semantic_disambiguation.py, entities.py — canonicalisait un SOUS-ENSEMBLE
# différent de memory.py/routing.py (qui, eux, consomment déjà slots.py). Un
# champ mappé dans un nœud mais pas dans l'autre = exactement la « petite
# erreur » récurrente due au doublon. On dérive désormais du registre, comme
# le fait déjà interpreter/intent.py (`build_canonical_field_aliases()`).
_CANONICAL_FIELD_ALIASES: Dict[str, str] = build_canonical_field_aliases()

CANONICAL_TRANSACTION_FIELDS = frozenset(
    {
        "intent",
        "role",
        "product",
        "variety",
        "quality_grade",
        "quantity",
        "unit",
        "price",
        # Déclinaisons de prix/conditionnement pour un MÊME produit (2026-08-27,
        # ex: "500f le demi-litre en sachet et 600f le bidon") — liste de
        # {"quantity","unit","price","packaging"}, en COMPLÉMENT de quantity/
        # unit/price ci-dessus (qui restent le 1er tier). Voir
        # interpreter/routing.py (extraction) et
        # services/ui/confirmation_summary.py (rendu groupé).
        "pricing_tiers",
        "currency",
        "is_negotiable",
        "zone",
        "farm_id",
        "stock_id",
    }
)


def normalize_slot_keys(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Map legacy alias keys to the canonical transaction vocabulary."""

    if not data:
        return {}
    normalized: Dict[str, Any] = {}
    for raw_key, value in data.items():
        if not isinstance(raw_key, str):
            normalized[raw_key] = value
            continue
        canonical = _CANONICAL_FIELD_ALIASES.get(raw_key.lower(), raw_key)
        normalized[canonical] = value
    return normalized


def slot_has_value(value: Any) -> bool:
    return value not in _EMPTY_SLOT_VALUES


# Generic error message returned when MCP fails irrecoverably
_GENERIC_TECHNICAL_ERROR = (
    "Une erreur technique est survenue lors de l'enregistrement. "
    "Veuillez réessayer ultérieurement."
)


_CANONICAL_UNIT_MAP: Dict[str, str] = {
    "KG": "KG",
    "KILO": "KG",
    "KGS": "KG",
    "KILOS": "KG",
    "KILOGRAMME": "KG",
    "KILOGRAMMES": "KG",
    "G": "KG",
    "GRAMME": "KG",
    "GRAMMES": "KG",
    "TON": "TONNE",
    "TONS": "TONNE",
    "TONE": "TONNE",
    "TONES": "TONNE",
    "T": "TONNE",
    "T.": "TONNE",
    "TONNE": "TONNE",
    "TONNES": "TONNE",
    "SAC": "SAC",
    "SACS": "SAC",
    "SACHET": "SAC",
    "SACHETS": "SAC",
    "UNITE": "UNITE",
    "UNITÉ": "UNITE",
    "UNITE.": "UNITE",
    "UNIT": "UNITE",
    "UNITES": "UNITE",
    "UNITÉS": "UNITE",
    "UNITS": "UNITE",
    "PIECE": "UNITE",
    "PIÈCE": "UNITE",
    "PIECES": "UNITE",
    "PIÈCES": "UNITE",
    "TETE": "UNITE",
    "TÊTE": "UNITE",
    "TETES": "UNITE",
    "TÊTES": "UNITE",
    "HEAD": "UNITE",
    "HEADS": "UNITE",
}


def canonical_unit_label(value: Any, default: str = "KG") -> str:
    unit = str(value or "").strip().upper()
    if not unit:
        return default
    return _CANONICAL_UNIT_MAP.get(unit, unit)


class MarketRuntimeError(RuntimeError):
    """Erreur spécifique au runtime du MarketCoach."""

    pass


# --- UTILS ---


async def _maybe_await(obj: Any) -> Any:
    """Utilitaire pour gérer les appels synchrones ou asynchrones du runtime."""
    if hasattr(obj, "__await__") or asyncio.iscoroutine(obj):
        return await obj
    return obj


def _to_float(value: Any) -> Optional[float]:
    """Sécurise la conversion numérique pour les calculs et la DB."""
    try:
        if value is None:
            return None
        return float(str(value).replace(",", "."))
    except (ValueError, TypeError):
        return None


# Nœuds d'infrastructure (aucun raisonnement métier) — le goal routé vers le
# LLM_ROUTER pour ces nœuds est leur PROPRE identité de stage, pas le goal
# métier de `state["current_goal"]` (qui reste le MÊME intent, ex:
# BUYER_REQUEST, sur TOUS les nœuds d'un tour — l'utiliser ici router ait le
# même modèle pour security_moderation ET goal_planner, ce qui annulerait
# tout l'intérêt du routage). Clés alignées sur `settings.ROUTING_MAP`.
_FAST_PATH_NODE_LABELS: Dict[str, str] = {
    "input_normalizer": "INPUT_NORMALIZATION",
    "security_moderation": "SECURITY_MODERATION",
    "state_cleaner": "STATE_CLEANER",
}


def _safe_node(
    fn: Callable[..., Awaitable[Dict[str, Any]]], name: str
) -> Callable[..., Awaitable[Dict[str, Any]]]:
    """Décorateur pour sécuriser les noeuds LangGraph et tracer leur durée."""

    async def _wrapped(
        state: Dict[str, Any], mc_runtime: "MarketRuntime", **_: Any
    ) -> Dict[str, Any]:
        scope_token = ToolScopeManager.activate_for_node(name)
        goal = str(state.get("current_goal") or "").upper()
        status = str(state.get("status") or "").upper()
        event = str(state.get("interpreted_event") or "").upper()
        # Met à jour le goal courant du runtime AVANT d'exécuter le nœud —
        # consommé par `MarketRuntime.model_answer` (llm_router). Zéro
        # changement requis dans les nœuds eux-mêmes : c'est le SEUL point de
        # passage commun à tous les nœuds du graphe.
        fast_label = _FAST_PATH_NODE_LABELS.get(name.strip().lower())
        mc_runtime.set_current_goal(fast_label or goal)
        start_ts = time.time()
        logger.info(
            "NODE_START | node=%s | goal=%s | status=%s | event=%s",
            name,
            goal,
            status,
            event,
        )
        try:
            result = await fn(state, mc_runtime)
            elapsed_ms = round((time.time() - start_ts) * 1000, 1)
            new_goal = str((result or {}).get("current_goal") or goal or "").upper()
            new_status = str((result or {}).get("status") or "").upper()
            logger.info(
                "NODE_END | node=%s | goal=%s | status=%s | duration_ms=%s",
                name,
                new_goal,
                new_status,
                elapsed_ms,
            )
            return result
        except Exception as exc:
            elapsed_ms = round((time.time() - start_ts) * 1000, 1)
            logger.exception(
                "NODE_ERROR | node=%s | goal=%s | duration_ms=%s | error=%s",
                name,
                goal,
                elapsed_ms,
                exc,
            )
            return {
                "status": "ERROR",
                "error_message": f"Erreur technique dans le noeud '{name}'.",
                "technical_details": str(exc),
            }
        finally:
            ToolScopeManager.reset_scope(scope_token)

    return _wrapped


def ensure_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {"status": "error", "message": "No response"}

    # If MCP already gave us a dict, we may still need to unwrap raw_result.
    if isinstance(obj, dict):
        raw = (
            (obj.get("data") or {}).get("raw_result")
            if isinstance(obj.get("data"), dict)
            else None
        )
        if isinstance(raw, str) and raw.strip():
            parsed = ensure_dict(raw)
            # If the embedded payload is a dict-like response, prefer it.
            if isinstance(parsed, dict) and parsed is not obj:
                return parsed
        return obj

    if isinstance(obj, str):
        obj = obj.strip()
        try:
            # Tentative 1 : JSON Standard
            return json.loads(obj)
        except json.JSONDecodeError:
            try:
                # Tentative 2 : Littéral Python (ex: "{'status': 'error', ...}").
                # PRIORITAIRE sur le nettoyage naïf de guillemets ci-dessous :
                # `ast.literal_eval` ne touche JAMAIS au contenu des chaînes —
                # contrairement à `.replace("'", '"')`, qui corrompt silencieusement
                # toute valeur contenant une apostrophe française interne
                # ("d'offres", "l'exploitation", "n'ai pas trouvé"...) en la
                # transformant en guillemet de délimitation JSON, cassant la
                # structure et faisant disparaître `message`/`data` de la
                # réponse sans lever d'erreur explicite.
                parsed = ast.literal_eval(obj)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass

            try:
                # Tentative 3 : Nettoyage des guillemets simples — dernier
                # recours seulement, car destructeur sur le texte français.
                cleaned = obj.replace("'", '"')
                return json.loads(cleaned)
            except Exception:
                pass

            # String brute non interprétable => traiter comme une erreur.
            logger.warning(f"Réponse brute non-JSON reçue du MCP : {obj[:50]}...")
            return {
                "status": "error",
                "data": {"raw_result": obj},
                "message": "Non-JSON response from MCP",
            }

    # Si c'est un objet (ex: Result de MCP)
    if hasattr(obj, "content"):  # Format spécifique à certains clients MCP
        return {"status": "ok", "data": obj.content}

    return {"status": "ok", "data": str(obj)}


def unwrap_tool_envelope(result: Any) -> Any:
    """Déballe une enveloppe d'exécution MCP (``ToolExecutionEnvelope``).

    Deux formes circulent selon le transport : le dict domaine brut
    (``{"status": "success", ...}``) OU l'enveloppe ``{"ok": bool, "data":
    {...}, "error": str, "meta": {...}}`` produite par
    ``ToolExecutionPolicy.execute`` (infrastructure/mcp/security.py). Une
    enveloppe ``ok=False`` qui atteint ``is_success_response`` sans être
    déballée n'a ni ``status`` ni ``message`` → faux négatif silencieux
    (bug historique « Stock insuffisant » sans chiffres).

    Détection STRICTE : clé ``ok`` booléenne présente ET pas de ``status``.
    Les dicts domaine portent souvent leur propre clé ``data`` (listes) —
    on ne touche jamais à un dict qui a déjà un ``status``.
    """
    if not isinstance(result, dict):
        return result
    if "status" in result or not isinstance(result.get("ok"), bool):
        return result
    ok = result["ok"]
    data = result.get("data")
    error = result.get("error")
    if isinstance(data, dict) and data:
        merged = dict(data)
        if error and "message" not in merged:
            merged["message"] = str(error)
        merged.setdefault("status", "success" if ok else "error")
        return merged
    if isinstance(data, list) and data:
        return {
            "status": "success" if ok else "error",
            "data": data,
            "message": (str(error) or None) if error else None,
        }
    # data vide → propager le verdict de l'enveloppe comme erreur/succès domaine.
    return {
        "status": "success" if ok else "error",
        "message": (str(error) or None) if error else None,
    }


def is_success_response(res: Dict[str, Any]) -> bool:
    if not res:
        return False
    # Filet enveloppe : si un ToolExecutionEnvelope non déballé arrive ici,
    # son verdict `ok` fait foi (jamais de faux positif "data présent").
    if "status" not in res and isinstance(res.get("ok"), bool):
        return res["ok"]
    status = str(res.get("status") or "").lower()
    if status in {"ok", "success", "completed"}:
        return True
    if status in {"error", "failed", "rejected", "not_found"}:
        return False
    if res.get("error") or res.get("_exception"):
        return False
    if "status" not in res:
        return bool(res.get("data"))
    # Statut inconnu (ex: "pending") : ni succès confirmé, ni erreur — False
    # explicite (comportement historique : fallthrough None, même falsiness).
    return False


def norm_intent(intent: Any) -> str:
    return str(intent or "UNKNOWN").upper().split(".")[-1]


def extract_first_int(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"\b(\d{1,3})\b", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def merge_payload(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    """Fusionne deux payloads conversationnels en ignorant les valeurs vides.

    - Conserve les clés existantes si la nouvelle valeur est vide (None, "", [], {}).
    - Supporte la directive ``{"__reset__": True}`` pour remettre une sous-structure à zéro.
    - Fusionne récursivement les dictionnaires imbriqués.
    """

    def _has_value(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
        if value == [] or value == {}:
            return False
        return True

    merged = dict(base or {})
    updates = updates or {}

    if updates.get("__reset__"):
        return {}

    for key, value in updates.items():
        if isinstance(value, dict) and value.get("__reset__"):
            merged.pop(key, None)
            continue

        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_payload(merged.get(key), value)
            continue

        if not _has_value(value):
            continue

        merged[key] = value

    return merged


class MarketRuntime:
    def __init__(
        self,
        llm_client: Any = None,
        transport_config: MCPTransportConfig | None = None,
        mcp_session: Any = None,
        db_service: Any = None,
    ):
        # Override explicite (tests / appelants legacy) — sinon `self.llm`
        # (property ci-dessous) retombe sur le cache module de `get_llm()`.
        self._llm_override: Any = llm_client

        # If orchestrator already provides a session/client, use it directly.
        self.db_client = mcp_session
        self.transport_config = None
        if mcp_session is None:
            self.transport_config = (
                transport_config or self._load_transport_from_settings()
            )

        self.security = SecurityService(self.llm)
        self.db_service = db_service
        # Goal courant du tour — mis à jour automatiquement par `_safe_node`
        # (voir plus bas) à chaque nœud du graphe, SANS qu'aucun nœud n'ait à
        # le faire lui-même. Pilote `model_answer` (routage LLM par goal).
        self.current_goal: Optional[str] = None
        # Téléphone de l'utilisateur courant, lié une fois par tour via
        # `bind_user()` (orchestrator). Filet de sécurité pour la dérivation
        # d'identité MCP — voir `_build_context_identity`.
        self._bound_phone: Optional[str] = None

    @property
    def llm(self) -> Any:
        """Client LLM (Groq), mis en cache au niveau module par `get_llm()`.

        Jamais ré-initialisé, quel que soit le modèle demandé : pour Groq, le
        modèle est un paramètre PAR APPEL de `chat.completions.create(...)`,
        pas une propriété du client — inutile (et coûteux) d'instancier un
        client différent par modèle. Un override explicite passé au
        constructeur (tests, appelants legacy) reste prioritaire.
        """
        if self._llm_override is not None:
            return self._llm_override
        return get_llm()

    @property
    def model_answer(self) -> str:
        """Modèle Groq à utiliser pour l'appel LLM du tour en cours.

        Résolu dynamiquement selon `self.current_goal` via
        `llm_router.get_model_for_goal()` — jamais figé au constructeur.
        Tous les sites d'appel existants font
        `getattr(mc_runtime, "model_answer", ...)`, donc ce passage en
        property est totalement transparent (aucun changement de nœud requis).

        DEPRECATED pour les nouveaux appels (2026-09-02) : les nodes migrés
        au LLM Gateway utilisent `profile_answer` + `llm_gateway.complete(...)`
        à la place — un nom de modèle ne doit plus jamais être choisi
        directement par un node métier. Conservé pour les call sites pas
        encore migrés (voir plan LLM Gateway, `services/memory/*`)."""
        from ladini.graphs.agents.market_coach.llm_router import get_model_for_goal

        return get_model_for_goal(self.current_goal)

    @property
    def profile_answer(self):
        """`LLMProfile` (FAST/REASONING) du tour en cours — à passer tel quel
        à `llm_gateway.complete(profile=...)`. Même résolution dynamique que
        `model_answer` (via `self.current_goal`), juste exprimée en profil
        plutôt qu'en nom de modèle — voir `llm_router.get_profile_for_goal`."""
        from ladini.graphs.agents.market_coach.llm_router import (
            get_profile_for_goal,
        )

        return get_profile_for_goal(self.current_goal)

    @property
    def llm_gateway(self):
        """LLM Gateway (2026-09-02) — point d'entrée UNIQUE pour tout nouvel
        appel LLM métier : `await mc_runtime.llm_gateway.complete(profile=
        mc_runtime.profile_answer, messages=..., response_format=...)`.

        Singleton process-wide (comme `get_llm()`) — voir
        `llm_gateway/gateway.py::get_llm_gateway()`. Pas d'override par
        instance en production : la santé/le disjoncteur DOIVENT être
        partagés à travers tout le process (et, via Redis, tous les
        workers), jamais réinitialisés par tour ou par MarketRuntime.

        EXCEPTION délibérée : si `_llm_override` est fourni (tests / appelants
        legacy — voir `self.llm` ci-dessus), retourne un `LegacyOverrideGateway`
        qui appelle CE client directement, sans toucher Redis/le registry —
        indispensable pour préserver la philosophie "AUCUN réseau" de la
        suite de tests (`tests/conftest.py`) : le vrai Gateway interroge
        Redis à CHAQUE décision de routage (`HealthRegistry.get`), ce qui
        casserait tout test injectant un `ScriptedLLM` sans double Redis."""
        from ladini.graphs.agents.market_coach.llm_gateway import (
            LegacyOverrideGateway,
            get_llm_gateway,
        )

        if self._llm_override is not None:
            return LegacyOverrideGateway(self._llm_override, lambda: self.model_answer)
        return get_llm_gateway()

    def set_current_goal(self, goal: Optional[str]) -> None:
        """Met à jour le goal courant, consommé par `model_answer`.

        Appelé automatiquement par `_safe_node` (wrapper unique de tous les
        nœuds du graphe) — aucun nœud n'a besoin de l'appeler explicitement.
        """
        cleaned = str(goal or "").strip().upper()
        self.current_goal = cleaned or None

    def bind_user(self, phone: Optional[str]) -> None:
        """Lie le téléphone de l'utilisateur courant au runtime pour tout le tour.

        À appeler une fois, dès que le téléphone est connu (avant tout appel
        MCP), typiquement dans l'orchestrateur au début du traitement d'un
        message. Sert de filet de sécurité : si un site d'appel oublie de
        transmettre `phone`/`buyer_phone`/etc. à un tool, l'identité de
        permission retombe quand même sur cet utilisateur au lieu d'échouer
        en ``PermissionDenied("missing_context_identity")``.
        """
        cleaned = str(phone or "").strip()
        self._bound_phone = cleaned or None

    def _load_transport_from_settings(self) -> MCPTransportConfig:
        try:
            return MCPTransportConfig.from_settings(settings)
        except Exception as exc:
            raise MarketRuntimeError(f"Configuration MCP invalide: {exc}") from exc

    async def __aenter__(self) -> MarketRuntime:
        """Initialise la connexion MCP."""
        if self.db_client is not None:
            return self

        if not self.transport_config:
            raise MarketRuntimeError("Configuration transport MCP manquante.")

        self.db_client = AgriMCPClient(self.transport_config)
        await self.db_client.__aenter__()

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.db_client:
            await self.db_client.__aexit__(exc_type, exc_val, exc_tb)
            self.db_client = None

    def ensure_db(self) -> Any:
        """Lazy-load an AgriDatabaseService instance shared across the runtime.

        .. deprecated::
            Use MCP gateways (``services/mcp/gateway.py``) instead of direct
            DB access.  All MarketCoach code should call MCP tools through
            typed gateways so that rate-limiting, timeouts, and audit apply
            uniformly.
        """
        import warnings

        warnings.warn(
            "ensure_db() bypasses MCP tooling — use a gateway instead",
            DeprecationWarning,
            stacklevel=2,
        )
        if not ToolScopeManager.can_use_direct_db():
            raise MarketRuntimeError(
                "Accès DB direct bloqué par la politique de scope courante."
            )
        if self.db_service is not None:
            return self.db_service
        try:
            from ladini.services.database import AgriDatabaseService

            self.db_service = AgriDatabaseService()
            logger.info(
                "MarketRuntime DB service initialised (id=%s)", hex(id(self.db_service))
            )
        except Exception as exc:
            logger.warning(
                "MarketRuntime unable to initialise AgriDatabaseService: %s", exc
            )
            self.db_service = None
        return self.db_service

    def get_mcp_runtime(self) -> Any:
        """Expose the underlying MCP runtime/session when available."""
        return self.db_client

    def _build_context_identity(
        self, kwargs: Dict[str, Any]
    ) -> Optional[FarmerContext]:
        """Dérive l'identité de contexte MCP pour le gate de permission.

        Filet de sécurité GLOBAL (voir ``bind_user``) : si aucun kwarg
        d'identité n'est présent sur CET appel précis (un gateway qui a oublié
        de transmettre ``phone``, ou un tool appelé par id métier seul comme
        ``get_auction_bids(auction_id=...)``), on retombe sur le téléphone lié
        au runtime pour tout le tour en cours. Ceci n'altère PAS les kwargs
        envoyés à la méthode DB elle-même (qui reste inchangée) — seule
        l'identité de permission en bénéficie. Corrige une classe entière de
        bugs « PermissionDenied silencieux » sans devoir patcher chaque site
        d'appel individuellement.
        """
        user_id = str(
            kwargs.get("user_id")
            or kwargs.get("producer_id")
            or kwargs.get("buyer_id")
            or kwargs.get("phone")
            or kwargs.get("user_phone")
            or kwargs.get("buyer_phone")
            or kwargs.get("customer_phone")
            or self._bound_phone
            or ""
        ).strip()
        phone = str(
            kwargs.get("phone")
            or kwargs.get("user_phone")
            or kwargs.get("buyer_phone")
            or kwargs.get("customer_phone")
            or self._bound_phone
            or ""
        ).strip()
        if not user_id and not phone:
            context_identity = get_mcp_context()
            return context_identity
        session_id = str(kwargs.get("session_id") or uuid.uuid4())
        return FarmerContext(
            user_id=user_id or phone,
            phone_number=phone or "unknown",
            session_id=session_id,
        )

    async def call_db(
        self, tool_name: str, *, idempotency_key: Optional[str] = None, **kwargs: Any
    ) -> Dict[str, Any]:
        """Single entry point for all MCP tool calls.

        Responsibilities consolidated here (no other layer should duplicate):
        1. Strip None values (Postel’s Law)
        2. ASCII-fold string arguments
        3. Set FarmerContext scope
        4. Generate request_id for cross-layer tracing
        5. Call the MCP client
        6. Normalise the response via ensure_dict
        7. Log success/failure with request_id

        `idempotency_key` (2026-09-03, mandat §8) : transmis TEL QUEL à
        `AgriMCPClient.call_tool` — identifie la TENTATIVE LOGIQUE (ex:
        `procurement:{draft_id}:{version}`), pas chaque essai réseau. Portée
        honnête (voir `domain/procurement_draft.py::execution_key`
        docstring) : `AgriDBMCPServer.call_tool` (runtime.py) retire cette
        clé AVANT dispatch — aucune déduplication CÔTÉ SERVEUR aujourd'hui.
        Elle sert la CORRÉLATION (log MCP_CALL_AUDIT, Langfuse) et le retry
        interne à `AgriMCPClient` (rejoue la MÊME clé après une coupure
        réseau, jamais une nouvelle par tentative) — pas encore l'exactly-once
        externe.
        """
        if not ToolScopeManager.can_call_tool(tool_name):
            raise MarketRuntimeError(
                f"Tool '{tool_name}' interdit dans le scope '{ToolScopeManager.current_scope().name}'."
            )

        if not self.db_client:
            raise MarketRuntimeError("Runtime non connecté. Utilisez ‘async with’.")

        safe_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        safe_kwargs = _ascii_fold_value(safe_kwargs)

        # CORRECTIF TRANSPORT (voir bind_user) : en production, MCP_DB_TRANSPORT
        # ="stdio" — le serveur DB tourne dans un PROCESSUS SÉPARÉ (db_server.py).
        # Le `mcp_context_scope` posé ci-dessous (contextvar) ne vit que dans CE
        # processus-ci et ne traverse JAMAIS la frontière stdio : côté serveur,
        # `AgriDBMCPServer.call_tool` ne voit QUE les clés JSON envoyées dans
        # `safe_kwargs`. Sans un identifiant DANS ces kwargs, l'identité y est
        # introuvable même si `bind_user()` a été appelé ici. On injecte donc
        # `_caller_phone` — un nom sentinelle qui ne collisionne avec AUCUN
        # paramètre métier réel — pour que l'identité traverse le fil. Le serveur
        # le retire TOUJOURS avant d'invoquer la méthode DB (jamais un TypeError
        # pour les tools qui n'acceptent pas `phone`). Voir runtime.py:call_tool.
        if self._bound_phone and not any(
            k in safe_kwargs
            for k in (
                "phone",
                "user_phone",
                "buyer_phone",
                "customer_phone",
                "producer_id",
                "buyer_id",
                "user_id",
            )
        ):
            safe_kwargs["_caller_phone"] = self._bound_phone

        request_id = str(uuid.uuid4())[:8]
        try:
            context_manager = self._context_scope(safe_kwargs)
            with context_manager:
                raw = await self.db_client.call_tool(
                    tool_name, safe_kwargs, idempotency_key=idempotency_key
                )
        except Exception as exc:
            logger.error(
                "MCP_CALL_FAILURE | rid=%s | tool=%s | keys=%s | error=%s",
                request_id,
                tool_name,
                json.dumps(list(safe_kwargs.keys())),
                str(exc),
            )
            raise

        result = unwrap_tool_envelope(ensure_dict(raw))
        if isinstance(result, dict):
            result["_request_id"] = request_id
        return result

    def ensure_ready(self):
        """Vérifie si le runtime est prêt et connecté."""
        if self.db_client is None:
            raise MarketRuntimeError(
                "Le Runtime n'est pas initialisé. "
                "Veuillez fournir mcp_session ou utiliser le bloc 'async with mc_runtime:'."
            )

    def _context_scope(self, kwargs: Dict[str, Any]):
        context = self._build_context_identity(kwargs)
        if context is None:
            return nullcontext()
        return mcp_context_scope(context)

    @classmethod
    def from_auto_path(cls, llm_client: Any = None) -> MarketRuntime:
        """Gardé pour compatibilité, mais maintenant alias du constructeur par défaut."""
        return cls(llm_client=llm_client)


def build_runtime(
    llm_client: Any = None, transport_config: MCPTransportConfig | None = None
) -> MarketRuntime:
    """
    Factory pour instancier le MarketRuntime.

    Args:
        llm_client: Client LLM (ex: Groq, OpenAI). Si None, le runtime utilisera get_llm().
        transport_config: Configuration de transport MCP. Si None, lit depuis settings.

    Returns:
        Une instance prête de MarketRuntime (nécessite ensuite 'async with' pour la connexion).
    """
    return MarketRuntime(llm_client=llm_client, transport_config=transport_config)


def build_runtime_from_session(
    llm_client: Any = None, mcp_session: Any = None
) -> MarketRuntime:
    """Factory pour runtime basé sur une session MCP déjà ouverte."""
    return MarketRuntime(llm_client=llm_client, mcp_session=mcp_session)


def _compute_progress(
    goal: Optional[str], payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Progression de remplissage des champs requis du but courant.

    Les champs auto-résolus (`_AUTO_RESOLVABLE_FIELDS`) sont exclus du calcul :
    `total`, `filled`, `remaining` et `pct` portent tous sur le même ensemble
    de champs demandés à l'utilisateur.
    """
    if not goal:
        return None
    config = INTENT_CONFIG.get(goal) or {}
    required = list(config.get("required") or [])
    if not required:
        return None
    user_fields = [f for f in required if f not in _AUTO_RESOLVABLE_FIELDS]
    if not user_fields:
        return None
    filled = [f for f in user_fields if payload.get(f) not in (None, "", [], {})]
    remaining = [f for f in user_fields if f not in filled]
    return {
        "total": len(user_fields),
        "filled": len(filled),
        "remaining": remaining,
        "pct": round(len(filled) / len(user_fields) * 100),
    }


# =====================================================================
# TEXT / QUANTITY NORMALIZATION
# =====================================================================


def _now() -> float:
    """Current Unix timestamp (seconds, float)."""
    return time.time()


def _normalize_text(raw: str) -> str:
    """Collapse whitespace and strip — for safe LLM input."""
    if not raw:
        return ""
    return re.sub(r"\s+", " ", raw).strip()


def _normalize_quantity_to_kg(
    payload: Dict[str, Any], *, refresh_display: bool = False
) -> Dict[str, Any]:
    """Standardise the canonical (quantity, unit) pair to kilograms.

    `quantity_display`/`original_quantity`/`unit_display`/`original_unit`
    preserve the user's ORIGINAL wording for the confirmation recap (show
    "2 TONNE" instead of the internally-converted "2000 KG"). They used
    `setdefault` unconditionally, which was deliberate for the common case —
    `transaction_payload` is a `merge_dict` channel, so this function re-runs
    on the SAME already-converted payload every turn (even ones that never
    touch quantity/unit again, e.g. answering just `deadline`); `setdefault`
    stops that re-run from clobbering the once-established original wording
    with the internal KG value.

    Bug réel (2026-09-03, incident PROCUREMENT_CREATE_REQUEST) : cette même
    protection empêchait aussi une VRAIE correction de quantité/unité
    ("non, plutôt 1 tonne et 125 kg" après "2 tonnes et 250 kg") de jamais
    rafraîchir l'affichage — `quantity`/`unit` (le contrat d'exécution)
    étaient bien mis à jour par `_apply_slot`, mais le récapitulatif
    (`services/ui/confirmation_summary.py::_format_quantity`, qui préfère
    `quantity_display`) restait figé sur la toute première valeur, tour après
    tour, alors même que l'action confirmée aurait utilisé la BONNE quantité.
    `refresh_display=True` (passé par l'appelant quand quantity/unit ont
    RÉELLEMENT changé CE tour — voir `nodes/memory.py`) force le
    rafraîchissement au lieu de `setdefault`, sans changer le comportement
    du cas commun (tour qui ne touche pas quantity/unit)."""

    normalized = dict(payload or {})
    qty = normalized.get("quantity")
    unit_original_value = normalized.get("unit")
    unit = str(unit_original_value or "").strip().upper()

    def _set(key: str, value: Any) -> None:
        if refresh_display:
            normalized[key] = value
        else:
            normalized.setdefault(key, value)

    if slot_has_value(qty):
        _set("quantity_display", qty)
        _set("original_quantity", qty)

    if slot_has_value(unit_original_value):
        unit_display_upper = str(unit_original_value).strip().upper()
        unit_display = canonical_unit_label(unit_display_upper, unit or "KG")
        _set("unit_display", unit_display)
        _set("original_unit", unit_display)

    if not slot_has_value(qty) or not unit:
        return normalized

    try:
        qf = float(qty)
    except (TypeError, ValueError):
        return normalized

    if unit in {"TONNE", "TONNES", "T", "TON", "TONS", "TONE", "TONES"}:
        normalized["quantity"] = qf * 1000.0
        normalized["unit"] = "KG"
        normalized["unit_conversion"] = {
            "from_unit": normalized.get("original_unit")
            or canonical_unit_label(unit, "TONNE"),
            "to_unit": "KG",
            "factor": 1000,
            "original_quantity": normalized.get("original_quantity", qf),
            "converted_quantity": normalized["quantity"],
        }
    elif unit in {"G", "GRAMME", "GRAMMES"}:
        normalized["quantity"] = qf * 0.001
        normalized["unit"] = "KG"
        normalized.setdefault("unit_display", "KG")
        normalized.setdefault("original_unit", "KG")
        normalized["unit_conversion"] = {
            "from_unit": canonical_unit_label(unit, "KG"),
            "to_unit": "KG",
            "factor": 0.001,
            "original_quantity": normalized.get("original_quantity", qf),
            "converted_quantity": normalized["quantity"],
        }
    elif unit in {"KG", "KILO", "KILOS", "KILOGRAMME", "KILOGRAMMES"}:
        normalized["quantity"] = qf
        normalized["unit"] = "KG"
        normalized.setdefault("unit_display", "KG")
        normalized.setdefault("original_unit", "KG")
    return normalized


def _clean_candidate_text(value: Optional[str]) -> Optional[str]:
    """Strip surrounding quotes, whitespace, and collapse spaces."""
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip(" '\"\n\r\t")
    return cleaned or None


# =====================================================================
# LLM-BASED ONBOARDING FIELD EXTRACTION
# =====================================================================


async def _llm_extract_onboarding_all(
    mc_runtime: "MarketRuntime",
    user_text: str,
    context_hint: str = "",
) -> Dict[str, Optional[str]]:
    """Extract every onboarding field from a single utterance in ONE LLM call —
    and, when the message carries no usable registration data, ALSO generate
    the reply text in the same call (key ``reply``).

    Why generate rather than pick from a fixed set of canned strings: any
    hardcoded response to "the user asked something / seemed confused /
    was hostile" only covers the specific cases someone thought to write —
    the next real conversation always finds a new one. The LLM gets grounding
    hints (what Ladini is, what's already known, what's still missing,
    how many times this user has already been answered this session) via
    *context_hint* instead, and composes an answer that actually fits what
    was said. See [[onboarding-adaptive-questions-2026-08]].

    Returns a dict with keys role / name / zone / confirm / is_question /
    reply. This replaces per-field sequential extraction so onboarding stays
    sub-second even when the user says everything at once (or corrects
    several fields mid-flow).
    """
    empty: Dict[str, Optional[str]] = {
        "role": None,
        "name": None,
        "zone": None,
        "confirm": None,
        "is_question": False,
        "reply": None,
    }
    if not user_text or not user_text.strip():
        return empty
    llm = getattr(mc_runtime, "llm", None)
    if llm is None:
        return empty

    system_prompt = (
        "Tu extrais des informations d'inscription Ladini a partir d'un message utilisateur, "
        "ET tu generes une reponse adaptee quand le message ne fournit aucune info exploitable.\n\n"
        "Contexte Ladini (base-toi dessus pour repondre, ne le recopie JAMAIS mot pour mot) : "
        "plateforme qui connecte producteurs et acheteurs agricoles directement par WhatsApp, sans "
        "intermediaire ni deplacement ; inscription gratuite et rapide ; on peut inscrire un compte "
        "pour quelqu'un d'autre (ex: un parent) en repondant a sa place ; les producteurs publient "
        "leurs recoltes et gerent leur stock, les acheteurs cherchent des produits et commandent.\n\n"
        f"Etat actuel de cette inscription : {context_hint or 'debut de la conversation.'}\n\n"
        "L'utilisateur peut donner plusieurs informations dans n'importe quel ordre, ou juste une, "
        "ou corriger une valeur precedente. Ne devine JAMAIS a partir d'une salutation ou d'une politesse.\n\n"
        "Reponds STRICTEMENT en JSON avec exactement ces 6 cles (mets null si non applicable) :\n"
        '{"role": "BUYER"|"PRODUCER"|null, "name": string|null, "zone": string|null, '
        '"confirm": "YES"|"NO"|null, "is_question": true|false, "reply": string|null}\n\n'
        "Regles :\n"
        "- role : 'PRODUCER' pour agriculteur, eleveur, producteur, fournisseur d'engrais/intrants/semences. "
        "'BUYER' pour acheteur, commercant, grossiste, client, revendeur. Sinon null.\n"
        "- name : uniquement un vrai nom de personne (prenom, nom complet). Jamais un role, une ville, une "
        "salutation, une profession generique. Jamais non plus un terme de lien familial employe SEUL "
        "('papa', 'pere', 'daron', 'vieux', 'maman', 'mere', 'tonton', 'shao') meme dans 'je cree un compte "
        "pour mon daron/papa' — c'est une DESCRIPTION de la personne, pas son nom ; renvoie null dans ce cas "
        "(exemple : 'je veux creer un compte pour mon daron' -> name: null). "
        "Seule exception : le terme est suivi d'un vrai prenom ('mon pere Ibrahim' -> name: 'Ibrahim').\n"
        "- zone : uniquement une localite (ville, province, region, quartier). Jamais un nom de personne.\n"
        "- confirm : 'YES' si l'utilisateur valide/accepte/confirme explicitement. 'NO' s'il refuse, corrige ou dit que c'est faux. "
        "Sinon null (ne devine pas depuis un simple bonjour ou une info non liee).\n"
        "- is_question : true si le message ne fournit AUCUNE info d'inscription exploitable — question, "
        "doute, hesitation, remarque hostile/insultante, situation inhabituelle, message hors-sujet. Sinon false.\n"
        "- reply : SEULEMENT si is_question=true. Redige une reponse COURTE (2-3 phrases max), chaleureuse, "
        "en francais simple, adaptee precisement a CE message (pas un texte generique). Base-toi sur le "
        "contexte Ladini et sur l'etat actuel ci-dessus. Si l'utilisateur a deja ete relance plusieurs "
        "fois dans cette session, NE REPETE PAS le meme pitch : sois plus bref, ou rassure-le s'il doute de "
        "la legitimite du service, sans jamais etre agressif meme si le message est hostile — reste calme et "
        "professionnel. Termine si naturel en invitant a donner ce qui manque, sans forcer. "
        "Si is_question=false, mets reply a null."
    )
    try:
        # LLM Gateway (2026-09-02) : budget/repli/disjoncteur portés par le
        # Gateway — voir `llm_gateway/gateway.py`.
        completion = await resolve_gateway(mc_runtime).complete(
            profile=resolve_profile(mc_runtime),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text.strip()},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=350,
            agent_node="onboarding_bulk_extract",
        )
        payload = json.loads(completion.choices[0].message.content or "{}")
    except Exception as exc:
        logger.warning("ONBOARDING_BULK_EXTRACT_ERROR | %s", exc)
        return empty

    logger.info(
        "ONBOARDING_BULK_EXTRACT | input=%r | output=%s", user_text[:120], payload
    )

    _CONFIRM_NORMALIZE: Dict[str, str] = {
        "OUI": "YES",
        "NON": "NO",
        "OK": "YES",
        "CORRECT": "YES",
        "EXACT": "YES",
        "TRUE": "YES",
        "FALSE": "NO",
        "VRAI": "YES",
        "FAUX": "NO",
    }

    def _norm(value: Any, allowed: Optional[set] = None) -> Optional[str]:
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        if allowed is not None:
            up = s.upper()
            return up if up in allowed else None
        return s

    raw_confirm = _norm(payload.get("confirm"), {"YES", "NO"})
    if raw_confirm is None:
        raw_val = str(payload.get("confirm") or "").strip().upper()
        raw_confirm = _CONFIRM_NORMALIZE.get(raw_val)

    return {
        "role": _norm(payload.get("role"), {"BUYER", "PRODUCER"}),
        "name": _norm(payload.get("name")),
        "zone": _norm(payload.get("zone")),
        "confirm": raw_confirm,
        "is_question": bool(payload.get("is_question")),
        "reply": _norm(payload.get("reply")),
    }


# =====================================================================
# LLM DEVIATION REPLY — un seul point d'implémentation pour "l'utilisateur
# a dit autre chose que ce qu'on attendait à cette étape". Réutilisé par
# nodes/confirmation_gate.py ET flows/buyer/gps_delivery_gate.py, qui
# avaient chacun leur propre copie légèrement différente avant cette
# consolidation — exactement le genre de duplication qui rendait la
# précommande fragile à chaque nouvelle exigence (adaptivité, GPS...).
# Voir [[precommande-architecture-consolidation-2026-08]].
# =====================================================================


async def llm_deviation_reply(
    mc_runtime: "MarketRuntime",
    user_text: str,
    context: str,
    *,
    extra_instructions: str = "",
) -> Optional[str]:
    """Réponse COURTE générée par le LLM quand l'utilisateur dévie de ce
    qu'une étape à choix contraint attend (confirmation oui/non, partage
    GPS...) — question, correction, remarque, hésitation, message hostile.
    Sans ceci, le texte figé de l'étape se répétait mot pour mot en boucle
    quoi que dise l'utilisateur (bug réel observé dans l'onboarding, dans
    `confirmation_gate` ET dans la précommande — la même classe de bug
    corrigée une seule fois ici plutôt que réinventée à chaque endroit).

    `context` décrit ce qui est en attente (le récap, ou "un point GPS de
    livraison"...) — le LLM s'en sert pour rester pertinent SANS jamais le
    recopier mot pour mot (l'appelant réaffiche ce contexte juste après).
    Retombe sur `None` si le LLM échoue ou n'est pas disponible : l'appelant
    garde alors son texte de repli habituel — jamais de crash, jamais de
    réponse vide."""
    llm = getattr(mc_runtime, "llm", None)
    if llm is None or not user_text:
        return None
    prompt = (
        "Un utilisateur Ladini (WhatsApp, Burkina Faso) devait répondre "
        "quelque chose de précis à cette étape et a dit autre chose.\n\n"
        f"Ce qui est attendu à cette étape : {context}\n\n"
        f'Message de l\'utilisateur : "{user_text}"\n\n'
        "Réponds en 1-2 phrases courtes, chaleureuses, en français simple : "
        "reconnais ce qu'il a dit (question, correction, hésitation, remarque "
        "hostile — reste calme et professionnel même si le message est hostile) "
        "SANS jamais répéter mot pour mot ce qui est attendu (ce sera rappelé "
        f"juste après ta réponse). {extra_instructions}"
    ).strip()
    try:
        # LLM Gateway (2026-09-02) : budget/repli/disjoncteur portés par le
        # Gateway — voir `llm_gateway/gateway.py`.
        completion = await resolve_gateway(mc_runtime).complete(
            profile=resolve_profile(mc_runtime),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=150,
            agent_node="llm_deviation_reply",
        )
        text = (completion.choices[0].message.content or "").strip()
        return text or None
    except Exception as exc:
        logger.warning("llm_deviation_reply: LLM call failed: %s", exc)
        return None


# =====================================================================
# STATE RESET HELPERS
# =====================================================================


def reset_error_status(state: Dict[str, Any]) -> Dict[str, Any]:
    """Reset error-related flags to allow re-planning after a failure."""
    return {
        "status": "PLANNING",
        "validation_errors": [],
        "execution_authorized": False,
        "is_certified": False,
        "retry_count": 0,
    }


__all__ = [
    "MarketRuntime",
    "MarketRuntimeError",
    "_maybe_await",
    "_safe_node",
    "ensure_dict",
    "unwrap_tool_envelope",
    "is_success_response",
    "norm_intent",
    "extract_first_int",
    "merge_payload",
    "build_runtime",
    "build_runtime_from_session",
    # Text / quantity helpers
    "_now",
    "_normalize_text",
    "_normalize_quantity_to_kg",
    "_clean_candidate_text",
    "canonical_unit_label",
    # LLM helpers
    "_llm_extract_onboarding_all",
    "llm_deviation_reply",
    # Compute / reset
    "_compute_progress",
    "reset_error_status",
    # Constants
    "_AUTO_RESOLVABLE_FIELDS",
    "_GENERIC_TECHNICAL_ERROR",
    "INTENT_CONFIG",
]
