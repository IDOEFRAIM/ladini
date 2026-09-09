"""`services/domain/slot_enrichment.py::llm_extract_quantity_unit` — audit
Bloc 2, Blocker C (2026-09-09).

Ce second appel LLM (à l'intérieur de `memory_update`, en plus de
`input_interpreter` en amont) est un `LEGACY_SLOT_RECOVERY` borné — ce
fichier verrouille ses garanties exactes :

1. Call-count : 0 appel sur le chemin nominal (quantity/unit déjà résolus),
   au plus 1 appel en recovery, 0 appel sur une sélection structurée.
2. Il ne doit JAMAIS écraser une valeur DÉJÀ explicite (bug réel trouvé et
   corrigé ici : `payload.update(...)` était inconditionnel pour
   quantity/unit, contrairement à la regex déterministe juste au-dessus qui
   était déjà fill-if-missing).
3. Il ne peut JAMAIS produire un intent/goal/action — `SlotExtractionPayload`
   n'expose que quantity/unit/product, tout le reste est ignoré par
   construction Pydantic."""
from __future__ import annotations

from typing import Any, Dict

from agriconnect.graphs.agents.market_coach.nodes.memory import memory_update
from tests.conftest import ForbiddenLLM, ScriptedLLM, StubRuntime, make_state, run


class TestNominalPathNeverCallsTheLLM:
    def test_quantity_and_unit_already_resolved_makes_zero_llm_calls(self):
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            normalized_text="je confirme",
            user_query="je confirme",
            transaction_payload={"product": "maïs", "quantity": 50, "unit": "KG"},
        )
        # ForbiddenLLM lève si le Gateway est sollicité — la simple absence
        # d'exception prouve 0 appel.
        run(memory_update(state, StubRuntime(llm=ForbiddenLLM())))

    def test_structured_selection_event_makes_zero_llm_calls(self):
        """Une réponse de sélection AG-UI (ex: choix d'un vendeur) sur un
        panier déjà entièrement résolu ne doit jamais déclencher le second
        LLM, quel que soit le texte libre associé."""
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            interpreted_event="SELECTION",
            normalized_text="2",
            user_query="2",
            extracted_entities={"selection_index": "2"},
            transaction_payload={"product": "maïs", "quantity": 50, "unit": "KG"},
        )
        run(memory_update(state, StubRuntime(llm=ForbiddenLLM())))


class TestRecoveryPathCallsTheLLMAtMostOnce:
    def test_missing_unit_triggers_exactly_one_recovery_call(self):
        llm = ScriptedLLM({"quantity": None, "unit": "SAC", "product": None})
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            # Texte sans unité extractible par la regex déterministe — sinon
            # elle résout `unit` elle-même et le repli LLM ne se déclenche
            # jamais (ce n'est pas ce que ce test veut prouver).
            normalized_text="j'en veux encore un peu plus",
            user_query="j'en veux encore un peu plus",
            transaction_payload={"product": "maïs", "quantity": 200},
        )
        run(memory_update(state, StubRuntime(llm=llm)))
        assert llm.calls == 1

    def test_dirty_product_triggers_exactly_one_recovery_call(self):
        llm = ScriptedLLM({"quantity": 200, "unit": "KG", "product": "maïs"})
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            normalized_text="200 kg de maïs",
            user_query="200 kg de maïs",
            # `product` porte un artefact de parsing connu ("kg" dedans).
            transaction_payload={"product": "200 kg", "quantity": 200, "unit": "KG"},
        )
        run(memory_update(state, StubRuntime(llm=llm)))
        assert llm.calls == 1


class TestRecoveryNeverOverwritesAnExplicitValue:
    """(Blocker C) Bug réel corrigé : avant ce correctif, ce repli écrasait
    inconditionnellement quantity/unit dès qu'il tournait — y compris le
    champ qui n'était PAS celui manquant."""

    def test_llm_cannot_override_an_already_explicit_quantity(self):
        llm = ScriptedLLM({"quantity": 999, "unit": "SAC", "product": None})
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            normalized_text="en sacs stp",
            user_query="en sacs stp",
            # quantity=500 est déjà une réponse EXPLICITE de l'utilisateur à
            # un tour précédent ; seul `unit` manque ce tour-ci.
            transaction_payload={"product": "maïs", "quantity": 500},
        )
        result = run(memory_update(state, StubRuntime(llm=llm)))
        payload = result.get("transaction_payload") or {}
        assert payload.get("quantity") == 500, (
            "la quantité déjà explicite (500) ne doit JAMAIS être remplacée "
            "par la valeur (999) renvoyée par le recovery LLM, même si ce "
            "recovery a été légitimement déclenché pour combler `unit`"
        )
        assert payload.get("unit") == "SAC"

    def test_llm_cannot_override_an_already_explicit_unit(self):
        llm = ScriptedLLM({"quantity": 77, "unit": "TONNE", "product": None})
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            normalized_text="200 ça suffira",
            user_query="200 ça suffira",
            # unit="SAC" déjà explicite ; seul `quantity` manque ce tour-ci.
            transaction_payload={"product": "maïs", "unit": "SAC"},
        )
        result = run(memory_update(state, StubRuntime(llm=llm)))
        payload = result.get("transaction_payload") or {}
        assert payload.get("unit") == "SAC"

    def test_dirty_product_is_the_only_field_allowed_to_be_overwritten(self):
        llm = ScriptedLLM({"quantity": 300, "unit": "KG", "product": "tomate"})
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            normalized_text="300 kg de tomate",
            user_query="300 kg de tomate",
            transaction_payload={"product": "300 kg", "quantity": 300, "unit": "KG"},
        )
        result = run(memory_update(state, StubRuntime(llm=llm)))
        payload = result.get("transaction_payload") or {}
        assert payload.get("product") == "tomate", (
            "un produit connu-sale (artefact de parsing) DOIT être remplacé "
            "par la ré-extraction — seul cas légitime d'écrasement"
        )


class TestNoSecondIntentClassifier:
    """Le recovery LLM ne doit jamais pouvoir produire un intent/goal/action
    — verrouillé par construction (`SlotExtractionPayload` n'a que 3 champs),
    ce test le prouve avec une sortie LLM qui essaie quand même."""

    def test_extra_fields_from_the_llm_are_silently_dropped(self):
        llm = ScriptedLLM(
            {
                "quantity": 10,
                "unit": "KG",
                "product": None,
                "intent": "SALES_PUBLISH_PRODUCT",
                "current_goal": "SOME_OTHER_GOAL",
                "detected_intent": "HIJACKED",
                "conversation_action": "INTERRUPT_ACTIVE_GOAL",
            }
        )
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            normalized_text="10 kg",
            user_query="10 kg",
            transaction_payload={"product": "maïs"},
        )
        result: Dict[str, Any] = run(memory_update(state, StubRuntime(llm=llm)))
        assert result.get("current_goal") == "BUYER_ADD_TO_CART", (
            "le goal du tour ne doit jamais être influencé par la sortie du "
            "recovery LLM"
        )
        payload = result.get("transaction_payload") or {}
        # `payload["intent"]` est un champ LÉGITIME que memory_update pose
        # lui-même (miroir de current_goal) sur CHAQUE tour — ce test vérifie
        # qu'il n'a PAS été détourné par la valeur injectée par le LLM
        # ("SALES_PUBLISH_PRODUCT"), pas que la clé est absente.
        assert payload.get("intent") != "SALES_PUBLISH_PRODUCT"
        for leaked_key in ("detected_intent", "conversation_action"):
            assert leaked_key not in payload
