from typing import Any, Dict, List, Optional
import unicodedata
from agriconnect.core.logging import get_logger
from agriconnect.graphs.agents.market_coach.services.menu_snapshot import (
    menu_snapshot_store,
)
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    _normalize_quantity_to_kg,
    canonical_unit_label,
    merge_payload,
    normalize_slot_keys,
    slot_has_value,
)

logger = get_logger("AgriConnect.MarketCoach.MemoryUpdate")

_ADD_TO_CART_DRAFT_FIELDS = {
    "product",
    "product_id",
    "quantity",
    "unit",
    "price",
    "zone",
    "selection_index",
    "selected_value",
    "resolved_id",
}


_PRIMARY_CANONICAL_UNITS = {"KG", "TONNE", "SAC", "UNITE"}

_ALIAS_MIRRORS = {
    "product": ("product_name",),
    "quantity": ("quantity_mentioned",),
    "unit": ("unit_mentioned",),
    "price": ("price_mentioned",),
    "zone": ("zone_name",),
    "original_quantity": ("original_quantity_mentioned",),
    "original_unit": ("original_unit_mentioned",),
}

_EMPTY_SLOT_VALUES = (None, "", [], {})
_CORRECTION_HISTORY_LIMIT = 5
_MAX_MESSAGE_HISTORY = 5
_MAX_TOOL_HISTORY = 10
_ORDER_MAPPING_KINDS = frozenset({"order", "order_list", "buyer_orders"})
_EPHEMERAL_WORKING_KEYS = frozenset({"payload_richness", "last_confidence", "step_index"})
_PRODUCT_CASCADE_FIELDS = (
    "quantity",
    "unit",
    "price",
    "currency",
    "is_negotiable",
    "variety",
    "quality_grade",
    "quantity_display",
    "original_quantity",
    "unit_conversion",
)
_UNIT_CASCADE_FIELDS = ("price",)
_CANONICAL_SLOT_ORDER = ("product", "quantity", "unit", "price", "zone")


def _mirror_aliases(container: Dict[str, Any]) -> None:
    for canonical, aliases in _ALIAS_MIRRORS.items():
        value = container.get(canonical)
        for alias in aliases:
            if slot_has_value(value):
                container[alias] = value
            else:
                container.pop(alias, None)


def _trim_sequence_window(items: Any, limit: int) -> Optional[List[Any]]:
    if isinstance(items, list) and len(items) > limit:
        return items[-limit:]
    return None


def _compact_order_tracking_context(raw_ctx: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw_ctx, dict):
        return None
    if "mapping_cache" in raw_ctx:
        compacted = dict(raw_ctx)
        compacted.pop("mapping_cache", None)
        return compacted
    return None


def _build_compaction_patch(state: Dict[str, Any]) -> Dict[str, Any]:
    patch: Dict[str, Any] = {}
    for key in ("messages", "history", "conversation_history"):
        trimmed = _trim_sequence_window(state.get(key), _MAX_MESSAGE_HISTORY)
        if trimmed is not None:
            patch[key] = trimmed

    trimmed_tool_history = _trim_sequence_window(state.get("tool_execution_history"), _MAX_TOOL_HISTORY)
    if trimmed_tool_history is not None:
        patch["tool_execution_history"] = trimmed_tool_history

    compacted_tracking = _compact_order_tracking_context(state.get("order_tracking_context"))
    if compacted_tracking is not None:
        patch["order_tracking_context"] = compacted_tracking

    return patch


def _values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b


def _format_value(value: Any) -> str:
    if value in _EMPTY_SLOT_VALUES:
        return "∅"
    if isinstance(value, float):
        return ("%g" % value)
    return str(value)


def _resolve_unit_value(value: Any) -> Optional[str]:
    if value in (None, "", [], {}):
        return None
    raw = str(value).strip().upper()
    if not raw:
        return None
    canonical = canonical_unit_label(raw, raw)
    canonical = str(canonical or "").upper().strip()
    if canonical in _PRIMARY_CANONICAL_UNITS:
        return canonical
    return None


