"""Politique de correction d'un besoin récurrent en cours (Phase 2, décisions C/D)."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
    CorrectionNeedsScope,
    RecurringNeedDraft,
    UpdateRecurringNeedDraft,
    apply_domain_action,
    plan_correction,
    resolve_domain_action,
)


def _single() -> RecurringNeedDraft:
    return RecurringNeedDraft.new(draft_id="d", product="coq", quantity=14.0, unit="TETE", recurrence_type="WEEKLY")


def _multi() -> RecurringNeedDraft:
    return RecurringNeedDraft.new(
        draft_id="d", product="coq", quantity=14.0, unit="TETE", recurrence_type="WEEKLY",
        additional_items=[{"product": "chèvre", "quantity": 20.0, "unit": "TETE"}],
    )


def _apply(draft, action):
    return apply_domain_action(draft, action).draft


def test_single_item_correction_replaces_the_item_and_keeps_shared_parameters():
    corrected = _apply(_single(), plan_correction(_single(), {"product": "boeuf", "quantity": 23.0}))
    assert (corrected.product, corrected.quantity, corrected.unit) == ("boeuf", 23.0, "TETE")
    assert corrected.recurrence_type == "WEEKLY" and corrected.draft_id == "d"
    assert corrected.version == _single().version + 1


def test_a_new_product_without_a_stated_unit_gets_its_own_default_unit():
    corrected = _apply(_single(), plan_correction(_single(), {"product": "tomates", "quantity": 20.0}))
    assert corrected.unit == "KG", "l'unité TETE du coq n'a plus de sens pour des tomates"


def test_a_stated_unit_always_wins():
    corrected = _apply(_single(), plan_correction(_single(), {"product": "tomate", "quantity": 2.0, "unit": "SAC"}))
    assert corrected.unit == "SAC"


def test_multi_item_correction_naming_an_item_changes_only_that_item():
    action = plan_correction(_multi(), {"product": "chèvres", "quantity": 23.0})
    corrected = _apply(_multi(), action)
    assert (corrected.product, corrected.quantity) == ("coq", 14.0)
    assert corrected.additional_items == [{"product": "chèvre", "quantity": 23.0, "unit": "TETE"}]


def test_multi_item_correction_naming_the_primary_changes_the_primary():
    corrected = _apply(_multi(), plan_correction(_multi(), {"product": "coqs", "quantity": 30.0}))
    assert corrected.quantity == 30.0 and corrected.additional_items[0]["quantity"] == 20.0


def test_an_explicit_replace_all_scope_replaces_every_item():
    corrected = _apply(_multi(), plan_correction(_multi(), {"product": "boeuf", "quantity": 23.0}, scope="ALL"))
    assert (corrected.product, corrected.quantity, corrected.unit) == ("boeuf", 23.0, "TETE")
    assert not corrected.additional_items
    assert corrected.recurrence_type == "WEEKLY"


def test_an_ambiguous_multi_item_correction_asks_instead_of_guessing():
    for fields in ({"product": "boeuf", "quantity": 23.0}, {"quantity": 30.0}, {"product": "chèvre"}):
        action = plan_correction(_multi(), fields)
        assert isinstance(action, CorrectionNeedsScope), fields
        assert set(action.candidates) == {"coq", "chèvre"}
        draft = _multi()
        assert apply_domain_action(draft, action).draft is draft, "le draft n'est jamais deviné"


def test_shared_parameters_apply_to_every_item():
    action = plan_correction(_multi(), {"recurrence_type": "DAILY"})
    assert isinstance(action, UpdateRecurringNeedDraft)
    corrected = _apply(_multi(), action)
    assert corrected.recurrence_type == "DAILY" and corrected.item_count() == 2


def test_a_restated_multi_product_request_replaces_the_item_set():
    fields = {"product": "tomate", "quantity": 10.0, "unit": "KG",
              "additional_items": [{"product": "oignon", "quantity": 20.0, "unit": "KG"}]}
    corrected = _apply(_multi(), plan_correction(_multi(), fields))
    assert corrected.product == "tomate"
    assert corrected.additional_items == [{"product": "oignon", "quantity": 20.0, "unit": "KG"}]


def test_a_bare_reject_cancels_and_a_reject_with_values_is_a_correction():
    from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
        CancelRecurringNeedDraft,
    )

    assert isinstance(resolve_domain_action(interpreted_event="REJECT", extracted_entities={}, pending_target=None),
                      CancelRecurringNeedDraft)
    action = resolve_domain_action(interpreted_event="REJECT", extracted_entities={"quantity": 20.0}, pending_target=None)
    assert isinstance(action, UpdateRecurringNeedDraft) and action.fields == {"quantity": 20.0}
