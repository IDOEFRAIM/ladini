import asyncio
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from agriconnect.core.logging import get_logger
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    _normalize_quantity_to_kg,
    _compute_progress,
    canonical_unit_label,
    normalize_slot_keys,
    slot_has_value,
)
from agriconnect.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from agriconnect.graphs.agents.market_coach.nodes.response_handlers import _label_for_field
from agriconnect.graphs.agents.market_coach.core.state_compaction import (
    build_compaction_patch,
)
from agriconnect.graphs.agents.market_coach.services.domain.quantity_unit import (
    UNIT_SYNONYMS as _CANONICAL_UNIT_SYNONYMS,
    normalize_unit_token as _normalize_unit_token_canonical,
    parse_quantity_unit_from_text as _parse_qty_unit,
    extract_unit_only_from_text as _extract_unit_only,
)

logger = get_logger("AgriConnect.MarketCoach.Validator")

_AUTO_RESOLVABLE_FIELDS = frozenset({"farm_id", "phone"})

_FIELD_PRIORITY: Dict[str, int] = {
    "product": 0,
    "quantity": 1,
    "price": 2,
    "unit": 3,
    "zone": 4,
    "farm_id": 5,
    "stock_id": 5,
    "auction_id": 5,
    "bid_id": 5,
    "cycle_id": 5,
    "movement_type": 6,
    "intervention_type": 6,
}

_PRODUCTION_TYPE_SYNONYMS = {
    "livestock": "LIVESTOCK",
    "elevage": "LIVESTOCK",
    "élevage": "LIVESTOCK",
    "betail": "LIVESTOCK",
    "bétail": "LIVESTOCK",
    "volaille": "LIVESTOCK",
    "poulet": "LIVESTOCK",
    "poussins": "LIVESTOCK",
    "culture": "CROP",
    "cultures": "CROP",
    "agriculture": "CROP",
    "champ": "CROP",
    "plantation": "CROP",
}




def _canonical_field_name(field: str) -> str:
    normalized = normalize_slot_keys({field: True})
    return next(iter(normalized.keys()), field)


def _canonicalize_required_fields(fields: List[str]) -> List[str]:
    canonical: List[str] = []
    seen: set[str] = set()
    for raw_field in fields:
        canonical_field = _canonical_field_name(raw_field)
        if canonical_field not in seen:
            canonical.append(canonical_field)
            seen.add(canonical_field)
    return canonical


def _finalize_validator_response(state: Dict[str, Any], response: Dict[str, Any]) -> Dict[str, Any]:
    cleanup = build_compaction_patch(state)
    if cleanup:
        response.update(cleanup)
    return response


def _missing_fields_for_goal(payload: Dict[str, Any], required_fields: List[str]) -> List[str]:
    missing = [
        field for field in required_fields
        if field not in _AUTO_RESOLVABLE_FIELDS
        and payload.get(field) in (None, "", [], {})
    ]
    missing.sort(key=lambda f: _FIELD_PRIORITY.get(f, 99))
    return missing


def _merge_extracted_entities(state: Dict[str, Any], payload: Dict[str, Any]) -> None:
    entities = normalize_slot_keys(state.get("extracted_entities") or {})
    for key, value in entities.items():
        if value not in (None, "", [], {}):
            payload[key] = value


_UNIT_SYNONYM_MAP = _CANONICAL_UNIT_SYNONYMS

_normalize_unit_token = _normalize_unit_token_canonical


