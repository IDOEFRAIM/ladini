"""Orchestration node — cycle de vie du `SalesPublishDraft` (2026-09-04,
migration SALES, même standard que `procurement_confirmation.py`).

Différence structurelle assumée avec PROCUREMENT (documentée, pas cachée) :
SALES n'a PAS de deuxième effet externe (pas d'escrow, pas de GPS) — le
plus proche gabarit possible est `ProcurementDraft`, réutilisé comme
modèle de STRUCTURE (5 mêmes responsabilités), jamais de CODE partagé
au-delà des primitives transverses (`PendingInteraction`/
`ConfirmationTarget`/`claim_once`/`cas_finalize`).

`SALES_PUBLISH_PRODUCT` réutilise le pipeline d'exécution GÉNÉRIQUE
existant (`nodes/executor.py::mcp_tool_executor` → `create_product`) —
CE module ne fait PAS l'appel MCP lui-même (contrairement à PREORDER) :
`build_response_plan`/`apply_response_plan` posent
`transaction_payload = draft.execution_payload()` +
`execution_authorized=True`, exactement le contrat que
`mcp_tool_executor`/`actions/sales.py::prep_sales_publish_product`
consomment déjà, INCHANGÉS. La finalisation post-exécution (EXECUTING →
PUBLISHED/FAILED/EXECUTION_UNKNOWN) est un nœud séparé,
`sales_execution_finalizer.py`, sur le modèle de
`procurement_execution_finalizer.py`."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from agriconnect.core.telemetry import record_procurement_transaction_event
from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    clear_pending_interaction,
    resolve_pending_interaction,
    set_pending_interaction,
)
from agriconnect.graphs.agents.market_coach.domain.sales_publish_draft import (
    SalesPublishDraft,
    SalesPublishOutcomeKind as _Kind,
    SalesPublishResponsePlan,
    apply_domain_action,
    build_response_plan,
    check_confirmation_target_invariant,
    resolve_domain_action,
)
from agriconnect.graphs.agents.market_coach.utils import llm_deviation_reply
from agriconnect.services.database import sales_publish_draft_store
from agriconnect.services.database.draft_store_support import cas_finalize

logger = logging.getLogger("AgriConnect.MarketCoach.SalesConfirmation")


def _target_of(pending_interaction: Any) -> Optional[Dict[str, Any]]:
    if isinstance(pending_interaction, dict):
        target = pending_interaction.get("target")
        return target if isinstance(target, dict) else None
    return None


def _is_mutation(before: Optional[SalesPublishDraft], after: Optional[SalesPublishDraft]) -> bool:
    if before is None or after is None:
        return before is not after
    return before.version != after.version or before.status != after.status


async def resolve_sales_confirmation(
    state: Dict[str, Any], mc_runtime: Any
) -> Dict[str, Any]:
    """Point d'entrée — CONFIRM/UPDATE/REJECT/CANCEL sur un draft déjà créé
    (voir `bootstrap_sales_publish_draft` pour la création v1, hors de ce
    module, appelée par `nodes/confirmation_gate.py`)."""
    interpreted_event = str(state.get("interpreted_event") or "").upper()
    extracted_entities = state.get("extracted_entities") or {}
    pending_target = _target_of(state.get("pending_interaction"))

    cached = state.get("sales_publish_draft") or {}
    draft_id = cached.get("draft_id") or (pending_target or {}).get("draft_id")
    draft = await sales_publish_draft_store.load(draft_id) if draft_id else None
    if draft is None and cached:
        logger.warning("SALES_PUBLISH_DRAFT_DB_UNAVAILABLE_FALLBACK_TO_CACHE | draft_id=%s", draft_id)
        draft = SalesPublishDraft.from_dict(cached)

    entry_violation = check_confirmation_target_invariant(pending_target, draft)
    if entry_violation:
        logger.warning("SALES_PUBLISH_INVARIANT_VIOLATION | on_entry | %s", entry_violation)

    action = resolve_domain_action(
        interpreted_event=interpreted_event,
        extracted_entities=extracted_entities,
        pending_target=pending_target,
    )
    outcome = apply_domain_action(draft, action)

    if draft is not None and _is_mutation(draft, outcome.draft):
        finalized_candidate = outcome.draft
        persisted, reconciled_draft = await cas_finalize(
            compare_and_swap=sales_publish_draft_store.compare_and_swap,
            load=sales_publish_draft_store.load,
            original=draft,
            finalized=finalized_candidate,
        )
        if not persisted:
            if reconciled_draft is not finalized_candidate:
                from agriconnect.graphs.agents.market_coach.domain.sales_publish_draft import (
                    SalesPublishOutcome,
                )

                outcome = SalesPublishOutcome(kind=_Kind.VERSION_CONFLICT, draft=reconciled_draft)
                logger.warning(
                    "SALES_PUBLISH_VERSION_CONFLICT | draft_id=%s | expected_version=%s | current_version=%s",
                    draft.draft_id,
                    draft.version,
                    reconciled_draft.version,
                )
            else:
                logger.error(
                    "SALES_PUBLISH_DRAFT_PERSISTENCE_UNAVAILABLE | draft_id=%s | "
                    "version %s -> %s NON PERSISTÉE (DB injoignable) | DEGRADED",
                    draft.draft_id,
                    draft.version,
                    outcome.draft.version if outcome.draft else None,
                )

    deviation_note: Optional[str] = None
    if outcome.kind == _Kind.DRAFT_UNCHANGED and outcome.draft:
        summary = outcome.draft.render_summary()
        user_text = str(state.get("normalized_text") or state.get("user_query") or "").strip()
        if summary:
            deviation_note = await llm_deviation_reply(
                mc_runtime, user_text, f"un récapitulatif à confirmer :\n{summary}"
            )

    plan = build_response_plan(outcome, deviation_note=deviation_note)
    patch = apply_response_plan(plan)

    exit_target = _target_of(patch.get("pending_interaction"))
    exit_violation = check_confirmation_target_invariant(exit_target, outcome.draft)
    if exit_violation:
        logger.error("SALES_PUBLISH_INVARIANT_VIOLATION | on_exit | %s", exit_violation)

    record_procurement_transaction_event(
        event_id=uuid.uuid4().hex,
        conversation_id=str(state.get("user_phone") or state.get("session_id") or "") or None,
        message_id=state.get("message_sid"),
        draft_id=draft.draft_id if draft else None,
        draft_version_before=draft.version if draft else None,
        draft_version_after=outcome.draft.version if outcome.draft else None,
        pending_kind=(
            (state.get("pending_interaction") or {}).get("kind")
            if isinstance(state.get("pending_interaction"), dict)
            else None
        ),
        confirmation_target=pending_target,
        interpreter_event=interpreted_event,
        domain_action=type(action).__name__,
        state_before=draft.status.value if draft else None,
        state_after=outcome.draft.status.value if outcome.draft else None,
        outcome=outcome.kind.value,
    )
    return patch


def apply_response_plan(plan: SalesPublishResponsePlan) -> Dict[str, Any]:
    """`SalesPublishResponsePlan` → patch d'état. MÉCANIQUE uniquement —
    n'importe même pas `SalesPublishOutcomeKind` (même preuve structurelle
    que `procurement_confirmation.py::apply_response_plan`)."""
    patch: Dict[str, Any] = {
        "sales_publish_draft": plan.draft.to_dict() if plan.draft else None,
        "status": plan.graph_status,
        "response_strategy": plan.response_strategy,
        "ag_ui_component": None,
        "execution_authorized": False,
        "is_certified": False,
    }
    if plan.final_response:
        patch["final_response"] = plan.final_response

    if plan.terminal_goal_reset:
        patch["current_goal"] = None
        patch["goal_status"] = "COMPLETED"

    if plan.pending_untouched:
        pass
    elif plan.pending_kind == "CONFIRM_ACTION":
        patch.update(
            set_pending_interaction(
                InteractionKind.CONFIRM_ACTION, context_ref="confirmation", target=plan.pending_target
            )
        )
    elif plan.pending_kind == "ENTER_FIELD":
        patch.update(set_pending_interaction(InteractionKind.ENTER_FIELD, field_name=plan.pending_field))
    elif plan.ready_for_execution or plan.graph_status == "COMPLETED":
        patch.update(resolve_pending_interaction())
    else:
        patch.update(clear_pending_interaction("sales_publish_response_plan_no_target"))

    if plan.ready_for_execution and plan.draft is not None:
        patch["transaction_payload"] = plan.draft.execution_payload()
        patch["is_certified"] = True
        patch["execution_authorized"] = True

    return patch


__all__ = ["resolve_sales_confirmation", "apply_response_plan"]
