"""`PreorderReconciliationService` — recovery des `PreorderDraft` bloqués
en `EXECUTING` ou `AWAITING_PAYMENT` (2026-09-03, clôture escrow/IPN — sur
le MODÈLE de `procurement_reconciliation_service.py`, en réutilisant les
primitives déjà validées, PAS un copier-coller).

## Deux candidats, deux inspections DIFFÉRENTES

```
EXECUTING (confirm_preorder_draft non-escrow en vol)
    │
    ▼
mcp_idempotency_store.peek(execution_key(draft), "confirm_preorder_draft")
    │   — EXACTEMENT le même mécanisme que PROCUREMENT (l'appel passe par
    │     le même chokepoint `AgriDBMCPServer.call_tool`)
    ├── COMPLETED → EXTERNAL_EFFECT_FOUND → EXECUTED
    ├── FAILED    → CONFIRMED_NO_EFFECT    → FAILED
    └── absent/PENDING → AMBIGUOUS         → EXECUTION_UNKNOWN

AWAITING_PAYMENT (paiement escrow en attente d'IPN)
    │
    ▼
flows/buyer/preorder_payment.py::reconcile_invoice(invoice_token)
    │   — RÉUTILISE le MÊME chemin que l'IPN réel (re-confirmation
    │     Paydunya serveur-à-serveur, PAS une supposition) : ce module ne
    │     fait QUE retrouver `invoice_token` (lecture `Order` par
    │     `order_id`) puis appeler cette fonction déjà idempotente.
    ├── "completed" confirmé → mark_escrow_paid → draft déjà synchronisé
    ├── "cancelled" confirmé → mark_escrow_payment_failed → draft synchronisé
    └── "pending"/erreur     → AUCUNE transition, reste AWAITING_PAYMENT
```

## Ce que ce module NE fait PAS (mandat §13)

Il n'appelle JAMAIS le graphe conversationnel, n'envoie JAMAIS de message
WhatsApp lui-même (`reconcile_invoice`/`apply_payment_outcome` s'en
chargent déjà, via Outbox, jamais un envoi direct). Il ne recrée JAMAIS
`create_preorder_draft`/`confirm_preorder_draft`/`initiate_escrow_payment`
— la réconciliation EXECUTING reste, comme PROCUREMENT, un classement +
une finalisation, jamais un retry d'écriture aveugle."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from ladini.core.settings import settings
from ladini.core.telemetry import record_procurement_reconciliation_event
from ladini.graphs.agents.market_coach.domain.preorder_draft import (
    PreorderDraft,
    PreorderDraftStatus,
    PreorderOutcome,
    PreorderOutcomeKind,
    adapt_mcp_result,
    build_response_plan,
    execution_key,
    finalize_after_execution,
)
from ladini.services.database import mcp_idempotency_store, preorder_draft_store
from ladini.services.database.draft_store_support import cas_finalize

logger = logging.getLogger("ladini.services.reconciliation.preorder")

# Le SEUL outil produit par la branche EXECUTING (non-escrow) du pipeline
# PREORDER — couplage EXPLICITE et documenté, même discipline que
# PROCUREMENT_MCP_TOOL_NAME.
PREORDER_MCP_TOOL_NAME = "confirm_preorder_draft"


class ReconciliationOutcome(str, Enum):
    EXTERNAL_EFFECT_FOUND = "EXTERNAL_EFFECT_FOUND"
    CONFIRMED_NO_EFFECT = "CONFIRMED_NO_EFFECT"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_APPLICABLE = "NOT_APPLICABLE"  # no-op : statut déjà terminal/différent


_OUTCOME_KIND_BY_TERMINAL_STATUS = {
    PreorderDraftStatus.EXECUTED: PreorderOutcomeKind.PREORDER_EXECUTED,
    PreorderDraftStatus.FAILED: PreorderOutcomeKind.PREORDER_FAILED,
    PreorderDraftStatus.EXECUTION_UNKNOWN: PreorderOutcomeKind.PREORDER_EXECUTION_UNKNOWN,
    PreorderDraftStatus.PAYMENT_FAILED: PreorderOutcomeKind.PREORDER_PAYMENT_FAILED,
    PreorderDraftStatus.PAYMENT_EXPIRED: PreorderOutcomeKind.PREORDER_PAYMENT_EXPIRED,
    PreorderDraftStatus.AWAITING_PAYMENT: PreorderOutcomeKind.PREORDER_AWAITING_PAYMENT,
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
    finalized_draft: Optional[PreorderDraft]
    persisted: bool


async def find_stale_executing_candidates() -> List[PreorderDraft]:
    """Point d'entrée — seuil configurable (`settings`, jamais codé en dur)."""
    return await preorder_draft_store.find_stale_by_status(
        "EXECUTING", older_than_seconds=settings.PREORDER_EXECUTING_STALE_SECONDS
    )


async def find_stale_awaiting_payment_candidates() -> List[PreorderDraft]:
    """Symétrique pour la branche paiement — même seuil (pas de raison
    métier de le distinguer, voir `settings.py`)."""
    return await preorder_draft_store.find_stale_by_status(
        "AWAITING_PAYMENT", older_than_seconds=settings.PREORDER_EXECUTING_STALE_SECONDS
    )