def _extract_quantity_unit_from_text(text: str, state: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    parsed = _parse_qty_unit(text or "")
    if not parsed:
        return None
    result = parsed.as_dict()
    if result and state is not None:
        return _finalize_validator_response(state, result)
    return result if result else None


def _extract_production_type_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    lowered = text.lower()
    for token, value in _PRODUCTION_TYPE_SYNONYMS.items():
        if token in lowered:
            return value
    stripped = lowered.strip().upper()
    if stripped in {"CROP", "LIVESTOCK"}:
        return stripped
    return None


def _extract_unit_only_from_text(text: str) -> Optional[str]:
    return _extract_unit_only(text)


def _extract_surface_from_text(text: str) -> Optional[float]:
    if not text:
        return None
    match = re.search(r"(\d+[\d\s,.]*)\s*(ha|hectare|hectares|m2|m²)", text, re.IGNORECASE)
    if not match:
        return None
    raw_value = match.group(1).replace(" ", "").replace(",", ".")
    unit = match.group(2).lower()
    try:
        numeric = float(raw_value)
    except (TypeError, ValueError):
        return None
    if unit in {"m2", "m²"}:
        return numeric / 10000.0
    return numeric


def _extract_future_datetime_from_text(text: str) -> Optional[str]:
    if not text:
        return None

    absolute = re.search(r"(\d{4}-\d{2}-\d{2}(?:[tT ]\d{2}:\d{2}(?::\d{2})?)?)", text)
    if absolute:
        candidate = absolute.group(1).replace(" ", "T")
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            pass

    now = datetime.now(timezone.utc)
    month_match = re.search(r"dans\s+(\d+)\s*mois", text, re.IGNORECASE)
    if month_match:
        months = int(month_match.group(1))
        return (now + timedelta(days=30 * months)).isoformat()

    week_match = re.search(r"dans\s+(\d+)\s*semaines?", text, re.IGNORECASE)
    if week_match:
        weeks = int(week_match.group(1))
        return (now + timedelta(weeks=weeks)).isoformat()

    day_match = re.search(r"dans\s+(\d+)\s*jours?", text, re.IGNORECASE)
    if day_match:
        days = int(day_match.group(1))
        return (now + timedelta(days=days)).isoformat()

    return None


def _needs_structured_extraction(payload: Dict[str, Any], fields: Tuple[str, ...]) -> bool:
    return any(payload.get(field) in (None, "", 0, [], {}) for field in fields)


def _contains_quantitative_hint(text: str) -> bool:
    return bool(re.search(r"\d", text or ""))


async def _llm_extract_quantity_unit_from_text(
    mc_runtime: MarketRuntime,
    user_text: str,
) -> Optional[Dict[str, Any]]:
    if not user_text:
        return None
    llm = getattr(mc_runtime, "llm", None)
    if llm is None:
        return None

    prompt = (
        "Tu extrais des entités de commande agricole. "
        "Réponds strictement un JSON avec les clés: "
        "quantity (number|null), unit (KG|TONNE|SAC|PANIER|null), "
        "product (string|null). "
        "Ne retourne rien d'autre."
    )

    try:
        completion = await asyncio.to_thread(
            lambda: llm.chat.completions.create(
                model=getattr(mc_runtime, "model_answer", "llama-3.3-70b-versatile"),
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_text},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=90,
            )
        )
        parsed = json.loads(completion.choices[0].message.content or "{}")
        out: Dict[str, Any] = {}
        quantity = parsed.get("quantity")
        unit = parsed.get("unit")
        product = parsed.get("product")

        if quantity not in (None, "", [], {}):
            try:
                out["quantity"] = float(quantity)
            except (TypeError, ValueError):
                pass

        if unit not in (None, "", [], {}):
            unit_norm = str(unit).upper().strip()
            if unit_norm in {"KG", "TONNE", "SAC", "PANIER"}:
                out["unit"] = unit_norm

        if product not in (None, "", [], {}):
            out["product"] = str(product).strip()

        return out or None
    except Exception as exc:
        logger.debug("validator llm extraction failed: %s", exc)
        return None


