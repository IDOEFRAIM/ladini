"""B22 — marqueur « confirmation déjà montrée » du rendu CONFIRMATION d'un besoin récurrent (logique pure)."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
    RecurringNeedDraft,
    RecurringNeedDraftStatus,
)
from ladini.graphs.agents.market_coach.nodes.rendering.confirm import (
    RECURRING_CONFIRMATION_MARKER as KEY,
)
from ladini.graphs.agents.market_coach.nodes.rendering.confirm import (
    _recurring_confirmation_marker as decide,
)


def _draft(version: int = 2, status: RecurringNeedDraftStatus = RecurringNeedDraftStatus.DRAFT) -> dict:
    d = RecurringNeedDraft.new("d1", product="boeuf", quantity=2.0, unit="TETE", recurrence_type="WEEKLY")
    return {**d.to_dict(), "version": version, "status": status.value}


def _state(draft: dict, sid: str | None, marker: dict | None = None) -> dict:
    return {"recurring_need_draft": draft, "message_sid": sid, "working_memory": {KEY: marker} if marker else {}}


def test_first_emission_is_recorded_not_deduped():
    shown, marker = decide(_state(_draft(), "m1"), "CREATE_RECURRING_NEED")
    assert shown is False and marker == {"draft_id": "d1", "version": 2, "message_sid": "m1"}


def test_a_different_message_for_the_same_draft_version_is_deduped():
    prior = {"draft_id": "d1", "version": 2, "message_sid": "m1"}
    shown, marker = decide(_state(_draft(), "m2", prior), "CREATE_RECURRING_NEED")
    assert shown is True and marker == prior


def test_a_retry_of_the_same_message_reemits():
    prior = {"draft_id": "d1", "version": 2, "message_sid": "m1"}
    shown, _ = decide(_state(_draft(), "m1", prior), "CREATE_RECURRING_NEED")
    assert shown is False


def test_a_new_draft_version_is_a_new_confirmation():
    prior = {"draft_id": "d1", "version": 2, "message_sid": "m1"}
    shown, marker = decide(_state(_draft(version=3), "m2", prior), "CREATE_RECURRING_NEED")
    assert shown is False and marker["version"] == 3


def test_webchat_messages_without_identity_are_never_treated_as_retries():
    prior = {"draft_id": "d1", "version": 2, "message_sid": None}
    shown, _ = decide(_state(_draft(), None, prior), "CREATE_RECURRING_NEED")
    assert shown is True


def test_other_goals_and_non_editable_drafts_are_untouched():
    assert decide(_state(_draft(), "m1"), "BUYER_PREORDER_INIT") == (False, None)
    assert decide(_state(_draft(status=RecurringNeedDraftStatus.EXECUTION_UNKNOWN), "m1"), "CREATE_RECURRING_NEED") == (False, None)
    assert decide({"message_sid": "m1"}, "CREATE_RECURRING_NEED") == (False, None)
