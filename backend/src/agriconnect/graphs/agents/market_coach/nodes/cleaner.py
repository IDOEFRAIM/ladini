from __future__ import annotations

from typing import Any, Dict, Optional, Set

from agriconnect.graphs.agents.market_coach.core.state import (
    MarketAgentState,
    resolve_current_goal,
)
from agriconnect.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from agriconnect.graphs.agents.market_coach.utils import CANONICAL_TRANSACTION_FIELDS

_ACTIVE_GOAL_STATES = frozenset(
    {"ACTIVE", "WAITING_INPUT", "WAITING_CONFIRMATION", "EXECUTING"}
)
_EPHEMERAL_WORKING_KEYS = ("payload_richness", "last_confidence", "step_index")
_ONBOARDING_TRANSACTION_FIELDS = frozenset({"name", "zone_name", "zone_id", "phone"})

# (2026-09-08, P1-4 audit architectural) : `chat_history`/`_trim_history`
# supprimés — recherche exhaustive dans `src/agriconnect` : AUCUN nœud du
# graphe n'écrit jamais de contenu réel sous cette clé (seul
# `workspace/checkpointer.py::_SHRINKABLE_CHANNELS` la mentionne, comme
# nom générique "sûr à réduire sous pression mémoire" — pas une preuve
# d'écriture active). Ce bloc était un no-op permanent : `_trim_history`
# ne retournait jamais qu'un `None` silencieusement absorbé par le garde
# `if trimmed_history is not None`. Gardait la FAUSSE promesse d'un
# historique borné à 4 entrées alors que rien ne le peuple.


def _goal_slot_fields(state: MarketAgentState) -> Set[str]:
    """Slots owned by the currently active goal (required + label_map keys).

    The canonical whitelist alone drops goal-specific slots such as
    ``movement_type``, ``cycle_id``, ``bid_id`` or ``auction_id`` at the end of
    a turn — which silently breaks any WRITE goal that parks for confirmation
    and then executes on the next turn. Preserving the active goal's own slots
    keeps the transaction payload intact across the confirmation boundary while
    still pruning unrelated extraction noise.
    """
    goal = str(resolve_current_goal(state) or "").upper()
    cfg = INTENT_CONFIG.get(goal)
    if not cfg:
        return set()
    fields: Set[str] = set(cfg.get("required") or [])
    fields.update((cfg.get("label_map") or {}).keys())
    return fields


def _sanitize_transaction_payload(
    payload: Any, state: MarketAgentState
) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    allowed: Set[str] = set(CANONICAL_TRANSACTION_FIELDS)
    allowed.update(_goal_slot_fields(state))
    if (
        state.get("is_onboarding")
        or str(state.get("response_strategy") or "").upper() == "ONBOARDING"
    ):
        allowed.update(_ONBOARDING_TRANSACTION_FIELDS)
    sanitized = {
        key: value
        for key, value in payload.items()
        if key in allowed and value not in (None, "", [], {})
    }
    return sanitized if sanitized != payload else None


