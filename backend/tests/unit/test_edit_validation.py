"""Validation d'édition structurée : le modèle n'est jamais l'autorité (quantité ≠ prix, ancienne valeur -> champ, ambiguïté -> clarifier)."""
from __future__ import annotations

import json

import pytest

from ladini.domain.edit_validation import (
    EditVerdict,
    ValueCue,
    cue_for_value,
    extract_old_new_values,
    message_carries_value,
    normalize_edit_entities,
    resolve_edit_field_from_state,
    validate_structured_edit,
)
from ladini.graphs.agents.market_coach.interpreter.new_task_micro import (  # noqa: E402
    _parse_and_validate,
)

FACTS = {"quantity": 300.0, "price": 250.0}


@pytest.mark.parametrize(
    "text,value,cue",
    [
        ("non 250 kg", 250, ValueCue.MEASURE),
        ("finalement 8 litres", 8, ValueCue.MEASURE),
        ("mets 5 sachets", 5, ValueCue.MEASURE),
        ("plutôt 280/kg", 280, ValueCue.MONEY),
        ("à 350 francs", 350, ValueCue.MONEY),
        ("600 fcfa le sachet", 600, ValueCue.MONEY),
        ("ah non c 350", 350, ValueCue.BARE),
    ],
)
def test_the_cue_of_a_number_comes_from_the_domain_unit_and_currency_primitives(text, value, cue):
    assert cue_for_value(text, value)[0] is cue


def test_a_measure_read_as_price_is_reclassified_to_quantity():
    check = validate_structured_edit(field="price", value=250, unit="KG", text="non 250 kg")
    assert check.verdict is EditVerdict.RECLASSIFIED and check.field == "quantity" and check.unit == "KG"


def test_money_read_as_quantity_is_reclassified_to_price():
    check = validate_structured_edit(field="quantity", value=280, unit=None, text="plutôt 280/kg")
    assert check.verdict is EditVerdict.RECLASSIFIED and check.field == "price"


def test_a_bare_number_is_not_judged_by_this_module():
    assert validate_structured_edit(field="price", value=350, unit=None, text="ah non c 350").verdict is EditVerdict.OK
    assert validate_structured_edit(field="quantity", value=350, unit=None, text="350").verdict is EditVerdict.OK


def test_a_quantity_with_a_money_unit_is_refused():
    assert validate_structured_edit(field="quantity", value=5, unit="FCFA", text="5 fcfa").verdict is EditVerdict.CLARIFY


@pytest.mark.parametrize(
    "text,expected",
    [
        ("c'est pas 300 c'est 250", (300.0, 250.0)),
        ("pas 10, 15", (10.0, 15.0)),
        ("jai dit 500 kg pas 300", (300.0, 500.0)),
        ("non 250 kg", None),  # pas d'ancienne valeur nommée
        ("pas 300", None),
    ],
)
def test_old_and_new_values_are_extracted_from_a_rejection_followed_by_a_value(text, expected):
    assert extract_old_new_values(text) == expected


def test_the_old_value_designates_the_field_in_the_current_state():
    assert resolve_edit_field_from_state(old_value=300, facts=FACTS).field == "quantity"
    assert resolve_edit_field_from_state(old_value=250, facts=FACTS).field == "price"


def test_an_old_value_shared_by_two_fields_is_ambiguous_never_guessed():
    res = resolve_edit_field_from_state(old_value=500, facts={"quantity": 500.0, "price": 500.0})
    assert res.field is None and res.ambiguous and set(res.candidates) == {"quantity", "price"}


def test_the_money_or_measure_cue_of_the_old_value_breaks_the_tie():
    facts = {"quantity": 500.0, "price": 500.0}
    assert resolve_edit_field_from_state(old_value=500, old_cue=ValueCue.MEASURE, facts=facts).field == "quantity"
    assert resolve_edit_field_from_state(old_value=500, old_cue=ValueCue.MONEY, facts=facts).field == "price"


def test_not_300_but_250_edits_the_quantity_even_when_the_model_proposed_the_price():
    out = normalize_edit_entities({"price": 250.0, "is_correction": True}, text="c'est pas 300 c'est 250", facts=FACTS)
    assert out.clarify is None and out.entities.get("quantity") == 250.0 and "price" not in out.entities and out.entities["is_correction"] is True


