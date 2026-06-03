from __future__ import annotations

import asyncio
import inspect
import json
import logging
import copy
import re
import time
import unicodedata
from typing import Any, Awaitable, Callable, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.actions import (
    MARKET_READ_ACTIONS_MAP,
    MARKET_WRITE_ACTIONS_MAP,
)
from agriconnect.graphs.agents.market_coach.intent import (
    INTENT_CONFIG,
    INTENT_DISAMBIGUATION,
)
from agriconnect.graphs.agents.market_coach.state import MarketAgentState
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    ensure_dict,
    is_success_response,
)

# Refactor: extracted response composition + routing to satellite modules
from .intent_router import response_strategy
from .response_handlers import final_response, _label_for_field



# =====================================================================
# NODE 9 — MCP TOOL EXECUTOR HELPERS
# =====================================================================

def _extract_tool_schema(tool_item: Any) -> Dict[str, Any]:
    """Extrait proprement le schéma de validation d'un outil MCP."""
    if isinstance(tool_item, dict):
        for key in ("inputSchema", "input_schema", "parameters"):
            if isinstance(tool_item.get(key), dict):
                return tool_item[key]
        fn = tool_item.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("parameters"), dict):
            return fn["parameters"]
    for attr in ("inputSchema", "input_schema", "parameters"):
        schema = getattr(tool_item, attr, None)
        if isinstance(schema, dict):
            return schema
    return {"type": "object", "properties": {}, "required": []}


def _extract_tool_name(tool_item: Any) -> Optional[str]:
    """Extrait le nom identifiant de l'outil MCP."""
    if isinstance(tool_item, dict):
        fn = tool_item.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            return str(fn["name"])
        if tool_item.get("name"):
            return str(tool_item["name"])
    name = getattr(tool_item, "name", None)
    return str(name) if name else None


async def _list_mcp_tools(mc_runtime: Any) -> List[Any]:
    """Liste l'ensemble des outils déclarés sur le client MCP."""
    client = getattr(mc_runtime, "db_client", None)
    if client is None:
        return []

    # Prefer list_tools() (returns OpenAI-format dicts with schema)
    for method_name in ("list_tools", "get_tools_for_langchain"):
        fn = getattr(client, method_name, None)
        if fn is None:
            continue
        raw = fn()
        if inspect.isawaitable(raw) or asyncio.iscoroutine(raw):
            raw = await raw
        if isinstance(raw, list) and raw:
            return raw

    # Fallback: try a dict-keyed response
    raw = getattr(client, "_tools_cache", None)
    if isinstance(raw, list) and raw:
        return raw
    return []


async def _get_mcp_tool_schema(mc_runtime: Any, tool_name: str) -> Dict[str, Any]:
    """Récupère le schéma associé à un outil spécifique ou renvoie un fallback vide."""
    tools = await _list_mcp_tools(mc_runtime)
    for tool_item in tools:
        if _extract_tool_name(tool_item) == tool_name:
            return _extract_tool_schema(tool_item)
    return {"type": "object", "properties": {}, "required": []}


def _lookup_arg_value(param_name: str, state: Dict[str, Any], payload: Dict[str, Any], initial_args: Dict[str, Any]) -> Any:
    """Recherche une valeur de paramètre de manière stricte (AG-UI).

    Résolution d'identité hiérarchique :
    - producer_id, user_id → préfère UUID du profil (state.user_id)
    - phone, user_phone → state.user_phone
    - Fallback : cherche dans initial_args, payload, entities, state
    """
    # UUID-based identity: prefer real UUID over phone
    if param_name in {"producer_id", "user_id"}:
        # First try real UUID from profile
        uuid_val = state.get("user_id")
        if uuid_val:
            return uuid_val
        # Fallback to phone (schema resolver may still work)
        if state.get("user_phone"):
            return state.get("user_phone")

    if param_name in {"user_phone", "phone"} and state.get("user_phone"):
        return state.get("user_phone")

    sources = [initial_args, payload, state.get("extracted_entities") or {}, state]
    candidate_names = [param_name] + _ARG_ALIASES.get(param_name, [])
    for candidate_name in candidate_names:
        for source in sources:
            if source and source.get(candidate_name) not in (None, "", [], {}):
                return source[candidate_name]
    return None


def _cast_arg_value(value: Any, json_type: str) -> Any:
    """Type et nettoie les arguments pour correspondre rigoureusement aux attentes SQL."""
    if value in (None, "", [], {}):
        return None
    try:
        if json_type == "number":
            return float(str(value).replace(",", ".").replace(" ", ""))
        if json_type == "integer":
            return int(float(str(value).replace(",", ".").replace(" ", "")))
        if json_type == "boolean":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "oui", "y", "on"}
        return str(value)
    except Exception:
        return value


