from __future__ import annotations

import asyncio
import ast
import inspect
import json
import logging
import re
import time
import unicodedata
import uuid
from contextlib import nullcontext
from typing import Any, Awaitable, Callable, Dict, Optional

from agriconnect.core.llm import get_llm
from agriconnect.core.settings import settings
from agriconnect.graphs.agents.market_coach.security import SecurityService
from agriconnect.graphs.agents.market_coach.interpreter.intent import (
    INTENT_CONFIG,
    INTENT_DISAMBIGUATION,
)
from agriconnect.infrastructure.mcp.client import AgriMCPClient, MCPTransportConfig
from agriconnect.infrastructure.mcp.context import FarmerContext, get_mcp_context, mcp_context_scope

logger = logging.getLogger("Agent.MarketCoach")

# ── ASCII folding (Postel's Law — MCP never receives accented strings) ──

_ASCII_LIGATURE_MAP = str.maketrans({"œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE", "’": "'"})


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

_CANONICAL_FIELD_ALIASES: Dict[str, str] = {
    "product_name": "product",
    "produit": "product",
    "item_name": "product",
    "commodity": "product",
    "quantity_mentioned": "quantity",
    "quantity_for_sale": "quantity",
    "quantite": "quantity",
    "qty": "quantity",
    "volume": "quantity",
    "unit_mentioned": "unit",
    "unite": "unit",
    "unit_label": "unit",
    "price_mentioned": "price",
    "prix": "price",
    "prix_unitaire": "price",
    "max_price": "price",
    "montant": "price",
    "zone_name": "zone",
    "zone_label": "zone",
    "location": "zone",
    "localite": "zone",
}

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
        if value is None: return None
        return float(str(value).replace(',', '.'))
    except (ValueError, TypeError):
        return None

def _safe_node(fn: Callable[..., Awaitable[Dict[str, Any]]], name: str) -> Callable[..., Awaitable[Dict[str, Any]]]:
    """Décorateur pour sécuriser les noeuds LangGraph et tracer leur durée."""

    async def _wrapped(state: Dict[str, Any], mc_runtime: "MarketRuntime", **_: Any) -> Dict[str, Any]:
        goal = str(state.get("current_goal") or "").upper()
        status = str(state.get("status") or "").upper()
        event = str(state.get("interpreted_event") or "").upper()
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

    return _wrapped


def ensure_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {"status": "error", "message": "No response"}

    # If MCP already gave us a dict, we may still need to unwrap raw_result.
    if isinstance(obj, dict):
        raw = (obj.get("data") or {}).get("raw_result") if isinstance(obj.get("data"), dict) else None
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
                # Tentative 2 : Nettoyage des guillemets simples (fréquent avec MCP)
                cleaned = obj.replace("'", '"')
                return json.loads(cleaned)
            except:
                try:
                    # Tentative 3 : Littéral Python (ex: "{'status': 'error', ...}")
                    parsed = ast.literal_eval(obj)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    pass

                # String brute non interprétable => traiter comme une erreur.
                logger.warning(f"Réponse brute non-JSON reçue du MCP : {obj[:50]}...")
                return {
                    "status": "error",
                    "data": {"raw_result": obj},
                    "message": "Non-JSON response from MCP"
                }
    
    # Si c'est un objet (ex: Result de MCP)
    if hasattr(obj, "content"): # Format spécifique à certains clients MCP
        return {"status": "ok", "data": obj.content}

    return {"status": "ok", "data": str(obj)}


def is_success_response(res: Dict[str, Any]) -> bool:
    if not res:
        return False
    status = str(res.get("status") or "").lower()
    if status in {"ok", "success", "completed"}:
        return True
    if status in {"error", "failed", "rejected", "not_found"}:
        return False
    if res.get("error") or res.get("_exception"):
        return False
    if "status" not in res:
        return bool(res.get("data"))


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


def friendly_missing(intent: str, missing: list[str], intent_config: Dict[str, Any]) -> list[str]:
    cfg = (intent_config or {}).get(intent, {})
    label_map = cfg.get("label_map", {}) if isinstance(cfg, dict) else {}
    return [label_map.get(field, field) for field in missing]


