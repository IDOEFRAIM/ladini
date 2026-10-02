"""Étape B1 (2026-10-01) — un pending `ENTER_QUANTITY` acheteur n'absorbe jamais
une nouvelle demande d'achat.

Incident : « je veux acheter du lait » pendant la quantité d'un achat de poulets
était classé ANSWER par le micro-prompt ACTIVE_SLOT ; la fast-path acheteur
sautait `cognitive_guard`, le produit restait « poulets » et 1 UNITE était
ajoutée au panier. Garde : `active_slot_contract.buyer_slot_answer_conflict`.
"""
from __future__ import annotations

from ladini.graphs.agents.market_coach.core.policies import FastPathPolicy
from ladini.graphs.agents.market_coach.interpreter.active_slot_contract import (
    ActiveSlotContext,
    ActiveSlotDecision,
    buyer_slot_answer_conflict,
)
from ladini.graphs.agents.market_coach.interpreter.routing import make_input_interpreter
from tests.conftest import StubRuntime, run
from tests.interpreter.test_active_slot_micro import _SequencedLLM, _slot_state


def _buyer_state(text: str):
    return _slot_state(
        normalized_text=text,
        expected_input="QUANTITY",
        current_goal="BUYER_ADD_TO_CART",
        user_role="BUYER",
        transaction_payload={"product": "poulets"},
    )


def _ctx(category="QUANTITY", goal="BUYER_ADD_TO_CART", product="poulets"):
    return ActiveSlotContext(
        category=category, field_name="quantity", goal=goal, current_product=product
    )


def _dec(disposition, entities, confidence=0.9):
    return ActiveSlotDecision(
        disposition=disposition, extracted_entities=entities, confidence=confidence
    )


class TestPipelineScenario:
    def test_new_purchase_during_enter_quantity_is_not_an_answer(self):
        # LLM micro-prompt se trompe : ANSWER avec le NOUVEAU produit.
        llm = _SequencedLLM(
            [
                {
                    "disposition": "ANSWER",
                    "extracted_entities": {"product": "lait"},
                    "confidence": 0.95,
                },
                {
                    "disposition": "NEW_TASK",
                    "intent": "BUYER_REQUEST",
                    "confidence": 0.9,
                    "entities": {"product": "lait"},
                },
            ]
        )
        interp = make_input_interpreter("BUYER")
        result = run(interp(_buyer_state("je veux acheter du lait"), StubRuntime(llm=llm)))

        assert llm.calls == 2  # reclassification NEW_TASK
        assert result["interpreted_event"] == "NEW_TASK"
        assert result["detected_intent"] == "BUYER_REQUEST"
        # => la fast-path acheteur ne saute PAS la chaîne cognitive.
        state = _buyer_state("x")
        state.update(
            interpreted_event=result["interpreted_event"],
            current_goal="BUYER_ADD_TO_CART",
        )
        assert FastPathPolicy.for_buyer().should_skip_cognitive(state) is False

    def test_empty_answer_is_unknown_not_a_new_task(self):
        # « je ne sais pas » : ni ANSWER (fast-path) ni DEVIATION (NEW_TASK) —
        # UNKNOWN → recover_active_tunnel (re-pose le slot). 1 seul appel LLM.
        llm = _SequencedLLM(
            [{"disposition": "ANSWER", "extracted_entities": {}, "confidence": 0.9}]
        )
        interp = make_input_interpreter("BUYER")
        result = run(interp(_buyer_state("je ne sais pas"), StubRuntime(llm=llm)))
        assert llm.calls == 1
        assert result["interpreted_event"] == "UNKNOWN"
        assert result["extracted_entities"] == {}

    def test_new_product_with_quantity_is_never_a_quantity_for_the_old_product(self):
        llm = _SequencedLLM(
            [
                {
                    "disposition": "ANSWER",
                    "extracted_entities": {"product": "lait", "quantity": 5, "unit": "L"},
                    "confidence": 0.95,
                },
                {
                    "disposition": "NEW_TASK",
                    "intent": "BUYER_REQUEST",
                    "confidence": 0.9,
                    "entities": {"product": "lait", "quantity": 5, "unit": "L"},
                },
            ]
        )
        interp = make_input_interpreter("BUYER")
        result = run(
            interp(_buyer_state("je veux acheter 5 L de lait"), StubRuntime(llm=llm))
        )
        assert result["interpreted_event"] == "NEW_TASK"
        assert result["extracted_entities"].get("product") == "lait"
        assert result["extracted_entities"].get("quantity") == 5

    def test_same_product_explicit_stays_an_answer_with_its_quantity(self):
        # Comportement retenu : « je veux acheter 2 poulets » pendant la
        # quantité des poulets reste un ANSWER du slot (même produit, la
        # quantité est préservée, pas de duplication ni de conflit artificiel).
        # (La fast-path numérique déterministe répond sans LLM : « 2 » est typé.)
        interp = make_input_interpreter("BUYER")
        llm = _SequencedLLM(
            [
                {
                    "disposition": "ANSWER",
                    "extracted_entities": {"product": "poulets", "quantity": 2},
                    "confidence": 0.95,
                }
            ]
        )
        result = run(
            interp(_buyer_state("je veux acheter 2 poulets"), StubRuntime(llm=llm))
        )
        assert result["interpreted_event"] == "ANSWER"
        assert result["extracted_entities"]["quantity"] == 2
        assert result["extracted_entities"]["product"] == "poulets"

    def test_bare_numeric_replies_stay_valid_answers(self):
        interp = make_input_interpreter("BUYER")
        for text, ents in (
            ("2", {"quantity": 2}),
            ("2 unités", {"quantity": 2, "unit": "UNITE"}),
            ("5 kg", {"quantity": 5, "unit": "KG"}),
            ("3 sacs", {"quantity": 3, "unit": "SAC"}),
        ):
            llm = _SequencedLLM(
                [{"disposition": "ANSWER", "extracted_entities": ents, "confidence": 0.95}]
            )
            result = run(interp(_buyer_state(text), StubRuntime(llm=llm)))
            assert result["interpreted_event"] == "ANSWER", text
            assert result["extracted_entities"]["quantity"] == ents["quantity"], text


