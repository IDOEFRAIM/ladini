from __future__ import annotations

import asyncio
import ast
import inspect
import json
import logging
import re
import time
import uuid
from contextlib import nullcontext
from typing import Any, Awaitable, Callable, Dict, Optional

from agriconnect.core.get_llm import get_llm
from agriconnect.core.settings import settings
from agriconnect.graphs.agents.market_coach.security import SecurityService
from agriconnect.graphs.agents.market_coach.interpreter.intent import (
    INTENT_CONFIG,
    INTENT_DISAMBIGUATION,
)
from agriconnect.infrastructure.mcp.client import AgriMCPClient, MCPTransportConfig
from agriconnect.infrastructure.mcp.context import FarmerContext, get_mcp_context, mcp_context_scope

logger = logging.getLogger("Agent.MarketCoach")

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
    """Décorateur pour sécuriser les noeuds LangGraph."""
    async def _wrapped(state: Dict[str, Any], mc_runtime: "MarketRuntime", **_: Any) -> Dict[str, Any]:
        try:
            # Ne pas exiger la connexion MCP ici : certains noeuds (NLU, modération)
            # fonctionnent sans accès DB. Les appels DB utiliseront `call_db`
            # qui lèvera une erreur claire si la session MCP manque.
            return await fn(state, mc_runtime)
        except Exception as exc:
            logger.exception("[MarketCoach] node=%s failed: %s", name, exc)
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
    status = str((res or {}).get("status") or "").lower()
    if status in {"ok", "success", "completed"}:
        return True
    if status in {"error", "failed", "rejected", "not_found"}:
        return False
    if (res or {}).get("error"):
        return False
    return "status" not in (res or {})


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


# Backward compat: old private name
_ensure_dict = ensure_dict


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
        """Lazy-load an AgriDatabaseService instance shared across the runtime."""
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
            or ""
        ).strip()
        phone = str(kwargs.get("phone") or kwargs.get("user_phone") or "").strip()
        if not user_id and not phone:
            context_identity = get_mcp_context()
            return context_identity
        session_id = str(kwargs.get("session_id") or uuid.uuid4())
        return FarmerContext(user_id=user_id or phone, phone_number=phone or "unknown", session_id=session_id)

    async def call_db(self, tool_name: str, **kwargs) -> Any:
        """
        Point d'entrée unique et direct pour la base de données.
        Ex: await runtime.call_db("get_farms", producer_id="123")
        """
        if not self.db_client:
            raise MarketRuntimeError("Runtime non connecté. Utilisez 'async with'.")

        import unicodedata

        def _ascii_fold_str(text: str) -> str:
            if not text:
                return text
            text = text.translate(str.maketrans({"œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE", "’": "'"}))
            normalized = unicodedata.normalize("NFKD", text)
            return normalized.encode("ascii", "ignore").decode("ascii")

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

        # Postel's Law: strip None values before forwarding to MCP client
        safe_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        # AXE 4: ensure MCP never receives accented strings
        safe_kwargs = _ascii_fold_value(safe_kwargs)

        try:
            context_manager = self._context_scope(safe_kwargs)
            with context_manager:
                return await self.db_client.call_tool(tool_name, safe_kwargs)
        except Exception as exc:
            logger.error(
                "MCP_CALL_FAILURE | tool=%s | args=%s | error=%s",
                tool_name,
                json.dumps(list(safe_kwargs.keys())),
                str(exc),
            )
            raise

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
    """Cherche dans INTENT_DISAMBIGUATION une entrée dont les `lexical_hints` matchent.

    INTENT_DISAMBIGUATION est un dict {trigger_key: {candidates, title, options, lexical_hints}}.
    Retourne (key, entry) la première paire matchée, ou None.
    """
    for key, entry in INTENT_DISAMBIGUATION.items():
        hints = entry.get("lexical_hints") or []
        for hint in hints:
            if hint and str(hint).lower() in text_lower:
                return {"id": key, **entry}
    return None


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
        normalized.setdefault("original_quantity_mentioned", normalized["original_quantity"])

    if slot_has_value(unit_original_value):
        unit_display_upper = str(unit_original_value).strip().upper()
        unit_display = canonical_unit_label(unit_display_upper, unit or "KG")
        normalized.setdefault("unit_display", unit_display)
        normalized.setdefault("original_unit", unit_display)
        normalized.setdefault("original_unit_mentioned", unit_display)

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

