"""Buyer recurring supply flow — CREATE_RECURRING_NEED / UPDATE_RECURRING_NEED / GET_MY_NEEDS
(Phase 2). Point d'entrée du tunnel `recurring_need` (`core/goals.py`), appelé depuis
`buyer_context_resolver` (`flows/buyer/flow.py`).

Même discipline transactionnelle que PREORDER (`preorder_confirmation.py`) — pas de nœud
`mcp_tool_executor` séparé pour la création : `RecurringSupplyGateway.create_recurring_need` est
appelé DIRECTEMENT dans ce nœud, dans la MÊME fonction que la décision de confirmation.
`EXECUTING` est persisté (CAS, `RecurringNeedDraft.version`) AVANT l'appel MCP, jamais après.

`UPDATE_RECURRING_NEED`/`GET_MY_NEEDS` n'ont pas de draft (l'objet modifié/lu est déjà persisté,
identifié conversationnellement — même choix que `PROCUREMENT_UPDATE_REQUEST`/`BUYER_LIST_AUCTIONS`,
voir le rapport de Phase 2) : l'action est appliquée directement via le même gateway.

Mandat §18 : aucune réponse utilisateur ne mentionne occurrence/CAS/recurring_need/version.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    clear_pending_interaction,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
    ConfirmRecurringNeedDraft,
    RecurringNeedDraft,
    RecurringNeedExecutionResult,
    apply_domain_action,
    build_response_plan,
    finalize_after_execution,
    resolve_domain_action,
)
from ladini.graphs.agents.market_coach.services.mcp.gateway import (
    MCPCallError,
    RecurringSupplyGateway,
)
from ladini.graphs.agents.market_coach.utils import MarketRuntime, slot_has_value

logger = logging.getLogger("Ladini.Market.RecurringNeed")

_DRAFT_FIELDS = (
    "product",
    "quantity",
    "unit",
    "recurrence_type",
    "weekly_days",
    "excluded_weekdays",
    "starts_at",
    "ends_at",
    "max_price_per_unit",
)


async def recurring_need_flow(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    goal = str(state.get("current_goal") or "").upper()
    if goal == "CREATE_RECURRING_NEED":
        return await _create_flow(state, mc_runtime)
    if goal == "UPDATE_RECURRING_NEED":
        return await _update_flow(state, mc_runtime)
    if goal == "GET_MY_NEEDS":
        return await _get_my_needs_flow(state, mc_runtime)
    logger.warning("recurring_need_flow: goal inattendu %s", goal)
    return {"status": "PLANNING", "final_response": "", "ag_ui_component": None}


# =====================================================================
# CREATE_RECURRING_NEED — draft conversationnel (CAS)
# =====================================================================


async def _create_flow(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    phone = state.get("user_phone")
    payload: Dict[str, Any] = state.get("transaction_payload") or {}
    interpreted_event = str(state.get("interpreted_event") or "").upper().strip()
    pending = state.get("pending_interaction") or {}
    pending_target = pending.get("target") if isinstance(pending, dict) else None

    draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
    extracted = {k: v for k, v in payload.items() if k in _DRAFT_FIELDS and slot_has_value(v)}
    action = resolve_domain_action(
        interpreted_event=interpreted_event or ("UPDATE" if extracted else "ANSWER"),
        extracted_entities=extracted,
        pending_target=pending_target,
    )
    outcome = apply_domain_action(draft, action)

    if isinstance(action, ConfirmRecurringNeedDraft) and outcome.draft is not None and outcome.draft.status.value == "EXECUTING":
        # DRAFT -> CONFIRMED -> EXECUTING déjà persisté en mémoire de tour (CAS) — appel MCP réel.
        executing = outcome.draft
        try:
            gw = RecurringSupplyGateway(mc_runtime)
            mcp_result = await gw.create_recurring_need(
                phone=str(phone),
                product_query=executing.product,
                quantity=executing.quantity,
                unit=executing.unit,
                recurrence_type=executing.recurrence_type,
                weekly_days=executing.weekly_days,
                excluded_weekdays=executing.excluded_weekdays,
                starts_at=executing.starts_at,
                ends_at=executing.ends_at,
                max_price_per_unit=executing.max_price_per_unit,
                idempotency_key=f"recurring_need:{executing.draft_id}:{executing.version}",
            )
            exec_result = RecurringNeedExecutionResult(success=True, external_id=mcp_result.get("recurring_need_id"))
        except MCPCallError as exc:
            logger.warning("recurring_need.create_failed | draft=%s | %s", executing.draft_id, exc)
            exec_result = RecurringNeedExecutionResult(success=False, error=str(exc))
        finalized = finalize_after_execution(executing, exec_result)
        from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
            RecurringNeedOutcome,
            RecurringNeedOutcomeKind,
        )

        kind = (
            RecurringNeedOutcomeKind.RECURRING_NEED_CREATED
            if exec_result.success
            else RecurringNeedOutcomeKind.RECURRING_NEED_FAILED
        )
        outcome = RecurringNeedOutcome(kind=kind, draft=finalized)

    plan = build_response_plan(outcome)
    return _apply_response_plan(plan)


def _apply_response_plan(plan) -> Dict[str, Any]:
    patch: Dict[str, Any] = {
        "final_response": plan.final_response,
        "response_strategy": plan.response_strategy,
        "status": plan.graph_status,
        "recurring_need_draft": plan.draft.to_dict() if plan.draft else None,
    }
    if plan.terminal_goal_reset:
        patch["current_goal"] = None
    if not plan.pending_untouched:
        if plan.pending_kind is None:
            patch.update(clear_pending_interaction("recurring_need_response_plan"))
        else:
            patch.update(
                set_pending_interaction(
                    InteractionKind[plan.pending_kind],
                    goal="CREATE_RECURRING_NEED",
                    field_name=plan.pending_field,
                    context_ref="confirmation" if plan.pending_kind == "CONFIRM_ACTION" else None,
                    target=plan.pending_target,
                )
            )
    return patch


# =====================================================================
# UPDATE_RECURRING_NEED — action structurée sur un besoin déjà créé
# =====================================================================


async def _update_flow(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    phone = state.get("user_phone")
    payload: Dict[str, Any] = state.get("transaction_payload") or {}
    gw = RecurringSupplyGateway(mc_runtime)

    resolved = await _resolve_target_need(gw, phone, payload)
    if resolved is None:
        return {"final_response": "Vous n'avez aucun besoin actif pour l'instant.", "status": "COMPLETED"}
    if isinstance(resolved, str):
        return {"final_response": resolved, "status": "WAITING_INPUT"}
    need_id, product_label = resolved

    action, kwargs = _resolve_update_action(payload)
    if action is None:
        return {
            "final_response": "Je n'ai pas compris ce que vous voulez changer sur ce besoin.",
            "status": "WAITING_INPUT",
        }

    try:
        result = await gw.update_recurring_need(phone=str(phone), recurring_need_id=need_id, action=action, **kwargs)
    except MCPCallError as exc:
        logger.warning("recurring_need.update_failed | need=%s | action=%s | %s", need_id, action, exc)
        return {"final_response": "Je n'ai pas pu appliquer ce changement.", "status": "COMPLETED"}

    return {"final_response": _render_update_confirmation(action, product_label), "status": "COMPLETED", "result": result}


def _resolve_update_action(payload: Dict[str, Any]):
    """Mandat §6 : UNE action structurée, jamais un intent séparé par verbe."""
    action_hint = str(payload.get("action") or "").upper().strip()
    occurrence_date = payload.get("occurrence_date")
    quantity = payload.get("quantity")

    if action_hint == "PAUSE" or payload.get("pause"):
        return "PAUSE", {"paused_until": payload.get("paused_until")}
    if action_hint == "RESUME" or payload.get("resume"):
        return "RESUME", {}
    if action_hint == "CANCEL" or payload.get("cancel"):
        return "CANCEL", {}
    if action_hint == "OCCURRENCE_SKIP" or (occurrence_date and payload.get("skip")):
        return "OCCURRENCE_SKIP", {"occurrence_date": occurrence_date}
    if action_hint == "OCCURRENCE_OVERRIDE" or (occurrence_date and slot_has_value(quantity)):
        return "OCCURRENCE_OVERRIDE", {"occurrence_date": occurrence_date, "quantity": quantity}
    if action_hint == "PERMANENT_FREQUENCY" or payload.get("recurrence_type"):
        return "PERMANENT_FREQUENCY", {
            "recurrence_type": payload.get("recurrence_type"),
            "weekly_days": payload.get("weekly_days"),
            "excluded_weekdays": payload.get("excluded_weekdays"),
        }
    if action_hint == "PERMANENT_QUANTITY" or slot_has_value(quantity):
        return "PERMANENT_QUANTITY", {"quantity": quantity}
    return None, {}


def _render_update_confirmation(action: str, product_label: str) -> str:
    labels = {
        "PERMANENT_QUANTITY": f"C'est noté, la quantité de {product_label} est mise à jour.",
        "PERMANENT_FREQUENCY": f"C'est noté, la fréquence de {product_label} est mise à jour.",
        "PAUSE": f"D'accord, {product_label} est suspendu.",
        "RESUME": f"D'accord, {product_label} a repris.",
        "CANCEL": f"D'accord, {product_label} est annulé.",
        "OCCURRENCE_OVERRIDE": "C'est noté pour cette date.",
        "OCCURRENCE_SKIP": "D'accord, pas de livraison à cette date.",
    }
    return labels.get(action, "C'est fait.")


# =====================================================================
# GET_MY_NEEDS — liste + sous-menu
# =====================================================================


async def _get_my_needs_flow(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    phone = state.get("user_phone")
    gw = RecurringSupplyGateway(mc_runtime)
    try:
        result = await gw.list_my_recurring_needs(phone=str(phone))
    except MCPCallError:
        return {"final_response": "Je n'ai pas pu récupérer vos besoins.", "status": "COMPLETED"}

    items: List[Dict[str, Any]] = result.get("items") or []
    if not items:
        return {"final_response": "Vous n'avez pas encore de besoin récurrent enregistré.", "status": "COMPLETED"}

    lines = ["Vos approvisionnements :", ""]
    for i, item in enumerate(items, start=1):
        lines.append(f"{i}. {_render_need_line(item)}")
    lines += ["", "1. Modifier", "2. Mettre en pause/reprendre", "3. Annuler", "4. Retour"]
    return {
        "final_response": "\n".join(lines),
        "status": "COMPLETED",
        **set_pending_interaction(
            InteractionKind.SELECTION,
            goal="GET_MY_NEEDS",
            candidates=tuple(str(i.get("recurring_need_id")) for i in items),
        ),
    }


def _render_need_line(item: Dict[str, Any]) -> str:
    freq = {
        "DAILY": "jour",
        "WEEKLY": "semaine",
        "ONE_OFF": "une fois",
    }.get(item.get("recurrence_type"), "jour")
    status = "actif" if item.get("status") == "ACTIVE" else "en pause" if item.get("status") == "PAUSED" else "annulé"
    return f"{item.get('product', '?').capitalize()} — {item.get('quantity')} {item.get('unit')}/{freq} — {status}"


async def _resolve_target_need(gw: RecurringSupplyGateway, phone: Any, payload: Dict[str, Any]):
    """Résout le besoin visé par nom de produit — jamais un UUID brut (même principe que
    `PROCUREMENT_UPDATE_REQUEST`). Retourne `(need_id, label)`, `None` (aucun besoin), ou une chaîne
    (question de clarification, ambiguïté)."""
    try:
        result = await gw.list_my_recurring_needs(phone=str(phone))
    except MCPCallError:
        return "Je n'ai pas pu récupérer vos besoins."
    items: List[Dict[str, Any]] = result.get("items") or []
    active = [i for i in items if i.get("status") in ("ACTIVE", "PAUSED")]
    if not active:
        return None
    if len(active) == 1:
        return active[0]["recurring_need_id"], active[0]["product"]

    product_hint = str(payload.get("product") or "").strip().lower()
    if product_hint:
        matches = [i for i in active if product_hint in str(i.get("product") or "").lower()]
        if len(matches) == 1:
            return matches[0]["recurring_need_id"], matches[0]["product"]

    names = ", ".join(i["product"] for i in active)
    return f"Vous avez plusieurs besoins actifs ({names}) — lequel voulez-vous modifier ?"


__all__ = ["recurring_need_flow"]