class TestGuardUnit:
    def test_different_product_is_a_switch(self):
        d = _dec("ANSWER", {"product": "lait"})
        assert buyer_slot_answer_conflict(d, _ctx()) == "product_switch"

    def test_update_with_different_product_is_a_switch(self):
        d = _dec("UPDATE", {"product": "lait", "quantity": 3})
        assert buyer_slot_answer_conflict(d, _ctx()) == "product_switch"

    def test_same_product_modulo_plural_case_article_is_not_a_switch(self):
        for said in ("poulet", "Poulets", "des poulets", "POULETS"):
            d = _dec("ANSWER", {"product": said, "quantity": 3})
            assert buyer_slot_answer_conflict(d, _ctx()) is None, said

    def test_quantity_with_same_or_no_product_is_fine(self):
        assert buyer_slot_answer_conflict(_dec("ANSWER", {"quantity": 3}), _ctx()) is None

    def test_no_value_is_a_conflict_on_value_slots(self):
        for cat in ("QUANTITY", "UNIT", "PRICE"):
            assert buyer_slot_answer_conflict(_dec("ANSWER", {}), _ctx(category=cat)) == "no_slot_value"

    def test_out_of_scope_cases_untouched(self):
        # PRODUCT slot : un produit est la réponse attendue.
        assert buyer_slot_answer_conflict(_dec("ANSWER", {"product": "lait"}), _ctx(category="PRODUCT")) is None
        # But producteur : hors périmètre B1.
        assert buyer_slot_answer_conflict(
            _dec("ANSWER", {"product": "lait"}), _ctx(goal="SALES_PUBLISH_PRODUCT")
        ) is None
        # Produit courant inconnu : pas de preuve de bascule.
        assert buyer_slot_answer_conflict(
            _dec("ANSWER", {"product": "lait"}), _ctx(product=None)
        ) is None
        # REJECT / DEVIATION / UNKNOWN : non concernés.
        assert buyer_slot_answer_conflict(_dec("REJECT", {}), _ctx()) is None


class TestQuantityProvenance:
    def test_text_states_a_quantity(self):
        from ladini.domain.quantity_unit import text_states_a_quantity as ok

        for t in ("2", "2 unités", "5 kg", "cinq têtes", "Vingt sacs", "une douzaine", "3sacs"):
            assert ok(t), t
        for t in ("je veux acheter du lait", "je ne sais pas", "beaucoup", ""):
            assert not ok(t), t

    def test_article_un_is_not_proof_in_a_non_quantitative_expression(self):
        from ladini.domain.quantity_unit import text_states_a_quantity as ok

        for t in ("je veux un peu de lait", "donne-moi un autre", "une petite quantite", "un max de lait"):
            assert not ok(t, 1.0), t
            assert not ok(t), t
        # « un/une » réellement quantitatif, valeur proposée = 1
        for t in ("je veux un poulet", "une tete de boeuf", "juste un"):
            assert ok(t, 1.0), t
        # « un » ne prouve JAMAIS une quantité différente de 1
        assert not ok("je veux un poulet", 5.0)
        # autres preuves : valeur non jugée (conversions légitimes)
        assert ok("une demi tonne", 500.0)
        assert ok("20 sacs", 20.0)

    def test_invented_quantity_without_any_number_is_not_an_answer(self):
        d = _dec("ANSWER", {"quantity": 1})
        ctx = ActiveSlotContext(
            category="QUANTITY", field_name="quantity", goal="BUYER_ADD_TO_CART",
            current_product="poulets", message_text="je veux acheter du lait",
        )
        assert buyer_slot_answer_conflict(d, ctx) == "quantity_without_textual_support"
        ok_ctx = ActiveSlotContext(
            category="QUANTITY", field_name="quantity", goal="BUYER_ADD_TO_CART",
            current_product="poulets", message_text="deux poulets",
        )
        assert buyer_slot_answer_conflict(_dec("ANSWER", {"quantity": 2}), ok_ctx) is None
        peu = ActiveSlotContext(
            category="QUANTITY", field_name="quantity", goal="BUYER_ADD_TO_CART",
            current_product="poulets", message_text="je veux un peu de lait",
        )
        assert buyer_slot_answer_conflict(_dec("ANSWER", {"quantity": 1}), peu) == "quantity_without_textual_support"