def build_confirmation_summary(intent: str, payload: Dict[str, Any], intent_config: Dict[str, Any]) -> str:
    cfg = (intent_config or {}).get(intent, {}) if isinstance(intent_config, dict) else {}
    label = cfg.get("label", intent)

    product = (payload or {}).get("product")
    qty = (payload or {}).get("quantity")
    unit = (payload or {}).get("unit")
    price = (payload or {}).get("price")
    currency = (payload or {}).get("currency") or "FCFA"

    lines = [f"✅ {label}"]
    if product:
        lines.append(f"- Produit: {product}")
    if qty is not None:
        lines.append(f"- Quantité: {qty} {unit or ''}".strip())
    if price is not None:
        lines.append(f"- Prix: {price} {currency}")

    lines.append("\nSi c'est correct, répondez juste: OK")
    return "\n".join(lines)



class MarketRuntime:
    def __init__(
        self,
        llm_client: Any = None,
        transport_config: MCPTransportConfig | None = None,
        mcp_session: Any = None,
        db_service: Any = None,
    ):
        self.llm = llm_client or get_llm()

        # If orchestrator already provides a session/client, use it directly.
        self.db_client = mcp_session
        self.transport_config = None
        if mcp_session is None:
            self.transport_config = transport_config or self._load_transport_from_settings()

        self.security = SecurityService(self.llm)
        self.model_answer = "llama-3.3-70b-versatile"
        self.db_service = db_service

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
        if self.db_service is not None:
            return self.db_service
        try:
            from agriconnect.services.database import AgriDatabaseService

            self.db_service = AgriDatabaseService()
            logger.info("MarketRuntime DB service initialised (id=%s)", hex(id(self.db_service)))
        except Exception as exc:
            logger.warning("MarketRuntime unable to initialise AgriDatabaseService: %s", exc)
            self.db_service = None
        return self.db_service

    def get_mcp_runtime(self) -> Any:
        """Expose the underlying MCP runtime/session when available."""
        return self.db_client

    def _build_context_identity(self, kwargs: Dict[str, Any]) -> Optional[FarmerContext]:
        user_id = str(
            kwargs.get("user_id")
            or kwargs.get("producer_id")
            or kwargs.get("buyer_id")
            or kwargs.get("phone")
            or kwargs.get("user_phone")
            or kwargs.get("buyer_phone")
            or kwargs.get("customer_phone")
            or ""
        ).strip()
        phone = str(
            kwargs.get("phone")
            or kwargs.get("user_phone")
            or kwargs.get("buyer_phone")
            or kwargs.get("customer_phone")
            or ""
        ).strip()
        if not user_id and not phone:
            context_identity = get_mcp_context()
            return context_identity
        session_id = str(kwargs.get("session_id") or uuid.uuid4())
        return FarmerContext(user_id=user_id or phone, phone_number=phone or "unknown", session_id=session_id)

    async def call_db(self, tool_name: str, **kwargs: Any) -> Dict[str, Any]:
        """Single entry point for all MCP tool calls.

        Responsibilities consolidated here (no other layer should duplicate):
        1. Strip None values (Postel’s Law)
        2. ASCII-fold string arguments
        3. Set FarmerContext scope
        4. Generate request_id for cross-layer tracing
        5. Call the MCP client
        6. Normalise the response via ensure_dict
        7. Log success/failure with request_id
        """
        if not self.db_client:
            raise MarketRuntimeError("Runtime non connecté. Utilisez ‘async with’.")

        safe_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        safe_kwargs = _ascii_fold_value(safe_kwargs)

        request_id = str(uuid.uuid4())[:8]
        try:
            context_manager = self._context_scope(safe_kwargs)
            with context_manager:
                raw = await self.db_client.call_tool(tool_name, safe_kwargs)
        except Exception as exc:
            logger.error(
                "MCP_CALL_FAILURE | rid=%s | tool=%s | keys=%s | error=%s",
                request_id,
                tool_name,
                json.dumps(list(safe_kwargs.keys())),
                str(exc),
            )
            raise

        result = ensure_dict(raw)
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

def build_runtime(llm_client: Any = None, transport_config: MCPTransportConfig | None = None) -> MarketRuntime:
    """
    Factory pour instancier le MarketRuntime.
    
    Args:
        llm_client: Client LLM (ex: Groq, OpenAI). Si None, le runtime utilisera get_llm().
        transport_config: Configuration de transport MCP. Si None, lit depuis settings.
    
    Returns:
        Une instance prête de MarketRuntime (nécessite ensuite 'async with' pour la connexion).
    """
    return MarketRuntime(llm_client=llm_client, transport_config=transport_config)