async def _llm_extract_onboarding_field(
    mc_runtime: "MarketRuntime",
    field: str,
    user_text: str,
) -> Optional[str]:
    """Extract a single onboarding field via LLM.

    Expected fields:
    - name
    - zone
    - role (BUYER | PRODUCER | empty)
    - confirm (YES | NO | empty)
    - correction_field (ROLE | NAME | ZONE | empty)
    """
    if not user_text:
        return None
    llm = getattr(mc_runtime, "llm", None)
    if llm is None:
        return None

    instructions = {
        "name": (
            "Retourne uniquement un nom de personne si l'utilisateur donne explicitement son identité. "
            "Ne retourne jamais une salutation, un rôle (acheteur/producteur), une ville, une zone, une profession, "
            "ni un mot ambigu. Si ce n'est pas clairement un nom de personne, retourne vide."
        ),
        "zone": (
            "Retourne uniquement une localité explicite (ville, zone, province, région) si l'utilisateur mentionne son lieu. "
            "Ne retourne jamais un prénom, un nom de personne, une salutation, ni un rôle. "
            "Si ce n'est pas clairement une localité, retourne vide."
        ),
        "role": (
            "Retourne UNIQUEMENT 'BUYER' ou 'PRODUCER' en suivant ces règles : "
            "- PRODUCTEUR = agriculteur, éleveur, fournisseur ou vendeur d'engrais/intrants/semences/outils agricoles (mots clés: engrais, intrants, semences, boutique d'intrants, distributeur, fournisseur). "
            "- BUYER = commerçant qui achète pour revendre, grossiste, consommateur final, client à la recherche d'offres (mots clés: acheter, grossiste, client, revendre). "
            "N'infère jamais le rôle à partir d'un prénom, d'une salutation (bonjour, bonsoir, salut, hello), d'un message de politesse ou d'une simple localité. "
            "Exemples à IGNORER totalement (retour vide): 'bonjour', 'bonsoir je viens aux nouvelles', 'salut c'est Jojo', 'je suis là'. "
            "Si le message n'exprime pas explicitement le rôle avec des mots liés à l'achat ou à la production agricole, retourne vide pour que l'agent repose la question."
        ),
        "confirm": (
            "Retourne 'YES' si l'utilisateur confirme que les informations sont correctes. "
            "Retourne 'NO' si l'utilisateur veut corriger. "
            "Retourne vide si ce n'est pas clair."
        ),
        "correction_field": (
            "Si l'utilisateur veut corriger, retourne UNIQUEMENT: 'ROLE' ou 'NAME' ou 'ZONE'. "
            "Retourne vide si ce n'est pas clair."
        ),
    }
    field_key = field if field in instructions else None
    if field_key is None:
        return None
    system_prompt = (
        "Tu extrais des informations d'onboarding pour AgriConnect. "
        "Réponds uniquement un JSON {\"value\": \"...\"}. "
        f"Instruction: {instructions.get(field_key)}"
    )

    try:
        completion = await asyncio.to_thread(
            lambda: llm.chat.completions.create(
                model=getattr(mc_runtime, "model_answer", "llama-3.3-70b-versatile"),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=60,
            )
        )
        payload = json.loads(completion.choices[0].message.content or "{}")
        value = payload.get("value")

        if field_key == "role":
            val = str(value or "").strip().upper()
            return val if val in {"BUYER", "PRODUCER"} else None

        if field_key == "confirm":
            val = str(value or "").strip().upper()
            if val in {"YES", "NO"}:
                return val
            if val in {"OUI", "NON"}:
                return "YES" if val == "OUI" else "NO"
            return None

        if field_key == "correction_field":
            val = str(value or "").strip().upper()
            return val if val in {"ROLE", "NAME", "ZONE"} else None

        return _clean_candidate_text(value)

    except Exception as exc:
        logger.debug("[Onboarding] LLM extraction failed for %s: %s", field, exc)
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
    "_llm_extract_onboarding_field",
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



