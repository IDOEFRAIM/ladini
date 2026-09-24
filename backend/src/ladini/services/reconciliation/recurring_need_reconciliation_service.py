"""Réconciliation des confirmations de besoin récurrent restées EN DOUTE.

Un draft est « en doute » (`EXECUTING`/`EXECUTION_UNKNOWN`) quand une exécution a été
lancée sans que son issue soit connue : timeout MCP, crash du worker après le COMMIT
métier, flush de l'état perdu...

Différence volontaire avec la réconciliation PROCUREMENT (qui ne rejoue JAMAIS l'écriture,
faute de garde durable) : ici, la garde existe. `services/database/recurring_supply.py`
verrouille la ligne du draft et la passe à EXECUTED dans la MÊME transaction que les
besoins. Rejouer LA MÊME confirmation `(draft_id, execution_version)` est donc sûr :
  - elle avait déjà abouti  -> la garde REJOUE le résultat stocké, rien n'est inséré ;
  - elle n'avait pas abouti -> elle s'exécute une fois, atomiquement ;
  - erreur métier           -> rollback, le draft passe FAILED.
Une erreur technique laisse le draft en doute : le prochain passage réessaiera.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ladini.core.settings import settings
from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
    RecurringNeedDraft,
    RecurringNeedDraftStatus,
    RecurringNeedExecutionResult,
    finalize_after_execution,
)
from ladini.services.database import recurring_need_draft_store
from ladini.services.database.draft_store_support import cas_finalize

logger = logging.getLogger("ladini.services.reconciliation.recurring_need")

Executor = Callable[[RecurringNeedDraft, str], Awaitable[Dict[str, Any]]]


class ReconciliationOutcome(str, Enum):
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    STILL_IN_DOUBT = "STILL_IN_DOUBT"
    NOT_IN_DOUBT = "NOT_IN_DOUBT"


@dataclass(frozen=True)
class ReconciliationResult:
    draft_id: str
    outcome: ReconciliationOutcome
    final_status: Optional[str]


def _items(draft: RecurringNeedDraft) -> List[Dict[str, Any]]:
    items = [{"product_query": draft.product, "quantity": draft.quantity, "unit": draft.unit}]
    for it in draft.additional_items or []:
        items.append({"product_query": it.get("product"), "quantity": it.get("quantity"), "unit": it.get("unit")})
    return items


async def _default_executor(draft: RecurringNeedDraft, conversation_id: str) -> Dict[str, Any]:
    """Rejoue la confirmation via le service métier (même garde que l'appel MCP)."""
    from ladini.services.database.d import AgriDatabaseService

    result: Dict[str, Any] = await AgriDatabaseService().create_recurring_needs(
        phone=conversation_id,
        items=_items(draft),
        recurrence_type=draft.recurrence_type,
        weekly_days=draft.weekly_days,
        excluded_weekdays=draft.excluded_weekdays,
        starts_at=draft.starts_at,
        ends_at=draft.ends_at,
        max_price_per_unit=draft.max_price_per_unit,
        draft_id=draft.draft_id,
        draft_version=draft.execution_version,
    )
    return result


async def find_in_doubt_candidates(
    older_than_seconds: Optional[float] = None,
) -> List[Tuple[RecurringNeedDraft, str]]:
    threshold = (
        older_than_seconds
        if older_than_seconds is not None
        else settings.RECURRING_NEED_EXECUTING_STALE_SECONDS
    )
    candidates: List[Tuple[RecurringNeedDraft, str]] = await recurring_need_draft_store.find_in_doubt(
        older_than_seconds=threshold
    )
    return candidates


async def reconcile(
    draft: RecurringNeedDraft, conversation_id: str, *, executor: Optional[Executor] = None
) -> ReconciliationResult:
    current = await recurring_need_draft_store.load(draft.draft_id) or draft
    if not current.is_in_doubt():
        return ReconciliationResult(current.draft_id, ReconciliationOutcome.NOT_IN_DOUBT, current.status.value)
    run = executor or _default_executor
    try:
        result = await run(current, conversation_id)
    except Exception as exc:  # technique : on réessaiera au prochain passage
        logger.warning("RECURRING_RECONCILIATION_RETRY_LATER | draft_id=%s | %s", current.draft_id, exc)
        return ReconciliationResult(current.draft_id, ReconciliationOutcome.STILL_IN_DOUBT, current.status.value)

    success = isinstance(result, dict) and str(result.get("status") or "").lower() == "success"
    finalized = finalize_after_execution(
        current,
        RecurringNeedExecutionResult(success=success, error=None if success else str((result or {}).get("message"))),
    )
    _persisted, final = await cas_finalize(
        compare_and_swap=recurring_need_draft_store.compare_and_swap,
        load=recurring_need_draft_store.load,
        original=current,
        finalized=finalized,
    )
    outcome = (
        ReconciliationOutcome.EXECUTED
        if final.status == RecurringNeedDraftStatus.EXECUTED
        else ReconciliationOutcome.FAILED
        if final.status == RecurringNeedDraftStatus.FAILED
        else ReconciliationOutcome.STILL_IN_DOUBT
    )
    logger.info(
        "RECURRING_RECONCILIATION | draft_id=%s | outcome=%s | replayed=%s",
        final.draft_id, outcome.value, bool(isinstance(result, dict) and result.get("replayed")),
    )
    return ReconciliationResult(final.draft_id, outcome, final.status.value)


__all__ = [
    "ReconciliationOutcome",
    "ReconciliationResult",
    "find_in_doubt_candidates",
    "reconcile",
]