def build_runtime_from_session(llm_client: Any = None, mcp_session: Any = None) -> MarketRuntime:
    """Factory pour runtime basé sur une session MCP déjà ouverte."""
    return MarketRuntime(llm_client=llm_client, mcp_session=mcp_session)


# S'intercale après input_interpreter quand le LLM est peu confiant ET que
# le texte contient un déclencheur lexical présent dans `INTENT_DISAMBIGUATION`.
# Si déclenchée, propose un AG-UI ListMenu et court-circuite goal_planner pour
# l'envoyer directement vers response_strategy. La sélection N de l'utilisateur
# au tour suivant est résolue par memory_update via mapping_kind="intent_disambiguation",
# puis exploitée par goal_planner (RÈGLE 0bis) comme un NEW_TASK propre.

# Seuil de confiance LLM en-dessous duquel on autorise la désambiguïsation.
_DISAMBIGUATION_CONFIDENCE_THRESHOLD = 0.85
_RECOVERY_MAX_RETRIES = 2


def _detect_disambiguation_candidates(text_lower: str) -> Optional[Dict[str, Any]]:
    """Cherche dans INTENT_DISAMBIGUATION l'entrée la PLUS SPÉCIFIQUE dont un
    `lexical_hint` matche le texte.

    INTENT_DISAMBIGUATION est un dict {trigger_key: {candidates, title, options,
    lexical_hints}}. On ne retourne plus la première entrée trouvée (l'ordre du
    dict décidait arbitrairement du gagnant — ex: "suivre mes appels" matchait
    ORDER_TRACKING via "suivre" au lieu d'AUCTION_TRACKING). On retient l'entrée
    dont le hint matché est le plus long (signal le plus spécifique), ce qui
    fait gagner "mes appel(s)" sur le générique "suivre".
    """
    best: Optional[Dict[str, Any]] = None
    best_len = 0
    for key, entry in INTENT_DISAMBIGUATION.items():
        for hint in entry.get("lexical_hints") or []:
            h = str(hint).lower().strip()
            if h and h in text_lower and len(h) > best_len:
                best = {"id": key, **entry}
                best_len = len(h)
    return best