async def state_cleaner_node(
    state: MarketAgentState,
    *_: Any,
    **__: Any,
) -> Dict[str, Any]:
    """Garbage collector executed at the very end of each turn."""

    patch: Dict[str, Any] = {}

    working = dict(state.get("working_memory") or {})
    working_patch = False

    if working.get("recent_corrections") not in (None, {}):
        working["recent_corrections"] = None
        working_patch = True

    if working.get("fetched_data_cache") is not None:
        working["fetched_data_cache"] = None
        working_patch = True

    for key in _EPHEMERAL_WORKING_KEYS:
        if working.get(key) is not None:
            working[key] = None
            working_patch = True

    if working_patch:
        patch["working_memory"] = working

    # ── Terminal-goal reset ──────────────────────────────────────────
    # When a goal finishes this turn (status COMPLETED/FAILED/ERROR), the
    # whole transaction payload and goal-tracking memory must be flushed so
    # the NEXT message starts from a clean slate. Without this, slots like
    # quantity=225 or a locked PROCUREMENT_CREATE_REQUEST intent leak into
    # the following unrelated request (e.g. "je veux commander des tomates"
    # wrongly resumes an auction with a stale quantity). See
    # [[market-coach-turn-boundary-state]].
    # NB: originally gated on `status == "COMPLETED"` only — a goal that
    # ends in FAILED/ERROR is just as "over" and left the exact same stale
    # fields behind, so the gate now covers all three terminal statuses.
    status_flag = str(state.get("status") or "").upper().strip()
    goal_completed = status_flag in {"COMPLETED", "FAILED", "ERROR"}

    if goal_completed:
        if status_flag in {"ERROR", "FAILED"} and state.get("current_goal"):
            # Consommé une seule fois par render_clarification (nodes/rendering/
            # feedback.py) si le tour suivant retombe sur le fallback générique
            # — évite un message "je ne sais pas ce que vous faites" juste
            # après un goal qui vient d'échouer sous les yeux de l'utilisateur.
            patch["last_terminated_goal"] = state.get("current_goal")
        patch["transaction_payload"] = {"__reset__": True}
        # merge_dict fields: must use the reset sentinel, a plain {} is a
        # no-op under merge_dict semantics (agents/reducers.py) and would
        # silently leave the old goal's data in place.
        patch["stable_entities"] = {"__reset__": True}
        patch["selected_tool_args"] = {"__reset__": True}
        # NOTE: execution_result is NOT reset here — final_response (which
        # runs AFTER state_cleaner) needs it to render the tool output.
        # It is properly reset by post_response_cleanup instead.
        patch["form_data"] = {"__reset__": True}
        # (2026-09-08, P1-4 audit architectural) : garde anti-double-tentative
        # de création de ferme — n'a de sens que pour LE goal farm-critique en
        # cours. Le laisser vivre après COMPLETED/FAILED/ERROR bloquerait à
        # tort une future tentative légitime sur un AUTRE goal.
        patch["farm_creation_attempted"] = False
        # NOTE: current_goal is NOT reset here — final_response needs it
        # for _resolve_goal_for_ui (e.g. distinguish READ vs WRITE goals
        # for the fallback message). Reset by post_response_cleanup.
        patch["goal_status"] = None
        patch["retry_count"] = 0
        patch["active_form"] = None
        patch["form_step"] = None
        # (2026-08-31) Incident réel : ces trois champs n'étaient JAMAIS
        # remis à zéro ici — `replace_value`/`replace_list`, donc ils
        # survivent indéfiniment tels quels tant que rien ne les réécrit
        # explicitement. Un `last_missing_field`/`conversation_progress`
        # laissé par UN goal (ex: une publication de produit abandonnée)
        # pouvait ressurgir sur un tour bien PLUS TARD, pour un goal sans
        # aucun rapport (ex: juste après l'enregistrement d'une récolte) —
        # `render_ask_missing_field` (nodes/rendering/ask.py) générait alors
        # une question incohérente à partir de ce champ périmé. Combiné au
        # fix du sentinelle `"NONE"` dans `interpreter/strategy.py`, ce
        # nettoyage garantit qu'un goal qui se termine n'a plus AUCUN champ
        # "en attente" à faire fuiter vers le tour suivant.
        patch["last_missing_field"] = None
        patch["missing_fields"] = []
        patch["conversation_progress"] = None
        wm_terminal = dict(patch.get("working_memory") or working)
        for key in (
            "active_goal",
            "pending_goal",
            "buyer_request_waiting_choice",
            "buyer_request_catalog_checked",
            "buyer_request_last_product",
        ):
            wm_terminal[key] = None
        patch["working_memory"] = wm_terminal
    else:
        payload_patch = _sanitize_transaction_payload(
            state.get("transaction_payload"), state
        )
        if payload_patch is not None:
            patch["transaction_payload"] = payload_patch

    draft_payload = state.get("draft_payload")
    if (
        isinstance(draft_payload, dict)
        and draft_payload
        and not draft_payload.get("__reset__")
    ):
        goal_status = str(state.get("goal_status") or "").upper()
        current_goal = str(state.get("current_goal") or "").upper()
        _DRAFT_SAFE_GOALS = frozenset(
            {
                "BUYER_ADD_TO_CART",
                "BUYER_VIEW_CART",
                "BUYER_PREORDER_INIT",
                "BUYER_PREORDER_CONFIRM",
            }
        )
        transaction_active = (
            goal_status in _ACTIVE_GOAL_STATES and current_goal in _DRAFT_SAFE_GOALS
        )
        status_flag = str(state.get("status") or "").upper()
        if not transaction_active or status_flag in {"FAILED", "COMPLETED"}:
            patch["draft_payload"] = {"__reset__": True}

    return patch


__all__ = ["state_cleaner_node"]