async def reconcile_executing_draft(draft: PreorderDraft, *, attempt: int = 1) -> ReconciliationResult:
    """EXECUTING (confirm_preorder_draft non-escrow) — mêmes 3 issues et la
    même limite honnête que PROCUREMENT (mcp_idempotency_store n'a une
    ligne QUE pour les tentatives ayant atteint `AgriDBMCPServer.call_tool`)."""
    if draft.status != PreorderDraftStatus.EXECUTING:
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

    record = await mcp_idempotency_store.peek(key, PREORDER_MCP_TOOL_NAME)

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
    # (2026-09-03, hardening transverse) : `cas_finalize` — même primitive
    # partagée que `procurement_reconciliation_service.reconcile_draft`
    # (voir sa docstring d'appel pour le détail du motif).
    persisted, finalized = await cas_finalize(
        compare_and_swap=preorder_draft_store.compare_and_swap,
        load=preorder_draft_store.load,
        original=draft,
        finalized=finalized_candidate,
        log_conflict=lambda: logger.warning(
            "PREORDER_RECONCILIATION_VERSION_CONFLICT | draft_id=%s | version=%s",
            draft.draft_id,
            draft.version,
        ),
    )

    outcome_kind = _OUTCOME_KIND_BY_TERMINAL_STATUS.get(finalized.status)
    if outcome_kind is not None:
        build_response_plan(PreorderOutcome(kind=outcome_kind, draft=finalized))

    logger.info(
        "PREORDER_RECONCILIATION_TRACE | phase=EXECUTING | draft_id=%s | version_before=%s | "
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
        reconciliation_reason=f"preorder_executing_{outcome.value.lower()}",
        external_lookup=external_lookup,
        external_result=mcp_result.order_id or mcp_result.error,
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


async def reconcile_awaiting_payment_draft(draft: PreorderDraft) -> ReconciliationResult:
    """AWAITING_PAYMENT (escrow) — retrouve `invoice_token` via `Order`
    (lecture directe, best-effort, même précédent que
    `services/database/procurement_draft_store.py`), puis délègue
    ENTIÈREMENT à `reconcile_invoice` (RÉUTILISÉ, jamais dupliqué — mandat
    §26). Ne fait AUCUNE supposition elle-même sur le statut Paydunya."""
    if draft.status != PreorderDraftStatus.AWAITING_PAYMENT:
        return ReconciliationResult(
            draft_id=draft.draft_id,
            version_before=draft.version,
            outcome=ReconciliationOutcome.NOT_APPLICABLE,
            final_status=draft.status.value,
            finalized_draft=draft,
            persisted=False,
        )

    invoice_token = await _find_invoice_token(draft.order_id) if draft.order_id else None
    if not invoice_token:
        logger.warning(
            "PREORDER_RECONCILIATION_NO_INVOICE_TOKEN | draft_id=%s | order_id=%s",
            draft.draft_id,
            draft.order_id,
        )
        return ReconciliationResult(
            draft_id=draft.draft_id,
            version_before=draft.version,
            outcome=ReconciliationOutcome.AMBIGUOUS,
            final_status=draft.status.value,
            finalized_draft=draft,
            persisted=False,
        )

    from ladini.graphs.agents.market_coach.flows.buyer.preorder_payment import (
        reconcile_invoice,
    )

    before_status = draft.status
    ipn_result = await reconcile_invoice(invoice_token)

    reloaded = await preorder_draft_store.load(draft.draft_id)
    finalized = reloaded or draft
    if finalized.status == before_status:
        outcome = ReconciliationOutcome.AMBIGUOUS
    elif finalized.status == PreorderDraftStatus.EXECUTED:
        outcome = ReconciliationOutcome.EXTERNAL_EFFECT_FOUND
    elif finalized.status == PreorderDraftStatus.PAYMENT_FAILED:
        outcome = ReconciliationOutcome.CONFIRMED_NO_EFFECT
    else:
        outcome = ReconciliationOutcome.AMBIGUOUS

    logger.info(
        "PREORDER_RECONCILIATION_TRACE | phase=AWAITING_PAYMENT | draft_id=%s | "
        "order_id=%s | invoice_token=%s | paydunya_status=%s | final_status=%s",
        draft.draft_id,
        draft.order_id,
        invoice_token,
        ipn_result.get("status"),
        finalized.status.value,
    )
    record_procurement_reconciliation_event(
        event_name=_RECONCILIATION_EVENT_NAME_BY_OUTCOME.get(outcome, "RECONCILIATION_UNKNOWN"),
        draft_id=draft.draft_id,
        draft_version=finalized.version,
        execution_key=None,
        state_before=before_status.value,
        state_after=finalized.status.value,
        reconciliation_reason="preorder_awaiting_payment_reconcile",
        external_lookup=f"paydunya_status={ipn_result.get('status')}",
        external_result=str(ipn_result.get("order_id") or ipn_result.get("reason") or ""),
        attempt=1,
        outcome=finalized.status.value,
    )

    return ReconciliationResult(
        draft_id=draft.draft_id,
        version_before=draft.version,
        outcome=outcome,
        final_status=finalized.status.value,
        finalized_draft=finalized,
        persisted=finalized.status != before_status,
    )


async def _find_invoice_token(order_id: str) -> Optional[str]:
    """Best-effort — session autonome, même précédent que les stores
    (`get_sessionmaker`, jamais d'exception qui remonte)."""
    try:
        from sqlalchemy import select

        from ladini.core.database import get_sessionmaker
        from ladini.domain.models import Order

        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            return None
        async with sessionmaker() as session:
            token = await session.scalar(
                select(Order.paydunya_invoice_token).where(Order.id == order_id)
            )
            return str(token) if token else None
    except Exception:
        logger.exception("PREORDER_RECONCILIATION_FIND_INVOICE_TOKEN_FAILED | order_id=%s", order_id)
        return None


__all__ = [
    "PREORDER_MCP_TOOL_NAME",
    "ReconciliationOutcome",
    "ReconciliationResult",
    "find_stale_executing_candidates",
    "find_stale_awaiting_payment_candidates",
    "reconcile_executing_draft",
    "reconcile_awaiting_payment_draft",
]
