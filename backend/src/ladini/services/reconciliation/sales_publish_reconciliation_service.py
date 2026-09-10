"""`SalesPublishReconciliationService` — recovery des `SalesPublishDraft`
bloqués en `EXECUTING` (2026-09-04, migration SALES). Calque STRUCTUREL de
`procurement_reconciliation_service.py` (même philosophie EXECUTION_UNKNOWN
— mandat hardening §6 : PROCUREMENT et PREORDER partagent la même
philosophie "jamais de blind retry, réconciliation, preuve externe,
finalisation" ; SALES la reprend à l'identique, pas une variante).

```
EXECUTING
    │
    ▼
mcp_idempotency_store.peek(execution_key(draft), "create_product")
    │
    ├── COMPLETED trouvé  → EXTERNAL_EFFECT_FOUND → finalize → PUBLISHED
    ├── FAILED trouvé     → CONFIRMED_NO_EFFECT    → finalize → FAILED
    └── PENDING / absent  → AMBIGUOUS              → finalize → EXECUTION_UNKNOWN
```

Réutilise `adapt_mcp_result`/`finalize_after_execution`
(`domain/sales_publish_draft.py`) et `cas_finalize`
(`services/database/draft_store_support.py`) — AUCUN adaptateur/primitive
de finalisation dupliqués."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from ladini.core.settings import settings
from ladini.core.telemetry import record_procurement_reconciliation_event
from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
    SalesPublishDraft,
    SalesPublishDraftStatus,
    SalesPublishOutcome,
    SalesPublishOutcomeKind,
    adapt_mcp_result,
    build_response_plan,
    execution_key,
    finalize_after_execution,
)
from ladini.services.database import mcp_idempotency_store, sales_publish_draft_store
from ladini.services.database.draft_store_support import cas_finalize

logger = logging.getLogger("ladini.services.reconciliation.sales_publish")

# Le SEUL outil produit par SALES_PUBLISH_PRODUCT — couplage EXPLICITE et
# documenté, même discipline que PROCUREMENT_MCP_TOOL_NAME.
SALES_PUBLISH_MCP_TOOL_NAME = "create_product"


class ReconciliationOutcome(str, Enum):
    EXTERNAL_EFFECT_FOUND = "EXTERNAL_EFFECT_FOUND"
    CONFIRMED_NO_EFFECT = "CONFIRMED_NO_EFFECT"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_APPLICABLE = "NOT_APPLICABLE"


_OUTCOME_KIND_BY_TERMINAL_STATUS = {
    SalesPublishDraftStatus.PUBLISHED: SalesPublishOutcomeKind.SALES_PUBLISHED,
    SalesPublishDraftStatus.FAILED: SalesPublishOutcomeKind.SALES_FAILED,
    SalesPublishDraftStatus.EXECUTION_UNKNOWN: SalesPublishOutcomeKind.SALES_EXECUTION_UNKNOWN,
}

_RECONCILIATION_EVENT_NAME_BY_OUTCOME = {
    ReconciliationOutcome.EXTERNAL_EFFECT_FOUND: "RECONCILIATION_FOUND_EXTERNAL_EFFECT",
    ReconciliationOutcome.CONFIRMED_NO_EFFECT: "RECONCILIATION_CONFIRMED_NO_EFFECT",
    ReconciliationOutcome.AMBIGUOUS: "RECONCILIATION_UNKNOWN",
}


@dataclass(frozen=True)
class ReconciliationResult:
    draft_id: str
    version_before: int
    outcome: ReconciliationOutcome
    final_status: Optional[str]
    finalized_draft: Optional[SalesPublishDraft]
    persisted: bool


async def find_stale_executing_candidates() -> List[SalesPublishDraft]:
    """Point d'entrée — seuil configurable (`settings`, jamais codé en dur)."""
    return await sales_publish_draft_store.find_stale_by_status(
        "EXECUTING", older_than_seconds=settings.SALES_EXECUTING_STALE_SECONDS
    )


