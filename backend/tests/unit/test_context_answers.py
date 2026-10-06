"""Questions de contexte : réponses CALCULÉES (jamais inventées), « Je n'ai pas cette information » quand le fait n'existe pas, aucune mutation."""
from __future__ import annotations

import pytest

from ladini.graphs.agents.market_coach.domain.context_answers import (
    UNKNOWN_FACT,
    ContextQuestion,
    QuestionTopic,
    answer_question,
)
from ladini.graphs.agents.market_coach.domain.selection_reference import (
    Criterion,
    ReferenceType,
    SelectionReference,
    options_from_vendors,
)

VENDORS = [
    {"display_index": 1, "offer_id": "o1", "vendor_name": "Ferme Sawadogo", "zone": "Bobo-Dioulasso", "price": 700, "unit": "LITRE", "available_qty": 300},
    {"display_index": 2, "offer_id": "o2", "vendor_name": "Moussa", "zone": "Ouagadougou", "price": 450, "unit": "LITRE", "available_qty": 300},
    {"display_index": 3, "offer_id": "o3", "vendor_name": "Gilbert-prod", "zone": "Ouagadougou", "price": 500, "unit": "LITRE", "available_qty": 1200},
]
OPTS = options_from_vendors(VENDORS)


def q(topic, **kw):
    return ContextQuestion(topic=topic, **kw)


@pytest.mark.parametrize("topic", [QuestionTopic.DELIVERY, QuestionTopic.CERTIFICATION])
def test_facts_the_system_does_not_have_are_never_invented(topic):
    out = answer_question(q(topic), OPTS, chosen_id="o2")
    assert UNKNOWN_FACT in out and "Moussa" in out
    assert "oui" not in out.lower().split() and "non" not in out.lower().split()


def test_total_is_computed_from_the_cart_not_guessed():
    cart = [{"line_total": 4500.0}, {"line_total": 1000.0}]
    assert "5500 FCFA (2 articles)" in answer_question(q(QuestionTopic.TOTAL), OPTS, cart=cart)
    assert "vide" in answer_question(q(QuestionTopic.TOTAL), OPTS, cart=[])


def test_stock_of_the_chosen_vendor_and_staleness_is_disclosed():
    assert "300" in answer_question(q(QuestionTopic.STOCK), OPTS, chosen_id="o2", created_at=1000.0, now=1005.0)
    stale = answer_question(q(QuestionTopic.STOCK), OPTS, chosen_id="o2", created_at=1000.0, now=5000.0)
    assert "300" in stale and "a pu changer" in stale, "un stock ancien n'est pas présenté comme actuel"


def test_stock_of_a_named_target_overrides_the_chosen_vendor():
    target = SelectionReference(reference_type=ReferenceType.ATTRIBUTE, producer_name="Gilbert")
    assert "1200" in answer_question(q(QuestionTopic.STOCK, target=target), OPTS, chosen_id="o2")


def test_objective_comparison_is_calculated_in_python():
    assert "Moussa est le moins cher" in answer_question(q(QuestionTopic.COMPARISON, criterion=Criterion.CHEAPEST), OPTS)
    assert "Gilbert-prod est celui qui a le plus de stock" in answer_question(q(QuestionTopic.COMPARISON, criterion=Criterion.HIGHEST_AVAILABILITY), OPTS)


def test_a_comparison_without_objective_criterion_gives_facts_and_asks_never_a_global_ranking():
    out = answer_question(q(QuestionTopic.COMPARISON, names=["Gilbert", "Moussa"]), OPTS)
    assert "Gilbert-prod" in out and "Moussa" in out and "Ferme Sawadogo" not in out
    assert "Moussa est moins cher" in out and "Gilbert-prod a plus de stock" in out and out.endswith("Tu préfères quoi ?")
    assert "meilleur" not in out.lower()


def test_closest_is_never_computed_from_a_region_alone():
    out = answer_question(q(QuestionTopic.DISTANCE), OPTS)
    assert "pas la distance" in out


def test_a_null_names_list_from_a_real_model_is_accepted():
    assert ContextQuestion.model_validate({"topic": "STOCK", "names": None}).names == []
