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


async def post_response_cleanup(state: Dict[str, Any], mc_runtime: Any) -> Dict[str, Any]:
    pending = state.get("pending_cleanup")
    status = str(state.get("status") or "").upper().strip()
    expected_input = str(state.get("expected_input") or "NONE").upper().strip()

    patch: Dict[str, Any] = {}

    # Keep channel mapping ONLY while actively waiting for a user selection.
    keep_selection_channel = status == "WAITING_INPUT" and expected_input == "SELECTION"

    # Working memory isolation: clear volatile per-turn UI/channel keys.
    working = dict(state.get("working_memory") or {})
    if working:
        if keep_selection_channel:
            preserved = {
                "available_mapping_kind",
                "auction_menu",
                "bids_menu",
                "stocks_menu",
                "generic_menu",
                "disambiguation_pending",
                "disambiguation_trigger_id",
            }
        else:
            preserved = set()

        for key in _VOLATILE_WORKING_KEYS:
            if key in preserved:
                continue
            working.pop(key, None)
        patch["working_memory"] = working

    # Channel safety: prevent stale index/value carry-over in payload.
    payload = dict(state.get("transaction_payload") or {})
    changed_payload = False
    for transient_key in ("selection_index", "selected_value", "resolved_id"):
        if transient_key in payload:
            payload.pop(transient_key, None)
            changed_payload = True
    if changed_payload:
        patch["transaction_payload"] = payload

    # Drop stale mapping/candidates once no selection is expected.
    if not keep_selection_channel:
        patch["available_mapping"] = {}
        patch["expected_candidates"] = []

    # Merge explicit cleanup requests from upstream nodes.
    if isinstance(pending, dict) and pending:
        patch.update(dict(pending))

    patch["pending_cleanup"] = None
    return patch