_POSTEL_DEFAULTS: Dict[str, Any] = {
    "string": "",
    "number": 0.0,
    "integer": 0,
    "boolean": False,
    "array": [],
    "object": {},
}


_IDENTITY_ALIASES = frozenset({"user_phone", "phone", "user_id", "producer_id"})
_ARG_ALIASES: Dict[str, List[str]] = {
    "phone": ["user_phone"],
    "user_id": ["producer_id"],
    "producer_id": ["user_id"],
    "product_name": ["product", "name", "item_name", "product_query"],
    "product": ["product_name", "name", "item_name", "product_query"],
    "item_name": ["product", "product_name", "name"],
    # FAILLE 3B — Restrict aliases to avoid semantic collisions.
    # `farm_name` must NEVER be treated as a product `name`.
    "name": ["product", "product_name", "item_name"],
    "farm_name": ["farm_label", "farm_title"],
    "quantity": ["quantity_mentioned", "qty", "quantity_for_sale"],
    "qty": ["quantity_mentioned", "quantity"],
    "quantity_for_sale": ["quantity_mentioned", "quantity", "qty"],
    "unit": ["unit_mentioned"],
    "price": ["price_mentioned", "max_price", "offered_price", "proposed_price"],
    "max_price": ["price_mentioned", "price", "target_price"],
    "offered_price": ["price_mentioned", "price"],
    "proposed_price": ["price_mentioned", "price"],
    "zone": ["zone_name", "zone_query", "zone_id"],
    "zone_name": ["zone", "zone_query", "zone_id"],
    "zone_query": ["zone_name", "zone"],
    "zone_id": ["zone_name", "zone"],
    "latitude": ["lat"],
    "longitude": ["lon", "lng"],
    "lat": ["latitude"],
    "lon": ["longitude", "lng"],
    "transaction_id": ["bid_id", "staging_id"],
    "staging_id": ["transaction_id", "bid_id"],
}


class MissingRequiredMCPArgs(ValueError):
    def __init__(self, tool_name: str, missing_args: List[str]):
        self.tool_name = tool_name
        self.missing_args = missing_args
        super().__init__(f"Missing required MCP args for {tool_name}: {', '.join(missing_args)}")


def _build_resolved_tool_args(tool_name: str, schema: Dict[str, Any], state: Dict[str, Any], payload: Dict[str, Any], initial_args: Dict[str, Any]) -> Dict[str, Any]:
    """Construit le dictionnaire final d'arguments validés par rapport au schéma.

        Apply a *strict* contract:
        - Optional fields that are None are omitted entirely.
        - Required fields MUST be present with a meaningful value (or a schema default).
            Never inject phantom defaults like "", 0, 0.0 to satisfy the schema.
        - All values are cast to schema-declared types.
    """
    properties: Dict[str, Any] = schema.get("properties") or {}
    required: List[str] = list(schema.get("required") or [])

    if not properties:
        # Fallback: no schema available — sanitize initial_args (remove None)
        return {k: v for k, v in (initial_args or {}).items() if v is not None}

    resolved_args: Dict[str, Any] = {}
    resolved_identity = False
    missing_required: List[str] = []
    for param_name, param_schema in properties.items():
        json_type = str(param_schema.get("type") or "string").lower()
        value = _lookup_arg_value(param_name, state, payload, initial_args)

        if param_name in _IDENTITY_ALIASES and value is not None:
            resolved_identity = True

        if value in (None, "", [], {}):
            # FAILLE 3A — Do not inject empty/zero values.
            # If required, only allow a *meaningful* schema default; otherwise mark missing.
            if param_name in required:
                default = param_schema.get("default")
                if default not in (None, "", [], {}):
                    resolved_args[param_name] = _cast_arg_value(default, json_type)
                else:
                    missing_required.append(param_name)
            continue

        resolved_args[param_name] = _cast_arg_value(value, json_type)

    if missing_required:
        raise MissingRequiredMCPArgs(tool_name, missing_required)

    return resolved_args


def _sanitize_mcp_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Final Postel's Law gate before MCP call: remove None, ensure str for str fields."""
    return {k: v for k, v in args.items() if v is not None}


_ASCII_FOLD_TRANSLATION = str.maketrans({
    "œ": "oe",
    "Œ": "OE",
    "æ": "ae",
    "Æ": "AE",
    "’": "'",
    "“": '"',
    "”": '"',
})