async def validator(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Valide la complétude du payload transactionnel selon l'intention courante."""
    goal = state.get("current_goal")
    event = str(state.get("interpreted_event") or "").upper()
    payload: Dict[str, Any] = normalize_slot_keys(dict(state.get("transaction_payload") or {}))
    goal_upper = str(goal or "").upper()
    goal_config = INTENT_CONFIG.get(goal_upper) or {}
    required_for_goal = _canonicalize_required_fields(list(goal_config.get("required") or []))
    if not payload and state.get("extracted_entities"):
        payload = normalize_slot_keys(dict(state.get("extracted_entities")))
    normalized_text = str(state.get("normalized_text") or state.get("user_query") or "")
    if not normalized_text:
        messages = state.get("messages") or []
        if isinstance(messages, list):
            for msg in reversed(messages):
                if not isinstance(msg, dict):
                    continue
                role = str(msg.get("role") or "").lower().strip()
                if role != "user":
                    continue
                content = msg.get("content")
                candidate = ""
                if isinstance(content, str) and content.strip():
                    candidate = content.strip()
                elif isinstance(content, list):
                    parts: List[str] = []
                    for chunk in content:
                        if isinstance(chunk, str) and chunk.strip():
                            parts.append(chunk.strip())
                        elif isinstance(chunk, dict):
                            text_part = chunk.get("text")
                            if isinstance(text_part, str) and text_part.strip():
                                parts.append(text_part.strip())
                    candidate = " ".join(parts).strip()
                if candidate:
                    normalized_text = candidate
                    break
    working_memory = state.get("working_memory") or {}

    previous_goal = working_memory.get("active_goal") or working_memory.get("locked_intent")
    prev_goal_upper = str(previous_goal or "").upper()
    goal_changed = bool(previous_goal and goal and prev_goal_upper != goal_upper)

    if goal_changed:
        for structural_key in ("quantity", "unit", "price"):
            payload.pop(structural_key, None)

    # -----------------------------------------------------------------
    # PROTECTION SYSTEMIQUE : Rupture d'amnésie & Vues en lecture seule
    # -----------------------------------------------------------------
    if goal in {"BUYER_LIST_ORDERS", "BUYER_VIEW_CART"}:
        # Nettoyage strict pour éviter les mutations collatérales en cas de rebond d'état
        for structural_key in ["quantity", "unit", "price"]:
            payload.pop(structural_key, None)

    if event in {"NEW_TASK", "SELECTION", "ANSWER"}:
        _merge_extracted_entities(state, payload)

    if not payload.get("product") and re.search(r"tomate", normalized_text, re.IGNORECASE):
        payload["product"] = "tomates"

    # -----------------------------------------------------------------
    # PIPELINE D'EXTRACTION DE DONNÉES (Résout l'erreur NLU N°1)
    # -----------------------------------------------------------------
    if goal_upper == "BUYER_ADD_TO_CART":
        # Étape A : Extraction déterministe via Regex
        extracted = _extract_quantity_unit_from_text(normalized_text, state)
        if extracted:
            payload.update({k: v for k, v in extracted.items() if v not in (None, "", 0, [], {})})

        # Étape B : Détection de contamination sémantique du champ "product" (Ex: "50kg de patates")
        product_str = str(payload.get("product") or "")
        product_is_dirty = _contains_quantitative_hint(product_str) or any(
            u in product_str.lower() for u in ["kg", "tonne", "sac", "panier"]
        )

        # Étape C : Appel LLM obligatoire si manque d'entités OU si le nom du produit est pollué
        if _needs_structured_extraction(payload, ("quantity", "unit")) or product_is_dirty:
            llm_extracted = await _llm_extract_quantity_unit_from_text(mc_runtime, normalized_text)
            if llm_extracted:
                payload.update({k: v for k, v in llm_extracted.items() if v not in (None, "", 0, [], {})})

    if payload.get("unit") in (None, "", [], {}) and normalized_text:
        unit_from_text = _extract_unit_only_from_text(normalized_text)
        if unit_from_text:
            payload["unit"] = unit_from_text

    if payload.get("surface") in (None, "", [], {}) and normalized_text:
        surface_value = _extract_surface_from_text(normalized_text)
        if surface_value is not None:
            payload["surface"] = surface_value

    if payload.get("estimated_available_at") in (None, "", [], {}) and normalized_text:
        estimated_available_at = _extract_future_datetime_from_text(normalized_text)
        if estimated_available_at:
            payload["estimated_available_at"] = estimated_available_at

    production_type_value = payload.get("production_type")
    if isinstance(production_type_value, str) and production_type_value.strip():
        candidate = production_type_value.strip().upper()
        if candidate in {"CROP", "LIVESTOCK"}:
            payload["production_type"] = candidate
        else:
            payload["production_type"] = None
    if payload.get("production_type") in (None, "", [], {}) and normalized_text:
        extracted_type = _extract_production_type_from_text(normalized_text)
        if extracted_type:
            payload["production_type"] = extracted_type

    if not goal:
        return _finalize_validator_response(
            state,
            {
                "status": "WAITING_INPUT",
                "response_strategy": "CLARIFICATION",
                "missing_fields": [],
                "validation_errors": [],
                "transaction_payload": payload,
                "ag_ui_component": None,
            },
        )

    # -----------------------------------------------------------------
    # WRITE TUNNELS — FARM & STOCKS
    # -----------------------------------------------------------------
    if goal == "FARM_CREATE":
        if payload.get("location") and not payload.get("zone"):
            payload["zone"] = payload.get("location")
        if payload.get("size") and not payload.get("surface"):
            payload["surface"] = payload.get("size")

    # Pass-through to context resolver for goals that resolve IDs dynamically
    _RESOLVER_PASSTHROUGH = {
        "MARKET_GET_REQUESTS": ("auction_id", ["product"]),
        "SALES_PLACE_BID": ("auction_id", ["product"]),
        "MARKET_GET_MY_PROPOSALS": ("bid_id", []),
        "SALES_ACCEPT_CONTRACT": ("bid_id", []),
        "PROCUREMENT_ACCEPT_OFFER": ("bid_id", []),
    }
    passthrough = _RESOLVER_PASSTHROUGH.get(goal_upper)
    if passthrough:
        id_field, hint_fields = passthrough
        needs_resolver = not payload.get(id_field)
        if goal_upper == "SALES_PLACE_BID":
            needs_resolver = needs_resolver and bool(payload.get("product"))
        if needs_resolver:
            return _finalize_validator_response(
                state,
                {
                    "status": "PLANNING",
                    "missing_fields": [],
                    "last_missing_field": None,
                    "expected_input": "NONE",
                    "completed_fields": [k for k in hint_fields if payload.get(k)],
                    "validation_errors": [],
                    "transaction_payload": payload,
                    "ag_ui_component": None,
                },
            )

    # -----------------------------------------------------------------
    # ÉVALUATION FINALE DES ERREURS ET DES CHAMPS MANQUANTS
    # -----------------------------------------------------------------
    missing = _missing_fields_for_goal(payload, required_for_goal)
    completed = [f for f in required_for_goal if f not in missing]

    if goal_upper == "BUYER_ADD_TO_CART" and "unit" in missing:
        retry_count = int(state.get("retry_count") or 0)
        security_patch: Dict[str, Any] = {}
        if retry_count >= 2:
            security_patch = {
                "security_status": "WARNING",
                "security_reason": "unit_missing",
                "requires_human": True,
            }
        completed_without_unit = [field for field in completed if field != "unit"]
        return _finalize_validator_response(
            state,
            {
                "status": "WAITING_INPUT",
                "goal_status": "WAITING_INPUT",
                "missing_fields": ["unit"],
                "completed_fields": completed_without_unit,
                "validation_errors": ["missing_unit"],
                "last_missing_field": "unit",
                "expected_input": "UNIT",
                "response_strategy": "ASK_MISSING_FIELD",
                "final_response": "Quelle est l'unité souhaitée (KG, SAC, TONNE) pour ce produit ?",
                "transaction_payload": payload,
                "ag_ui_component": None,
                **security_patch,
            },
        )

    errors: List[str] = []
    warnings: List[str] = []

    if "product" in required_for_goal and not slot_has_value(payload.get("product")):
        return _finalize_validator_response(
            state,
            {
                "status": "WAITING_INPUT",
                "goal_status": "WAITING_INPUT",
                "missing_fields": ["product"],
                "completed_fields": [f for f in completed if f != "product"],
                "validation_errors": ["missing_product"],
                "last_missing_field": "product",
                "expected_input": "PRODUCT",
                "response_strategy": "ASK_MISSING_FIELD",
                "final_response": (
                    "Je n'ai pas bien compris quel produit agricole vous souhaitez enregistrer "
                    "(Maïs, Riz, Engrais Urée...). Peux-tu préciser ?"
                ),
                "transaction_payload": payload,
                "ag_ui_component": None,
            },
        )
    
    price = payload.get("price")
    if price is not None:
        try:
            pf = float(price)
            if pf <= 0:
                errors.append("Le prix doit être supérieur à 0.")
            elif pf > 10_000_000:
                warnings.append(f"Prix très élevé ({pf:,.0f} FCFA). Vérifiez.")
        except (TypeError, ValueError):
            errors.append("Le prix indiqué n'est pas un nombre valide.")

    qty = payload.get("quantity")
    if qty is not None:
        try:
            qf = float(qty)
            if qf <= 0:
                errors.append("La quantité doit être supérieure à 0.")
            elif qf > 100_000:
                warnings.append(f"Quantité très importante ({qf:,.0f}). Vérifiez l'unité.")
        except (TypeError, ValueError):
            errors.append("La quantité indiquée n'est pas un nombre valide.")
            
    payload = _normalize_quantity_to_kg(payload)

    unit_raw = str(payload.get("unit") or "KG").upper()
    unit_display = canonical_unit_label(
        payload.get("unit_display")
        or payload.get("original_unit")
        or unit_raw
    )
    if qty is not None and unit_display == "TONNE":
        try:
            if float(qty) > 500:
                warnings.append("500+ tonnes semble excessif. Vérifiez l'unité.")
        except (TypeError, ValueError):
            pass

    progress = _compute_progress(goal, payload)

    if missing or errors:
        first_missing = missing[0] if missing else None
        expected_input_map = {
            "product": "PRODUCT",
            "price": "PRICE",
            "quantity": "QUANTITY",
            "unit": "UNIT",
            "zone": "LOCATION",
            "surface": "QUANTITY",
            "production_type": "PRODUCT",
            "estimated_available_at": "DATE",
        }
        hint = None
        if progress and first_missing:
            total = progress.get("total", 0)
            filled = progress.get("filled", 0)
            label = _label_for_field(goal, first_missing)
            hint = f"Étape {filled + 1}/{total} : {label}"

        return _finalize_validator_response(
            state,
            {
                "status": "WAITING_INPUT",
                "goal_status": "WAITING_INPUT",
                "missing_fields": missing,
                "completed_fields": completed,
                "validation_errors": errors + warnings,
                "last_missing_field": first_missing,
                "expected_input": expected_input_map.get(first_missing or "", "NONE"),
                "response_strategy": "ASK_MISSING_FIELD",
                "transaction_payload": payload,
                "conversation_progress": progress,
                "proactive_hint": hint,
                "waiting_for_confirmation": False,
                "is_certified": False,
                "confirmation_summary": None,
                "execution_authorized": False,
                "ag_ui_component": None,
            },
        )

    validation_warns = warnings if warnings else []

    is_cart_goal = goal_upper in {"BUYER_ADD_TO_CART", "BUYER_VIEW_CART"}
    
    # Construction du dictionnaire de retour
    result = {
        "status": "PROCESSING" if is_cart_goal else "PLANNING",
        "missing_fields": [],
        "last_missing_field": None,
        "expected_input": "NONE",
        "completed_fields": completed,
        "validation_errors": validation_warns,
        "transaction_payload": payload,
        "conversation_progress": progress,
        "proactive_hint": None,
    }

    # AJOUTE CECI : Forcer le nettoyage des clés de réponse pour le prochain nœud
    # Si tu utilises LangGraph, ces clés seront écrasées dans le state global
    result["final_response"] = None
    result["ag_ui_component"] = None
    
    return result