async def memory_update(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """Met à jour la mémoire de transaction, résout les sélections AG-UI,
    hérite les entités stables pour continuité inter-tours, et injecte
    les défauts profil (zone) quand l'utilisateur ne les spécifie pas."""
    extracted_raw: Dict[str, Any] = state.get("extracted_entities") or {}
    payload_source: Dict[str, Any] = dict(state.get("transaction_payload") or {})
    stable_source: Dict[str, Any] = dict(state.get("stable_entities") or {})
    working_source: Dict[str, Any] = dict(state.get("working_memory") or {})
    working: Dict[str, Any] = {
        key: value for key, value in working_source.items() if key not in _EPHEMERAL_WORKING_KEYS
    }
    draft_source: Dict[str, Any] = dict(state.get("draft_payload") or {})

    extracted = normalize_slot_keys(extracted_raw)
    payload = normalize_slot_keys(payload_source)
    stable = normalize_slot_keys(stable_source)
    draft_payload = normalize_slot_keys(draft_source)
    onboarding_profile = dict(state.get("onboarding_profile") or {})

    def _rehydrate_onboarding_slot(source_key: str, target_key: Optional[str] = None) -> None:
        key = target_key or source_key
        value = onboarding_profile.get(source_key)
        if slot_has_value(value) and not slot_has_value(payload.get(key)):
            payload[key] = value

    _rehydrate_onboarding_slot("phone")
    _rehydrate_onboarding_slot("name")
    _rehydrate_onboarding_slot("role")
    _rehydrate_onboarding_slot("zone_name")
    _rehydrate_onboarding_slot("zone_id")

    existing_corrections = working_source.get("recent_corrections") or {}
    if isinstance(existing_corrections, dict):
        recent_corrections = dict(existing_corrections)
    else:
        # Legacy format (list of dicts) → flatten
        recent_corrections = {}
        for entry in existing_corrections:
            if isinstance(entry, dict):
                for key, value in entry.items():
                    recent_corrections[key] = value
    payload_reset = False
    product_slot_changed = False

    def _record_correction(field: str, old: Any, new: Any) -> None:
        if not field:
            return
        change_value = f"{_format_value(old)} -> {_format_value(new)}"
        if field in recent_corrections:
            recent_corrections.pop(field, None)
        elif len(recent_corrections) >= _CORRECTION_HISTORY_LIMIT:
            first_key = next(iter(recent_corrections))
            recent_corrections.pop(first_key, None)
        recent_corrections[field] = change_value

    def _reset_payload(reason: str) -> None:
        nonlocal payload_reset, payload
        if payload:
            logger.info("[MemoryUpdate] Payload reset (%s)", reason)
        payload.clear()
        payload_reset = True

    def _cascade_clear(fields) -> None:
        for field in fields:
            payload.pop(field, None)

    def _clean_upper(value: Any) -> Optional[str]:
        if not slot_has_value(value):
            return None
        trimmed = str(value).strip().upper()
        return trimmed or None

    def _apply_slot(field: str, value: Any) -> None:
        nonlocal product_slot_changed
        if not slot_has_value(value):
            return
        current_value = payload.get(field)
        if not slot_has_value(current_value):
            payload[field] = value
            return
        if _values_equal(current_value, value):
            return
        _record_correction(field, current_value, value)
        if field == "product":
            _cascade_clear(_PRODUCT_CASCADE_FIELDS)
            stable.pop("product", None)
            stable.pop("quantity", None)
            stable.pop("price", None)
            stable.pop("unit", None)
            stable.pop("stock_id", None)
            product_slot_changed = True
        elif field == "unit":
            _cascade_clear(_UNIT_CASCADE_FIELDS)
            stable.pop("unit", None)
        payload[field] = value

    def _normalize_menu_text(value: str | None) -> str:
        if not value:
            return ""
        normalized = unicodedata.normalize("NFKD", value)
        normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
        normalized = normalized.lower()
        return "".join(ch for ch in normalized if ch.isalnum())

    interpreted_event = str(state.get("interpreted_event") or "").upper().strip()
    expected_input = str(state.get("expected_input") or "NONE").upper().strip()
    form_step_state = str(state.get("form_step") or "").upper().strip()
    form_completed = form_step_state == "COMPLETE"
    onboarding_active = bool(
        state.get("is_onboarding")
        or interpreted_event == "ONBOARDING_INPUT"
        or str(state.get("detected_intent") or "").upper().strip() == "ONBOARDING"
        or str(state.get("response_strategy") or "").upper().strip() == "ONBOARDING"
    )

    if expected_input == "CANCELLATION_REASON":
        reason_text = state.get("normalized_text") or state.get("user_query")
        if isinstance(reason_text, str):
            reason_text = reason_text.strip()
            if reason_text:
                payload["cancel_reason"] = reason_text

    if expected_input == "UNIT":
        unit_candidates = [
            extracted.get("unit"),
            payload.get("unit"),
            stable.get("unit"),
            state.get("normalized_text"),
            state.get("user_query"),
        ]
        resolved_unit = None
        for candidate in unit_candidates:
            resolved_unit = _resolve_unit_value(candidate)
            if resolved_unit:
                break
        if resolved_unit:
            payload["unit"] = resolved_unit
            extracted["unit"] = resolved_unit
            # Avoid treating the unit answer as a product change.
            extracted.pop("product", None)

    incoming_role = _clean_upper(extracted.get("role"))
    if not incoming_role and not onboarding_active:
        incoming_role = _clean_upper(state.get("user_role"))
    if incoming_role == "UNKNOWN":
        incoming_role = None
    previous_role = _clean_upper(payload.get("role"))
    if previous_role and incoming_role and previous_role != incoming_role and not onboarding_active:
        _record_correction("role", previous_role, incoming_role)
        _reset_payload("role_change")
    elif previous_role and incoming_role and previous_role != incoming_role and onboarding_active:
        _record_correction("role", previous_role, incoming_role)
    if incoming_role:
        payload["role"] = incoming_role
    extracted.pop("role", None)

    detected_intent = (
        state.get("current_goal")
        or state.get("detected_intent")
        or extracted.get("intent")
    )
    incoming_intent = _clean_upper(detected_intent)
    if incoming_intent == "UNKNOWN":
        incoming_intent = None
    previous_intent = _clean_upper(payload.get("intent"))
    if previous_intent and incoming_intent and previous_intent != incoming_intent and not onboarding_active:
        _record_correction("intent", previous_intent, incoming_intent)
        if not form_completed:
            _reset_payload("intent_change")
    if incoming_intent and not onboarding_active:
        payload["intent"] = incoming_intent
    extracted.pop("intent", None)

    # FAILLE 1 — Sanctuarise the active business goal.
    # Never overwrite an active tunnel goal with None/UNKNOWN because the user replied briefly ("1", "ok", "oui").
    incoming_goal = state.get("current_goal")
    previous_goal = working.get("active_goal") or working.get("locked_intent")
    in_tunnel = bool(previous_goal and expected_input not in {"", "NONE"})
    if (
        previous_goal
        and (incoming_goal in (None, "", "UNKNOWN") or interpreted_event in {"UNKNOWN", "ANSWER"})
        and (in_tunnel or str(state.get("status") or "").upper() in {"WAITING_INPUT", "WAITING_CONFIRMATION"})
    ):
        current_goal = previous_goal
    else:
        current_goal = incoming_goal

    goal_upper = str(current_goal or "").upper()

    if goal_upper == "BUYER_ADD_TO_CART" and draft_payload and not draft_payload.get("__reset__"):
        payload = merge_payload(draft_payload, payload)

    for field in _CANONICAL_SLOT_ORDER:
        value = extracted.pop(field, None)
        _apply_slot(field, value)

    for key, value in list(extracted.items()):
        if value in _EMPTY_SLOT_VALUES:
            continue
        payload[key] = value

    # --- ENTITY INHERITANCE : stable_entities → payload pour continuité ---
    # Pendant un tunnel, si le payload manque un champ stable, on l'injecte.
    # Cela évite de redemander le produit si l'utilisateur l'a déjà dit.
    if current_goal:
        skip_product_inheritance = payload_reset or product_slot_changed
        for key in ("product", "unit", "zone"):
            if slot_has_value(payload.get(key)) or not slot_has_value(stable.get(key)):
                continue
            if key == "product" and skip_product_inheritance:
                continue
            payload[key] = stable[key]
            logger.debug("[MemoryUpdate] Inherited stable entity %s=%s", key, stable[key])

    # --- ZONE INJECTION from user profile ---
    # Si aucune zone n'est fournie et que le profil en a une, on l'utilise
    if not slot_has_value(payload.get("zone")):
        profile_zone = state.get("zone") or state.get("zone_name")
        if slot_has_value(profile_zone):
            payload["zone"] = str(profile_zone).strip()

    # --- AG-UI: free-text resolution of ListMenu labels ---
    user_free_text = (
        extracted.get("normalized_text")
        or payload.get("normalized_text")
        or state.get("normalized_text")
        or state.get("user_query")
        or ""
    )
    user_free_text = str(user_free_text or "").strip()
    candidates = state.get("expected_candidates") or []
    if (
        user_free_text
        and candidates
        and "selection_index" not in payload
        and "selected_value" not in payload
        and expected_input == "SELECTION"
    ):
        normalized_user = _normalize_menu_text(user_free_text)
        label_index_map = {
            _normalize_menu_text(label): str(i)
            for i, label in enumerate(candidates, start=1)
        }
        matched_index = label_index_map.get(normalized_user)
        if matched_index:
            payload["selection_index"] = matched_index
            extracted["selection_index"] = matched_index

    # --- AG-UI: Selection index resolution via available_mapping ---
    sel_idx = extracted.get("selection_index") or payload.get("selection_index")
    sel_val = extracted.get("selected_value") or payload.get("selected_value")
    mapping = state.get("available_mapping") or {}
    mapping_kind = working.get("available_mapping_kind")
    session_id = str(state.get("session_id") or state.get("user_phone") or "")
    snapshot_id = (
        working.get("menu_snapshot_id")
        or state.get("menu_snapshot_id")
    )

    if sel_idx is not None or sel_val is not None:
        resolved_id = None
        if mapping:
            if sel_idx is not None:
                resolved_id = mapping.get(str(sel_idx))
            if resolved_id is None and sel_val is not None:
                resolved_id = mapping.get(str(sel_val))
        if resolved_id is None and snapshot_id:
            selection_token = sel_idx if sel_idx is not None else sel_val
            resolved_id = menu_snapshot_store.resolve(session_id, snapshot_id, selection_token)

        if resolved_id:
            resolved_str = str(resolved_id)
            if mapping_kind == "auction":
                payload["auction_id"] = resolved_str
            elif mapping_kind == "bid":
                payload["bid_id"] = resolved_str
            elif mapping_kind == "stock":
                payload["stock_id"] = resolved_str
            elif mapping_kind == "farm":
                payload["farm_id"] = resolved_str
            elif mapping_kind in _ORDER_MAPPING_KINDS:
                payload["order_id"] = resolved_str
            elif mapping_kind == "intent_disambiguation":
                pass
            else:
                payload["resolved_id"] = resolved_str
            payload.pop("selection_index", None)
            payload.pop("selected_value", None)

    # --- Update stable entities uniquement après complétion ---
    status = str(state.get("status") or "").upper()
    goal_status = str(state.get("goal_status") or "").upper()
    strategy = str(state.get("response_strategy") or "").upper()
    transaction_closed = strategy == "SUCCESS" or status in {"COMPLETED", "SUCCESS"} or goal_status == "COMPLETED"

    updated_stable = dict(stable)
    if transaction_closed:
        for k in ("product", "unit", "movement_type", "zone"):
            candidate = payload.get(k) or extracted.get(k)
            if slot_has_value(candidate):
                updated_stable[k] = candidate

    # --- Conversation metrics ---
    working["last_event"] = state.get("interpreted_event")
    working["last_confidence"] = float(state.get("interpreter_confidence") or 0.0)
    entity_count = sum(1 for v in payload.values() if v not in (None, "", [], {}))
    working["payload_richness"] = entity_count
    if current_goal and current_goal != "DISAMBIGUATION_PENDING":
        working["active_goal"] = current_goal
        working["locked_intent"] = current_goal
        working["step_index"] = 0

    existing_draft = draft_payload if draft_payload and not draft_payload.get("__reset__") else {}

    draft_patch: Dict[str, Any] | None = None
    if goal_upper == "BUYER_ADD_TO_CART":
        if not transaction_closed:
            tracked = {
                key: payload.get(key)
                for key in _ADD_TO_CART_DRAFT_FIELDS
                if payload.get(key) not in (None, "", [], {})
            }
            if tracked:
                draft_patch = merge_payload(existing_draft, tracked)
        elif existing_draft:
            draft_patch = {"__reset__": True}
    elif transaction_closed and existing_draft:
        draft_patch = {"__reset__": True}

    # Let downstream nodes acknowledge corrections once; response_handlers will clear it.
    if recent_corrections:
        working["recent_corrections"] = recent_corrections
    else:
        working.pop("recent_corrections", None)

    payload = _normalize_quantity_to_kg(payload)
    _mirror_aliases(payload)
    _mirror_aliases(updated_stable)
    if draft_patch and not draft_patch.get("__reset__"):
        _mirror_aliases(draft_patch)
    elif existing_draft:
        _mirror_aliases(existing_draft)

    result = {
        "transaction_payload": payload,
        "stable_entities": updated_stable,
        "working_memory": working,
        "current_goal": current_goal,
    }

    if draft_patch is not None:
        result["draft_payload"] = draft_patch
    elif existing_draft:
        result["draft_payload"] = existing_draft

    compaction_patch = _build_compaction_patch(state)
    if compaction_patch:
        result.update(compaction_patch)

    return result


