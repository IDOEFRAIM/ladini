from __future__ import annotations

from typing import Any, Dict


_VOLATILE_WORKING_KEYS = (
    "available_mapping_kind",
    "auction_menu",
    "bids_menu",
    "stocks_menu",
    "generic_menu",
    "disambiguation_pending",
    "disambiguation_trigger_id",
)

_MERGE_DICT_RESET = {"__reset__": True}

# merge_dict fields that are recomputed every turn and must NOT accumulate
# across turns. Without this reset, the workspace grows ~5-10 KB per turn
# and hits the 480 KB WorkspaceCheckpointer limit after ~6 turns, causing
# state truncation that loses vendor_selection_context and breaks flows.
_EPHEMERAL_MERGE_DICT_FIELDS = (
    "extracted_entities",
    "raw_analysis",
    "cognitive_decision",
    "execution_result",
    "selected_tool_args",
    "interruption_payload",
    "volatile_entities",
)

# replace_value ephemeral fields safe to clear after response is sent
_EPHEMERAL_REPLACE_FIELDS = {
    "interpreted_event": None,
    "detected_intent": None,
    "interpreter_confidence": None,
    "validation_status": None,
    "pending_goal": None,
    "selected_tool": None,
    "retry_count": 0,
    "confirmation_summary": None,
    "execution_authorized": False,
    "waiting_for_confirmation": False,
    "is_certified": False,
    "is_locked": False,
    "should_replan": False,
    "should_interrupt": False,
    "interruption_detected": False,
    "interruption_type": None,
    "security_reason": None,
    "requires_human": False,
    "pending_menu": None,
    "reply_audio_url": None,
    "proactive_hint": None,
}


async def post_response_cleanup(state: Dict[str, Any], mc_runtime: Any) -> Dict[str, Any]:
    pending = state.get("pending_cleanup")
    status = str(state.get("status") or "").upper().strip()
    expected_input = str(state.get("expected_input") or "NONE").upper().strip()

    patch: Dict[str, Any] = {}

    keep_selection_channel = status == "WAITING_INPUT" and expected_input == "SELECTION"

    # When the turn parks the transaction awaiting explicit confirmation, the
    # confirmation channel (waiting_for_confirmation / confirmation_summary) MUST
    # survive to the next turn — otherwise confirmation_gate re-asks forever and
    # the user's "Oui" never triggers execution.
    keep_confirmation_channel = status == "WAITING_CONFIRMATION"

    working = dict(state.get("working_memory") or {})
    if working:
        preserved = set(_VOLATILE_WORKING_KEYS) if keep_selection_channel else set()
        for key in _VOLATILE_WORKING_KEYS:
            if key not in preserved:
                working[key] = None
        patch["working_memory"] = working

    if not keep_selection_channel:
        patch["available_mapping"] = {}
        patch["expected_candidates"] = []

    if isinstance(pending, dict) and pending:
        patch.update(dict(pending))

    patch["pending_cleanup"] = None

    # Reset merge_dict ephemeral fields to prevent unbounded state growth.
    for field in _EPHEMERAL_MERGE_DICT_FIELDS:
        current = state.get(field)
        if isinstance(current, dict) and current and not current.get("__reset__"):
            patch[field] = _MERGE_DICT_RESET

    # Reset replace_value ephemeral fields
    _confirmation_preserved = {"waiting_for_confirmation", "confirmation_summary"}
    for field, default in _EPHEMERAL_REPLACE_FIELDS.items():
        if keep_confirmation_channel and field in _confirmation_preserved:
            continue
        current = state.get(field)
        if current is not None and current != default:
            patch[field] = default

    return patch