def _compute_progress(goal: Optional[str], payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Calcule la progression de remplissage du formulaire pour le but courant."""
    if not goal:
        return None
    config = INTENT_CONFIG.get(goal) or {}
    required = list(config.get("required") or [])
    if not required:
        return None
    filled = [f for f in required if f not in _AUTO_RESOLVABLE_FIELDS and payload.get(f) not in (None, "", [], {})]
    return {
        "total": len([f for f in required if f not in _AUTO_RESOLVABLE_FIELDS]),
        "filled": len(filled),
        "remaining": [f for f in required if f not in _AUTO_RESOLVABLE_FIELDS and f not in [x for x in filled]],
        "pct": round(len(filled) / max(len(required), 1) * 100),
    }


def _build_proactive_hint(goal: Optional[str], progress: Optional[Dict[str, Any]], payload: Dict[str, Any]) -> Optional[str]:
    """Génère un indice proactif contextuel pour guider l'utilisateur."""
    if not goal or not progress:
        return None
    pct = progress.get("pct", 0)
    remaining = progress.get("remaining") or []
    goal_label = (INTENT_CONFIG.get(goal) or {}).get("label", goal)

    if pct == 0:
        return f"Nouvelle opération : {goal_label}"
    if pct >= 100:
        return "Toutes les informations sont réunies, prêt pour confirmation."
    if len(remaining) == 1:
        field_label = _label_for_field(goal, remaining[0])
        return f"Plus qu'une info : {field_label}."
    return f"Progression : {pct}% — encore {len(remaining)} infos nécessaires."



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


def _normalize_quantity_to_kg(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Standardise the canonical (quantity, unit) pair to kilograms."""

    normalized = dict(payload or {})
    qty = normalized.get("quantity")
    unit_original_value = normalized.get("unit")
    unit = str(unit_original_value or "").strip().upper()

    if slot_has_value(qty):
        normalized.setdefault("quantity_display", qty)
        normalized.setdefault("original_quantity", qty)

    if slot_has_value(unit_original_value):
        unit_display_upper = str(unit_original_value).strip().upper()
        unit_display = canonical_unit_label(unit_display_upper, unit or "KG")
        normalized.setdefault("unit_display", unit_display)
        normalized.setdefault("original_unit", unit_display)

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
            "from_unit": normalized.get("original_unit") or canonical_unit_label(unit, "TONNE"),
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
) -> Dict[str, Optional[str]]:
    """Extract every onboarding field from a single utterance in ONE LLM call.

    Returns a dict with keys role / name / zone / confirm. Each is either the
    extracted value or None. This replaces per-field sequential extraction so
    onboarding stays sub-second even when the user says everything at once
    (or corrects several fields mid-flow).
    """
    empty: Dict[str, Optional[str]] = {"role": None, "name": None, "zone": None, "confirm": None}
    if not user_text or not user_text.strip():
        return empty
    llm = getattr(mc_runtime, "llm", None)
    if llm is None:
        return empty

    system_prompt = (
        "Tu extrais des informations d'inscription AgriConnect a partir d'un message utilisateur. "
        "L'utilisateur peut donner plusieurs informations dans n'importe quel ordre, ou juste une, "
        "ou corriger une valeur precedente. Ne devine JAMAIS a partir d'une salutation ou d'une politesse.\n\n"
        "Reponds STRICTEMENT en JSON avec exactement ces 4 cles (mets null si l'info n'est pas explicite) :\n"
        '{\"role\": \"BUYER\"|\"PRODUCER\"|null, \"name\": string|null, \"zone\": string|null, \"confirm\": \"YES\"|\"NO\"|null}\n\n'
        "Regles :\n"
        "- role : 'PRODUCER' pour agriculteur, eleveur, producteur, fournisseur d'engrais/intrants/semences. "
        "'BUYER' pour acheteur, commercant, grossiste, client, revendeur. Sinon null.\n"
        "- name : uniquement un nom de personne (prenom, nom complet). Jamais un role, une ville, une salutation, une profession generique.\n"
        "- zone : uniquement une localite (ville, province, region, quartier). Jamais un nom de personne.\n"
        "- confirm : 'YES' si l'utilisateur valide/accepte/confirme explicitement. 'NO' s'il refuse, corrige ou dit que c'est faux. "
        "Sinon null (ne devine pas depuis un simple bonjour ou une info non liee)."
    )
    try:
        completion = await asyncio.to_thread(
            lambda: llm.chat.completions.create(
                model=getattr(mc_runtime, "model_answer", "llama-3.3-70b-versatile"),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text.strip()},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=200,
            )
        )
        payload = json.loads(completion.choices[0].message.content or "{}")
    except Exception as exc:
        logger.warning("ONBOARDING_BULK_EXTRACT_ERROR | %s", exc)
        return empty

    logger.info("ONBOARDING_BULK_EXTRACT | input=%r | output=%s", user_text[:120], payload)

    _CONFIRM_NORMALIZE: Dict[str, str] = {
        "OUI": "YES", "NON": "NO", "OK": "YES",
        "CORRECT": "YES", "EXACT": "YES", "TRUE": "YES", "FALSE": "NO",
        "VRAI": "YES", "FAUX": "NO",
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
    }


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
        "waiting_for_confirmation": False,
        "retry_count": 0,
    }


__all__ = [
    "MarketRuntime",
    "MarketRuntimeError",
    "_maybe_await",
    "_safe_node",
    "ensure_dict",
    "is_success_response",
    "norm_intent",
    "extract_first_int",
    "merge_payload",
    "friendly_missing",
    "build_confirmation_summary",
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
    # Compute / reset
    "_compute_progress",
    "_build_proactive_hint",
    "_detect_disambiguation_candidates",
    "reset_error_status",
    # Constants
    "_AUTO_RESOLVABLE_FIELDS",
    "_GENERIC_TECHNICAL_ERROR",
    "_DISAMBIGUATION_CONFIDENCE_THRESHOLD",
    "_RECOVERY_MAX_RETRIES",
    "INTENT_CONFIG",
    "INTENT_DISAMBIGUATION",
]