def _ascii_fold_str(text: str) -> str:
    """Strip accents/ligatures to keep MCP arguments ASCII-only."""
    if not text:
        return text
    text = text.translate(_ASCII_FOLD_TRANSLATION)
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


# =====================================================================
# MCP ERROR TRANSLATION + POST-SUCCESS SUGGESTIONS
# =====================================================================

_MCP_ERROR_TRANSLATIONS: Dict[str, str] = {
    "not_found": "L'élément demandé n'a pas été trouvé. Vérifiez les données ou reformulez.",
    "duplicate": "Cet enregistrement existe déjà. Voulez-vous le modifier plutôt ?",
    "permission": "Vous n'avez pas les droits pour cette opération.",
    "invalid": "Les données envoyées ne sont pas valides. Vérifiez et réessayez.",
    "stock": "Problème lié au stock. Vérifiez vos quantités.",
    "closed": "Cette enchère ou offre est déjà clôturée.",
}


def _translate_mcp_error(raw_error: str) -> str:
    """Traduit une erreur MCP brute en message utilisateur compréhensible."""
    lower = (raw_error or "").lower()
    for key, msg in _MCP_ERROR_TRANSLATIONS.items():
        if key in lower:
            return msg
    return _GENERIC_TECHNICAL_ERROR


_POST_SUCCESS_SUGGESTIONS: Dict[str, str] = {
    "SALES_PUBLISH_PRODUCT": "Astuce : consultez vos offres en disant \"voir mon stock\".",
    "PROCUREMENT_CREATE_REQUEST": "Vous serez notifié dès qu'un producteur répond.",
    "SALES_PLACE_BID": "Vous pouvez suivre vos offres avec \"mes enchères\".",
    "STOCK_REGISTER_HARVEST": "Vous pouvez maintenant mettre en vente avec \"publier produit\".",
    "STOCK_RECORD_MOVEMENT": "Votre inventaire a été mis à jour.",
}


def _post_success_suggestion(goal: str, payload: Dict[str, Any]) -> Optional[str]:
    """Génère une suggestion proactive après le succès d'une opération."""
    return _POST_SUCCESS_SUGGESTIONS.get(goal)


# =====================================================================
# SELF-HEALING ARG REPAIR — Tentative de réparation autonome des args
# =====================================================================

def _attempt_arg_repair(raw_value: Any, expected_type: str) -> Optional[Any]:
    """Tente de réparer un argument mal typé SANS appel LLM.

    Exemples:
      - "100kg" → 100.0 (pour expected_type='number')
      - "2 tonnes" → 2000.0
      - "trois" → 3.0 (mots-nombres fr courants)
    """
    if raw_value is None:
        return None
    s = str(raw_value).strip().lower()

    if expected_type in ("number", "integer"):
        # Strip common unit suffixes
        for suffix in ("kg", "tonnes", "tonne", "t", "sacs", "sac", "fcfa", "cfa", "f"):
            if s.endswith(suffix):
                s = s[:-len(suffix)].strip()
                break
        # Handle comma as decimal separator
        s = s.replace(",", ".").replace(" ", "").replace("\xa0", "")
        # French word numbers
        _WORD_NUMS = {"un": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
                      "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10,
                      "vingt": 20, "trente": 30, "cinquante": 50, "cent": 100, "mille": 1000}
        if s in _WORD_NUMS:
            return float(_WORD_NUMS[s]) if expected_type == "number" else _WORD_NUMS[s]
        try:
            val = float(s)
            return val if expected_type == "number" else int(val)
        except (ValueError, TypeError):
            return None

    if expected_type == "boolean":
        return s in ("1", "true", "yes", "oui", "ok", "y")

    return str(raw_value)  # string: return as-is


# =====================================================================
# NODE 9 — MCP TOOL EXECUTOR (Self-Healing + Retry)
# =====================================================================

