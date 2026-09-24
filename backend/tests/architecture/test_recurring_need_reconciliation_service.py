"""Réconciliation des confirmations de besoin récurrent en doute : rejeu SÛR de la même
confirmation (garde durable), jamais un doublon. Le contrat de la garde est celui de
`tests/harness/recurring.py::RecurringSupplyServerDouble`, prouvé contre PostgreSQL dans
`tests/schema/test_recurring_need_confirmation_ledger.py`."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
    RecurringNeedDraft,
)
from ladini.services.reconciliation.recurring_need_reconciliation_service import (
    ReconciliationOutcome,
    find_in_doubt_candidates,
    reconcile,
)
from tests.conftest import run
from tests.harness.recurring import RecurringSupplyServerDouble


def _in_doubt(table, draft_id="d-1", *, unknown=False) -> RecurringNeedDraft:
    draft = RecurringNeedDraft.new(
        draft_id=draft_id, product="coq", quantity=14.0, unit="TETE", recurrence_type="WEEKLY"
    )._confirm_to_executing()
    run(table.insert(draft, conversation_id="+226"))
    if unknown:
        from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
            RecurringNeedDraftStatus,
        )

        moved = draft.with_status(RecurringNeedDraftStatus.EXECUTION_UNKNOWN)
        run(table.compare_and_swap(draft.draft_id, expected_version=draft.version, new_draft=moved))
    return run(table.load(draft_id))


def _executor(server: RecurringSupplyServerDouble):
    async def _run(draft, conversation_id):
        return server.create_recurring_needs(
            phone=conversation_id,
            items=[{"product_query": draft.product, "quantity": draft.quantity, "unit": draft.unit}],
            recurrence_type=draft.recurrence_type,
            draft_id=draft.draft_id,
            draft_version=draft.execution_version,
        )

    return _run


def test_a_committed_confirmation_is_never_in_doubt(recurring_draft_table):
    """La garde passe le draft à EXECUTED dans la transaction des besoins : un COMMIT dont la
    réponse a été perdue laisse une ligne déjà tranchée, jamais une exécution à rejouer."""
    server = RecurringSupplyServerDouble(recurring_draft_table)
    draft = _in_doubt(recurring_draft_table)
    run(_executor(server)(draft, "+226"))  # commit dont le tour d'origine a perdu la réponse
    assert run(find_in_doubt_candidates(older_than_seconds=0)) == []
    result = run(reconcile(draft, "+226", executor=_executor(server)))
    assert result.outcome is ReconciliationOutcome.NOT_IN_DOUBT
    assert result.final_status == "EXECUTED"
    assert server.executions == 1


def test_a_confirmation_that_never_ran_is_executed_exactly_once(recurring_draft_table):
    server = RecurringSupplyServerDouble(recurring_draft_table)
    draft = _in_doubt(recurring_draft_table, unknown=True)
    first = run(reconcile(draft, "+226", executor=_executor(server)))
    again = run(reconcile(draft, "+226", executor=_executor(server)))
    assert first.outcome is ReconciliationOutcome.EXECUTED
    assert again.outcome is ReconciliationOutcome.NOT_IN_DOUBT
    assert server.executions == 1 and len(server.created) == 1


def test_a_business_error_settles_the_draft_as_failed(recurring_draft_table):
    server = RecurringSupplyServerDouble(recurring_draft_table)
    server.fail_with = {"status": "error", "message": "Produit inconnu."}
    draft = _in_doubt(recurring_draft_table)
    result = run(reconcile(draft, "+226", executor=_executor(server)))
    assert result.outcome is ReconciliationOutcome.FAILED
    assert recurring_draft_table.status_of("d-1") == "FAILED"
    assert server.created == []


def test_a_technical_error_leaves_the_draft_in_doubt_for_the_next_pass(recurring_draft_table):
    async def broken(_draft, _cid):
        raise ConnectionError("DB down")

    draft = _in_doubt(recurring_draft_table)
    result = run(reconcile(draft, "+226", executor=broken))
    assert result.outcome is ReconciliationOutcome.STILL_IN_DOUBT
    assert recurring_draft_table.status_of("d-1") == "EXECUTING"


def test_only_stale_in_doubt_drafts_are_candidates(recurring_draft_table):
    _in_doubt(recurring_draft_table, "fresh")
    _in_doubt(recurring_draft_table, "stale")
    recurring_draft_table.age("stale", 3600)
    candidates = run(find_in_doubt_candidates(older_than_seconds=120))
    assert [d.draft_id for d, _ in candidates] == ["stale"]


def test_overlapping_reconciliation_passes_never_duplicate(recurring_draft_table):
    server = RecurringSupplyServerDouble(recurring_draft_table)
    draft = _in_doubt(recurring_draft_table)
    for _ in range(3):
        run(reconcile(draft, "+226", executor=_executor(server)))
    assert server.executions == 1