def test_an_ambiguous_old_value_asks_instead_of_choosing():
    out = normalize_edit_entities({"price": 300.0}, text="non pas 500, 300", facts={"quantity": 500.0, "price": 500.0})
    assert out.clarify and out.entities == {"price": 300.0}


def test_normalization_never_invents_a_field_for_an_unrelated_correction():
    out = normalize_edit_entities({"product": "oignon", "is_correction": True}, text="en fait c'est oignon pas tomate", facts=FACTS)
    assert out.entities == {"product": "oignon", "is_correction": True} and out.clarify is None


def test_non_250_kg_read_as_price_by_the_model_is_repaired_to_a_quantity():
    out = normalize_edit_entities({"price": 250.0, "price_unit": "KG", "is_correction": True}, text="non 250 kg", facts=FACTS)
    assert out.entities.get("quantity") == 250.0 and out.entities.get("unit") == "KG" and "price" not in out.entities


def test_conflicting_cues_clarify():
    out = normalize_edit_entities({"price": 250.0, "quantity": 300.0}, text="non 250 kg", facts=FACTS)
    assert out.clarify


def test_a_bare_number_with_two_candidate_fields_and_no_named_field_is_clarified():
    out = normalize_edit_entities({"price": 280.0, "is_correction": True}, text="non 280", facts=FACTS)
    assert out.clarify and out.entities == {"price": 280.0, "is_correction": True}


def test_a_named_field_or_a_cue_removes_the_ambiguity():
    named = normalize_edit_entities({"price": 350.0}, text="ah non c 350 le prix", facts=FACTS, named_fields={"price"})
    assert named.clarify is None and named.entities == {"price": 350.0}
    cued = normalize_edit_entities({"price": 280.0}, text="plutôt 280/kg", facts=FACTS)
    assert cued.clarify is None


def test_a_single_candidate_field_needs_no_clarification():
    out = normalize_edit_entities({"quantity": 250.0}, text="non 250", facts={"quantity": 300.0})
    assert out.clarify is None


# ── un accord / refus PUR ne contient jamais de valeur ──────────────────────────────────────────────────────────────────

_INTENTS = frozenset({"BUYER_EDIT_CART", "SALES_PUBLISH_PRODUCT"})


def _raw(**kw):
    return json.dumps({"confidence": 0.95, "candidate_goals": [], "entities": {}, **kw})


def test_a_pure_agreement_carries_no_value():
    assert not message_carries_value("oui vas-y")
    assert message_carries_value("vas-y mais plutôt 20")


def test_confirm_with_a_value_is_rejected_so_the_old_confirmation_never_executes():
    decision, reason = _parse_and_validate(_raw(disposition="CONFIRM"), _INTENTS, "vas-y mais plutôt 20", pending_recap=True)
    assert decision is None and "CORRIGE" in reason


def test_reject_with_a_value_is_also_not_a_refusal():
    decision, _ = _parse_and_validate(_raw(disposition="REJECT"), _INTENTS, "non 5", pending_recap=True)
    assert decision is None


def test_a_plain_confirm_still_passes():
    decision, _ = _parse_and_validate(_raw(disposition="CONFIRM"), _INTENTS, "oui vas-y")
    assert decision is not None and decision.disposition.value == "CONFIRM"


def test_a_confirm_that_carries_a_cart_edit_is_read_as_the_edit_the_structure_says():
    decision, _ = _parse_and_validate(
        _raw(disposition="CONFIRM", entities={"cart_edit": {"field": "REMOVE", "product": "lait"}}), _INTENTS, "retire le lait"
    )
    assert decision is not None and decision.disposition.value == "NEW_TASK" and decision.intent == "BUYER_EDIT_CART"


def test_an_acceptance_that_restates_a_value_is_fine_outside_a_cart_or_draft_recap():
    """« je prends les 250 kg » accepte une proposition (écran récurrent) : le nombre répète ce qui est accepté, il ne corrige rien."""
    decision, _ = _parse_and_validate(_raw(disposition="CONFIRM"), _INTENTS, "je prends les 250 kg")
    assert decision is not None and decision.disposition.value == "CONFIRM"


def test_a_number_that_is_not_in_the_text_never_triggers_the_bare_number_clarification():
    out = normalize_edit_entities({"price": 275.0}, text="", facts=FACTS)
    assert out.clarify is None and out.entities == {"price": 275.0}