async def mcp_tool_executor(state: Dict[str, Any], mc_runtime: Any) -> Dict[str, Any]:
    """Exécuteur MCP agentique avec auto-réparation d'arguments.

    Capacités :
    1. Dispatcher → args préparés
    2. Schema resolution → args validés
    3. Self-healing sur dispatcher ValueError (tente résolution alternative)
    4. Auto-retry sur erreurs transitoires (timeout, 502, 503)
    5. Arg repair sur rejet MCP (parse erreur, corrige, réessaie)
    6. Traduction erreur MCP → français user-friendly
    7. Suggestion proactive post-succès
    """
    if not state.get("execution_authorized"):
        logger.warning("Executor invoqué sans autorisation — refus d'écriture")
        return {
            "status": "ERROR",
            "validation_errors": ["execution_not_authorized"],
            "response_strategy": "ERROR",
            "ag_ui_component": None,
        }

    goal = (state.get("current_goal") or "").upper()
    payload: Dict[str, Any] = state.get("transaction_payload") or {}
    phone = state.get("user_phone")
    retry_count = int(state.get("retry_count") or 0)

    prep = MARKET_WRITE_ACTIONS_MAP.get(goal) or MARKET_READ_ACTIONS_MAP.get(goal)
    if prep is None:
        logger.error("Aucun dispatcher trouvé pour goal=%s", goal)
        return {
            "status": "ERROR",
            "validation_errors": [f"no_dispatcher_for_{goal}"],
            "response_strategy": "ERROR",
            "selected_tool": None,
            "selected_tool_args": {},
            "ag_ui_component": None,
        }

    # --- SELF-HEALING DISPATCHER CALL ---
    # If dispatcher raises ValueError (missing field), attempt repair from state
    try:
        tool_name, tool_args = prep(payload, str(phone or ""))
    except ValueError as ve:
        # Analyze which field is missing and attempt to find it in state/profile
        missing_field = str(ve).replace("Missing required field: ", "").strip()
        logger.info("[SelfHeal] Dispatcher ValueError for %s: missing '%s' — attempting repair", goal, missing_field)

        # Try to resolve from stable_entities, working_memory, or profile
        repair_sources = [
            state.get("stable_entities") or {},
            state.get("working_memory") or {},
            state.get("extracted_entities") or {},
        ]
        repaired_value = None
        for source in repair_sources:
            candidate = source.get(missing_field)
            if candidate not in (None, "", [], {}):
                repaired_value = candidate
                break

        if repaired_value is not None:
            # Inject repaired value and retry dispatcher
            payload[missing_field] = repaired_value
            logger.info("[SelfHeal] Repaired '%s' = %r from state — retrying dispatcher", missing_field, repaired_value)
            try:
                tool_name, tool_args = prep(payload, str(phone or ""))
            except Exception as exc2:
                logger.error("[SelfHeal] Dispatcher still fails after repair: %s", exc2)
                return {
                    "status": "ERROR",
                    "validation_errors": [f"dispatcher_error_after_repair: {exc2}"],
                    "response_strategy": "ERROR",
                    "final_response": _GENERIC_TECHNICAL_ERROR,
                    "selected_tool": None,
                    "selected_tool_args": {},
                    "ag_ui_component": None,
                }
        else:
            # Cannot self-heal — route back to slot-filling
            logger.warning("[SelfHeal] Cannot repair '%s' — routing to ASK_MISSING_FIELD", missing_field)
            return {
                "status": "WAITING_INPUT",
                "missing_fields": [missing_field],
                "last_missing_field": missing_field,
                "response_strategy": "ASK_MISSING_FIELD",
                "validation_errors": [],
                "selected_tool": None,
                "selected_tool_args": {},
                "ag_ui_component": None,
            }
    except Exception as exc:
        logger.error("Le dispatcher pour %s a échoué: %s", goal, exc)
        return {
            "status": "ERROR",
            "validation_errors": [f"dispatcher_error: {exc}"],
            "response_strategy": "ERROR",
            "final_response": _GENERIC_TECHNICAL_ERROR,
            "selected_tool": None,
            "selected_tool_args": {},
            "ag_ui_component": None,
        }

    tool_schema = await _get_mcp_tool_schema(mc_runtime, tool_name)
    try:
        resolved_args = _build_resolved_tool_args(
            tool_name=tool_name,
            schema=tool_schema,
            state=state,
            payload=payload,
            initial_args=tool_args or {},
        )
    except MissingRequiredMCPArgs as exc:
        # Controlled failure: route back to slot-filling instead of sending nonsense.
        logger.warning("[Executor] %s", str(exc))

        # Map MCP param names to conversational slots when possible.
        mcp_to_slot = {
            "name": "product",
            "product_name": "product",
            "quantity_for_sale": "quantity_mentioned",
            "quantity": "quantity_mentioned",
            "price": "price_mentioned",
            "producer_id": "phone",
            "user_id": "phone",
        }
        missing_slots = [mcp_to_slot.get(m, m) for m in (exc.missing_args or [])]
        missing_slots = [m for m in missing_slots if m]

        first_missing = missing_slots[0] if missing_slots else None
        return {
            "status": "WAITING_INPUT",
            "execution_authorized": False,
            "validation_errors": [f"missing_required_args: {', '.join(exc.missing_args)}"],
            "selected_tool": tool_name,
            "selected_tool_args": {},
            "missing_fields": missing_slots,
            "last_missing_field": first_missing,
            "response_strategy": "ASK_MISSING_FIELD",
            "ag_ui_component": None,
        }
    # Final Postel's Law gate: never pass None to MCP
    resolved_args = _sanitize_mcp_args(resolved_args)
    # AXE 4: ensure MCP never receives accented strings
    resolved_args = _ascii_fold_value(resolved_args)

    logger.info(
        "MCP_EXEC_AUDIT | tool=%s | args=%s",
        tool_name,
        json.dumps(resolved_args, default=str, ensure_ascii=False),
    )
    history = list(state.get("tool_execution_history") or [])

    # --- AUTO-RETRY loop for transient failures ---
    _MCP_MAX_TRANSIENT_RETRIES = 2
    _TRANSIENT_MARKERS = ("timeout", "connection", "unavailable", "temporary", "503", "502")
    last_exc: Optional[Exception] = None

    for attempt in range(1, _MCP_MAX_TRANSIENT_RETRIES + 1):
        try:
            raw = await mc_runtime.call_db(tool_name, **resolved_args)
            result = ensure_dict(raw)
            success = is_success_response(result)

            history.append({
                "tool": tool_name,
                "args": resolved_args,
                "success": success,
                "raw": result,
                "ts": _now(),
                "attempt": attempt,
            })

            if not success:
                err_msg = result.get("message") or result.get("error") or "Transaction rejetée par le système"
                logger.warning("Tool %s rejeté par le MCP (tentative %d): %s", tool_name, attempt, err_msg)
                # Translate MCP errors to user-friendly advice
                user_msg = _translate_mcp_error(str(err_msg))
                return {
                    "status": "ERROR",
                    "execution_result": result,
                    "selected_tool": tool_name,
                    "selected_tool_args": resolved_args,
                    "tool_execution_history": history,
                    "retry_count": retry_count,
                    "validation_errors": [str(err_msg)],
                    "response_strategy": "ERROR",
                    "final_response": user_msg,
                    "ag_ui_component": None,
                }

            # --- POST-SUCCESS proactive suggestion ---
            proactive = _post_success_suggestion(goal, payload)

            success_state: Dict[str, Any] = {
                "status": "COMPLETED",
                "goal_status": "COMPLETED",
                "execution_result": result,
                "selected_tool": tool_name,
                "selected_tool_args": resolved_args,
                "tool_execution_history": history,
                "retry_count": retry_count,
                "is_certified": False,
                "execution_authorized": False,
                "waiting_for_confirmation": False,
                "transaction_payload": {},
                "missing_fields": [],
                "completed_fields": [],
                "expected_input": "NONE",
                "current_goal": None,
                "response_strategy": "SUCCESS",
                "proactive_hint": proactive,
                "ag_ui_component": None,
            }

            if isinstance(result.get("mapping"), dict):
                success_state["available_mapping"] = result["mapping"]
                kind = "auction" if "auction" in tool_name else "bid"
                success_state["working_memory"] = {"available_mapping_kind": kind}

            return success_state

        except Exception as exc:
            last_exc = exc
            exc_lower = str(exc).lower()
            is_transient = any(m in exc_lower for m in _TRANSIENT_MARKERS)
            history.append({
                "tool": tool_name,
                "args": resolved_args,
                "success": False,
                "error": str(exc),
                "ts": _now(),
                "attempt": attempt,
                "transient": is_transient,
            })
            if is_transient and attempt < _MCP_MAX_TRANSIENT_RETRIES:
                logger.warning(
                    "[Executor] Transient error on %s (attempt %d/%d): %s — retrying",
                    tool_name, attempt, _MCP_MAX_TRANSIENT_RETRIES, exc,
                )
                await asyncio.sleep(0.5 * attempt)
                continue
            break

    # All retries exhausted
    logger.exception("Le call MCP %s a crashé après %d tentatives: %s", tool_name, _MCP_MAX_TRANSIENT_RETRIES, last_exc)
    return {
        "status": "ERROR",
        "selected_tool": tool_name,
        "selected_tool_args": resolved_args,
        "tool_execution_history": history,
        "retry_count": retry_count + 1,
        "validation_errors": [f"mcp_crash: {last_exc}"],
        "execution_authorized": False,
        "response_strategy": "ERROR",
        "final_response": _translate_mcp_error(str(last_exc)),
        "ag_ui_component": None,
    }