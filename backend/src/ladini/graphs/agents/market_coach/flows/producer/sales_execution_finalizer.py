"""Finalisation post-exécution — `SalesPublishDraft` EXECUTING → PUBLISHED/
FAILED/EXECUTION_UNKNOWN (2026-09-04, migration SALES). Nœud de graphe
INSÉRÉ entre `nodes/executor.py::mcp_tool_executor` (générique, inchangé)
et `response_strategy` — même position, même contrat que
`flows/buyer/procurement_execution_finalizer.py`.

    mcp_tool_executor (générique, transport MCP)
        ↓ status / execution_result (bruts)
    adapt_mcp_result()               [domain/sales_publish_draft.py]
        ↓ SalesPublishExecutionResult
    finalize_after_execution()       [domain/sales_publish_draft.py]
        ↓ SalesPublishDraft (PUBLISHED/FAILED/EXECUTION_UNKNOWN)
    build_response_plan()            [domain/sales_publish_draft.py]
        ↓ SalesPublishResponsePlan
    apply_response_plan (mécanique) ↓ patch d'état

No-op immédiat pour tout tour qui ne concerne pas un draft `EXECUTING`."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict

from ladini.core.telemetry import record_procurement_transaction_event
from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
    SalesPublishDraft,
    SalesPublishDraftStatus,
    SalesPublishOutcome,
    SalesPublishOutcomeKind,
    adapt_mcp_result,
    build_response_plan,
    finalize_after_execution,
)
from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
    execution_key as _execution_key,
)
from ladini.graphs.agents.market_coach.flows.producer.sales_confirmation import (
    apply_response_plan,
)
from ladini.graphs.agents.market_coach.services.domain.commercial_gate import (
    log_offer_lifecycle,
)
from ladini.services.database import sales_publish_draft_store
from ladini.services.database.draft_store_support import cas_finalize

logger = logging.getLogger("Ladini.MarketCoach.SalesExecutionFinalizer")

_OUTCOME_KIND_BY_TERMINAL_STATUS = {
    SalesPublishDraftStatus.PUBLISHED: SalesPublishOutcomeKind.SALES_PUBLISHED,
    SalesPublishDraftStatus.FAILED: SalesPublishOutcomeKind.SALES_FAILED,
    SalesPublishDraftStatus.EXECUTION_UNKNOWN: SalesPublishOutcomeKind.SALES_EXECUTION_UNKNOWN,
}


async def finalize_sales_publish_execution(
    state: Dict[str, Any], mc_runtime: Any
) -> Dict[str, Any]:
    cached = SalesPublishDraft.from_dict(state.get("sales_publish_draft"))
    if cached is None or cached.status != SalesPublishDraftStatus.EXECUTING:
        return {}

    draft = await sales_publish_draft_store.load(cached.draft_id) or cached

    execution_status = state.get("status")
    execution_result = state.get("execution_result")
    result = adapt_mcp_result(execution_status, execution_result)
    finalized_candidate = finalize_after_execution(draft, result)

    persisted, finalized_draft = await cas_finalize(
        compare_and_swap=sales_publish_draft_store.compare_and_swap,
        load=sales_publish_draft_store.load,
        original=draft,
        finalized=finalized_candidate,
    )
    if not persisted:
        if finalized_draft is not finalized_candidate:
            logger.warning(
                "SALES_PUBLISH_FINALIZATION_VERSION_CONFLICT | draft_id=%s | version=%s",
                draft.draft_id,
                draft.version,
            )
        else:
            logger.error(
                "SALES_PUBLISH_FINALIZATION_PERSISTENCE_UNAVAILABLE | draft_id=%s | "
                "version %s -> %s NON PERSISTÉE (DB injoignable) | DEGRADED",
                draft.draft_id,
                draft.version,
                finalized_draft.version,
            )

    outcome_kind = _OUTCOME_KIND_BY_TERMINAL_STATUS.get(finalized_draft.status)
    if outcome_kind is None:
        logger.error("SALES_PUBLISH_FINALIZER_UNEXPECTED_STATUS | status=%s", finalized_draft.status)
        return {}

    if result.success and draft.offer is not None:
        log_offer_lifecycle(
            "COMMERCIAL_OFFER_EXECUTED",
            state,
            draft.offer,
            draft_id=draft.draft_id,
            version=draft.version,
            idempotency_key=_execution_key(draft),
        )

    outcome = SalesPublishOutcome(kind=outcome_kind, draft=finalized_draft)
    plan = build_response_plan(outcome)
    patch = apply_response_plan(plan)

    logger.info(
        "SALES_PUBLISH_FINALIZATION_TRACE | draft_id=%s | version=%s | "
        "execution_status_in=%s | success=%s | ambiguous=%s | product_id=%s | "
        "final_status=%s | persisted=%s",
        draft.draft_id,
        draft.version,
        execution_status,
        result.success,
        result.ambiguous,
        result.product_id,
        finalized_draft.status.value,
        persisted,
    )
    record_procurement_transaction_event(
        event_id=uuid.uuid4().hex,
        conversation_id=str(state.get("user_phone") or state.get("session_id") or "") or None,
        message_id=state.get("message_sid"),
        draft_id=draft.draft_id,
        draft_version_before=draft.version,
        draft_version_after=finalized_draft.version,
        pending_kind=None,
        confirmation_target=None,
        interpreter_event=None,
        domain_action="finalize_after_execution",
        state_before=draft.status.value,
        state_after=finalized_draft.status.value,
        execution_key=_execution_key(draft),
        external_request_id=result.product_id,
        mcp_status=execution_status,
        execution_result="success" if result.success else ("ambiguous" if result.ambiguous else "error"),
        outcome=outcome_kind.value,
        error=result.error,
    )
    return patch


__all__ = ["finalize_sales_publish_execution"]