async def reconcile_draft(draft: SalesPublishDraft, *, attempt: int = 1) -> ReconciliationResult:
    if draft.status != SalesPublishDraftStatus.EXECUTING:
        return ReconciliationResult(
            draft_id=draft.draft_id,
            version_before=draft.version,
            outcome=ReconciliationOutcome.NOT_APPLICABLE,
            final_status=draft.status.value,
            finalized_draft=draft,
            persisted=False,
        )

    key = execution_key(draft)
    record_procurement_reconciliation_event(
        event_name="STALE_EXECUTING_DETECTED",
        draft_id=draft.draft_id,
        draft_version=draft.version,
        execution_key=key,
        state_before=draft.status.value,
        state_after=None,
        reconciliation_reason="executing_past_stale_threshold",
        external_lookup=None,
        attempt=attempt,
        outcome="PENDING_LOOKUP",
    )

    record = await mcp_idempotency_store.peek(key, SALES_PUBLISH_MCP_TOOL_NAME)

    external_lookup = "peek_none"
    if record is None:
        outcome = ReconciliationOutcome.AMBIGUOUS
        mcp_result = adapt_mcp_result(None, None)
    elif record.status == "COMPLETED":
        external_lookup = "peek_completed"
        outcome = ReconciliationOutcome.EXTERNAL_EFFECT_FOUND
        raw = record.external_result if isinstance(record.external_result, dict) else {}
        mcp_result = adapt_mcp_result("COMPLETED", raw)
    elif record.status == "FAILED":
        external_lookup = "peek_failed"
        outcome = ReconciliationOutcome.CONFIRMED_NO_EFFECT
        raw = (
            record.external_result
            if isinstance(record.external_result, dict)
            else {"message": "reconciliation_confirmed_no_effect"}
        )
        mcp_result = adapt_mcp_result("ERROR", raw)
    else:
        external_lookup = "peek_pending"
        outcome = ReconciliationOutcome.AMBIGUOUS
        mcp_result = adapt_mcp_result(None, None)

    finalized_candidate = finalize_after_execution(draft, mcp_result)
    persisted, finalized = await cas_finalize(
        compare_and_swap=sales_publish_draft_store.compare_and_swap,
        load=sales_publish_draft_store.load,
        original=draft,
        finalized=finalized_candidate,
        log_conflict=lambda: logger.warning(
            "SALES_PUBLISH_RECONCILIATION_VERSION_CONFLICT | draft_id=%s | version=%s",
            draft.draft_id,
            draft.version,
        ),
    )

    outcome_kind = _OUTCOME_KIND_BY_TERMINAL_STATUS.get(finalized.status)
    if outcome_kind is not None:
        build_response_plan(SalesPublishOutcome(kind=outcome_kind, draft=finalized))

    logger.info(
        "SALES_PUBLISH_RECONCILIATION_TRACE | draft_id=%s | version_before=%s | "
        "version_after=%s | execution_key=%s | external_lookup=%s | "
        "reconciliation_outcome=%s | final_status=%s | persisted=%s",
        draft.draft_id,
        draft.version,
        finalized.version,
        key,
        external_lookup,
        outcome.value,
        finalized.status.value,
        persisted,
    )
    record_procurement_reconciliation_event(
        event_name=_RECONCILIATION_EVENT_NAME_BY_OUTCOME[outcome],
        draft_id=draft.draft_id,
        draft_version=finalized.version,
        execution_key=key,
        state_before=draft.status.value,
        state_after=finalized.status.value,
        reconciliation_reason=f"sales_publish_executing_{outcome.value.lower()}",
        external_lookup=external_lookup,
        external_result=mcp_result.product_id or mcp_result.error,
        attempt=attempt,
        outcome=outcome_kind.value if outcome_kind else outcome.value,
    )

    return ReconciliationResult(
        draft_id=draft.draft_id,
        version_before=draft.version,
        outcome=outcome,
        final_status=finalized.status.value,
        finalized_draft=finalized,
        persisted=persisted,
    )


__all__ = [
    "SALES_PUBLISH_MCP_TOOL_NAME",
    "ReconciliationOutcome",
    "ReconciliationResult",
    "find_stale_executing_candidates",
    "reconcile_draft",
]
