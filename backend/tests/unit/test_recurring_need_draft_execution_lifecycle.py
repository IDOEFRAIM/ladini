"""Cycle d'exécution de `RecurringNeedDraft` (Phase 2, P0)."""
from __future__ import annotations

import pytest

from ladini.graphs.agents.market_coach.core.confirmation_target import (
    ConfirmationTarget,
)
from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
    ConfirmRecurringNeedDraft,
    IllegalDraftTransition,
    RecurringNeedDraft,
    RecurringNeedDraftStatus,
    RecurringNeedExecutionResult,
    RecurringNeedOutcomeKind,
    apply_domain_action,
    execution_key,
    finalize_after_execution,
)

S = RecurringNeedDraftStatus


def _draft() -> RecurringNeedDraft:
    return RecurringNeedDraft.new(draft_id="d", product="coq", quantity=14.0, unit="TETE", recurrence_type="WEEKLY")


def _confirm(draft, version=None):
    target = ConfirmationTarget(draft_id=draft.draft_id, draft_version=draft.version if version is None else version)
    return apply_domain_action(draft, ConfirmRecurringNeedDraft(target=target), claim=lambda key: True)


def test_confirmation_fixes_the_execution_version_used_as_durable_key():
    executing = _confirm(_draft()).draft
    assert executing.status is S.EXECUTING
    assert executing.execution_version == executing.version == 2
    assert execution_key(executing) == "recurring_need:d:2"


def test_an_ambiguous_outcome_is_in_doubt_not_terminal_and_keeps_its_key():
    executing = _confirm(_draft()).draft
    unknown = finalize_after_execution(executing, RecurringNeedExecutionResult(success=False, ambiguous=True))
    assert unknown.status is S.EXECUTION_UNKNOWN
    assert unknown.is_in_doubt() and not unknown.is_terminal()
    assert execution_key(unknown) == execution_key(executing)


@pytest.mark.parametrize("status", [S.EXECUTING, S.EXECUTION_UNKNOWN])
def test_confirming_an_in_doubt_draft_resumes_the_same_execution(status):
    draft = _confirm(_draft()).draft
    if status is S.EXECUTION_UNKNOWN:
        draft = finalize_after_execution(draft, RecurringNeedExecutionResult(success=False, ambiguous=True))
    outcome = _confirm(draft, version=1)  # la projection du tour précédent a pu être perdue
    assert outcome.kind is RecurringNeedOutcomeKind.RESUME_EXECUTION
    assert outcome.draft is draft


def test_an_in_doubt_draft_of_another_request_is_not_resumed():
    draft = _confirm(_draft()).draft
    target = ConfirmationTarget(draft_id="other", draft_version=1)
    outcome = apply_domain_action(draft, ConfirmRecurringNeedDraft(target=target), claim=lambda key: True)
    assert outcome.kind is RecurringNeedOutcomeKind.STALE_TARGET


def test_in_doubt_resolves_to_executed_or_failed_and_terminal_drafts_never_move():
    unknown = finalize_after_execution(
        _confirm(_draft()).draft, RecurringNeedExecutionResult(success=False, ambiguous=True)
    )
    executed = finalize_after_execution(unknown, RecurringNeedExecutionResult(success=True, external_id="n1"))
    assert executed.status is S.EXECUTED and executed.execution_result == {"external_id": "n1"}
    for terminal in (executed, finalize_after_execution(unknown, RecurringNeedExecutionResult(success=False))):
        assert terminal.is_terminal()
        with pytest.raises(IllegalDraftTransition):
            terminal.with_updates(quantity=99.0)


def test_item_count_counts_the_primary_and_every_additional_item():
    draft = RecurringNeedDraft.new(
        draft_id="d", product="coq", quantity=14.0, unit="TETE", recurrence_type="WEEKLY",
        additional_items=[{"product": "chèvre", "quantity": 20.0, "unit": "TETE"}],
    )
    assert draft.item_count() == 2
    assert RecurringNeedDraft.from_dict(draft.to_dict()).item_count() == 2
