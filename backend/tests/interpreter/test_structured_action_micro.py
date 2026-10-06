"""Chantier "State Router + micro-prompts" — Incrément D (2026-09-12) :
micro-prompt STRUCTURED_ACTION.

Couvre : SELECT_PRODUCER, SELECT_PRICING_TIER, SET_PACKAGE_COUNT,
SET_QUANTITY, l'incident critique "5 L" (spec §11/§37), la règle "one
semantic action" (spec §20), la priorité DEVIATION > REJECT (réutilisant
l'apprentissage C.1), l'anti-ID (spec §15/§43), et le dispatch réel par
route. Miroir structurel de `test_selection_microprompt.py`/
`test_active_slot_micro.py` — mêmes doubles de test, mêmes conventions."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import ladini.graphs.agents.market_coach.interpreter.structured_action_micro as structured_action_micro_module
from ladini.graphs.agents.market_coach.domain.selection_actions import (
    ActionType,
    ProducerOption,
    SelectionContext,
    TierOption,
)
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from ladini.graphs.agents.market_coach.interpreter.structured_action_contract import (
    StructuredActionDecision,
    build_structured_action_prompt_context,
)
from ladini.graphs.agents.market_coach.interpreter.structured_action_prompts import (
    STRUCTURED_ACTION_PROMPT_VERSION,
    build_structured_action_user_prompt,
)
from tests.conftest import ForbiddenLLM, ScriptedLLM, StubRuntime, make_state, run


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Completion:
    def __init__(self, content: str, model: Optional[str] = None) -> None:
        self.choices = [_Choice(content)]
        self.model = model


class _SequencedLLM:
    def __init__(self, payloads: List[Any]) -> None:
        self._payloads = list(payloads)
        self.calls = 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs: Any):
        idx = min(self.calls, len(self._payloads) - 1)
        payload = self._payloads[idx]
        self.calls += 1
        content = payload if isinstance(payload, str) else json.dumps(payload)
        return _Completion(content, model=kwargs.get("model"))


PRODUCERS = [
    ProducerOption(producer_id="P1", label="Diallo — 450 FCFA/kg"),
    ProducerOption(producer_id="P2", label="Ouedraogo — 475 FCFA/kg"),
    ProducerOption(producer_id="P3", label="Sawadogo — 500 FCFA/kg"),
]

TIERS = [
    TierOption(tier_id="T5", label="5.0 L (bidon) — 450.0 FCFA"),
    TierOption(tier_id="T10", label="10.0 L (bidon) — 800.0 FCFA"),
]


def _producer_state(**overrides: Any) -> Dict[str, Any]:
    # `expected_input` n'a pas d'importance ici — le tunnel structuré est
    # dérivé de `vendor_selection_context` (state réel), simplifié ici en
    # injectant directement l'état "brut" attendu par `make_input_interpreter`.
    base: Dict[str, Any] = dict(
        normalized_text="le deuxieme",
        current_goal="BUYER_REQUEST",
        user_role="BUYER",
        message_sid=None,
        vendor_selection_context={
            "vendors": [
                {"producer_id": p.producer_id, "vendor_name": p.label.split(" — ")[0],
                 "price": 1, "unit": "KG"}
                for p in PRODUCERS
            ]
        },
    )
    base.update(overrides)
    return make_state(**base)


def _tier_state(active_tier_id: Optional[str] = None, **overrides: Any) -> Dict[str, Any]:
    tiers_raw = [
        {"tier_id": "T5", "quantity": 5.0, "unit": "L", "price": 450.0, "packaging": "bidon"},
        {"tier_id": "T10", "quantity": 10.0, "unit": "L", "price": 800.0, "packaging": "bidon"},
    ]
    base: Dict[str, Any] = dict(
        normalized_text="le premier",
        current_goal="BUYER_ADD_TO_CART",
        user_role="BUYER",
        message_sid=None,
        vendor_selection_context={"chosen_vendor": {"producer_id": "P1", "pricing_tiers": tiers_raw}},
        tier_selection_context={"tiers": tiers_raw, "resolved_tier_id": active_tier_id},
    )
    base.update(overrides)
    return make_state(**base)


def _quantity_state(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = dict(
        normalized_text="20 kg",
        current_goal="BUYER_ADD_TO_CART",
        user_role="BUYER",
        message_sid=None,
        vendor_selection_context={"chosen_vendor": {"producer_id": "P1", "pricing_tiers": None}},
    )
    base.update(overrides)
    return make_state(**base)


# =====================================================================
# A. SELECT_PRODUCER (spec §9/§35)
# =====================================================================


class TestSelectProducer:
    def test_named_producer_resolves_via_index(self):
        interp = make_input_interpreter("BUYER")
        state = _producer_state(normalized_text="celui de Koudougou (Ouedraogo)")
        llm = ScriptedLLM({"disposition": "ACTION", "action": "SELECT_PRODUCER", "selection_index": 2, "confidence": 0.9})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["extracted_entities"] == {
            "agent_action": "SELECT_PRODUCER",
            "action_offer_id": "P2#2",
            "action_producer_id": "P2",
        }
        assert result["interpreted_event"] == "SELECTION"

    def test_no_technical_id_leaves_the_microprompt(self):
        interp = make_input_interpreter("BUYER")
        # « le premier » étaye l'index 1 (un index que le texte n'étaye pas — « le deuxième » -> 1 — est refusé, voir `_selection_is_evidenced`).
        state = _producer_state(normalized_text="le premier")
        llm = ScriptedLLM({"disposition": "ACTION", "action": "SELECT_PRODUCER", "selection_index": 1, "confidence": 0.9})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["extracted_entities"]["action_producer_id"] == "P1"  # résolu par PYTHON


# =====================================================================
# B. SELECT_PRICING_TIER + incident critique "5 L" (spec §10/§11/§37)
# =====================================================================


class TestSelectPricingTierAndTheFiveLiterIncident:
    def test_bidon_de_dix_litres_resolves_to_the_correct_tier(self):
        interp = make_input_interpreter("BUYER")
        state = _tier_state(normalized_text="le bidon de dix litres")
        llm = ScriptedLLM({"disposition": "ACTION", "action": "SELECT_PRICING_TIER", "selection_index": 2, "confidence": 0.9})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["extracted_entities"] == {
            "agent_action": "SELECT_PRICING_TIER",
            "action_pricing_tier_id": "T10",
        }

    def test_the_5l_incident_produces_exactly_one_semantic_action(self):
        # "le premier, c'est-à-dire 5 L" : le "5" ne doit JAMAIS remplir
        # simultanément quantity/package_count — une SEULE sémantique
        # (sélection du palier), preuve par le contrat + l'adapter.
        interp = make_input_interpreter("BUYER")
        state = _tier_state(normalized_text="le premier, c'est-a-dire 5 L")
        llm = ScriptedLLM({"disposition": "ACTION", "action": "SELECT_PRICING_TIER", "selection_index": 1, "confidence": 0.95})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["extracted_entities"] == {
            "agent_action": "SELECT_PRICING_TIER",
            "action_pricing_tier_id": "T5",
        }
        assert "action_quantity" not in result["extracted_entities"]
        assert "action_package_count" not in result["extracted_entities"]
        assert "quantity" not in result["extracted_entities"]
        assert "package_count" not in result["extracted_entities"]

    def test_schema_rejects_a_mixed_index_and_quantity_response(self):
        # Garde Pydantic (spec §20) : même si le LLM produisait ce mélange,
        # le contrat lui-même le refuse — pas seulement l'obéissance du prompt.
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            StructuredActionDecision(
                disposition="ACTION",
                action="SELECT_PRICING_TIER",
                selection_index=1,
                quantity=5.0,
                confidence=0.9,
            )

    def test_schema_rejects_mixed_package_count_and_quantity(self):
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            StructuredActionDecision(
                disposition="ACTION",
                action="SET_PACKAGE_COUNT",
                package_count=3,
                quantity=3,
                confidence=0.9,
            )


# =====================================================================
# C. SET_PACKAGE_COUNT + changement de tier explicite (spec §12/§13/§38)
# =====================================================================


class TestSetPackageCount:
    def test_bare_number_means_package_count_never_an_index(self):
        interp = make_input_interpreter("BUYER")
        state = _tier_state(active_tier_id="T5", normalized_text="j'en veux trois")
        llm = ScriptedLLM({"disposition": "ACTION", "action": "SET_PACKAGE_COUNT", "package_count": 3, "confidence": 0.9})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["extracted_entities"] == {
            "agent_action": "SET_PACKAGE_COUNT",
            "action_package_count": 3,
        }
        assert result["interpreted_event"] == "ANSWER"

    def test_explicit_tier_change_during_package_count_is_supported(self):
        # Comportement métier existant préservé (spec §13) — `validate_action`
        # autorise déjà SELECT_PRICING_TIER pendant SET_PACKAGE_COUNT.
        interp = make_input_interpreter("BUYER")
        state = _tier_state(active_tier_id="T5", normalized_text="finalement le bidon de 10 litres")
        llm = ScriptedLLM({"disposition": "ACTION", "action": "SELECT_PRICING_TIER", "selection_index": 2, "confidence": 0.9})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["extracted_entities"] == {
            "agent_action": "SELECT_PRICING_TIER",
            "action_pricing_tier_id": "T10",
        }

    def test_tier_change_resolves_even_when_the_model_omits_the_action_field(self):
        # Incident réel D.1 (2026-09-13) : validation live a montré, de
        # façon déterministe (6 reproductions, 2 modèles Groq différents,
        # max_tokens porté à 800 sans effet), que le modèle pose bien
        # "selection_index" pour un changement de conditionnement pendant
        # SET_PACKAGE_COUNT mais OMET le champ "action" malgré l'instruction
        # explicite du prompt. Corrigé par une inférence Python
        # non-ambiguë (`_infer_missing_action`) plutôt que par une confiance
        # aveugle dans l'obéissance du prompt.
        interp = make_input_interpreter("BUYER")
        state = _tier_state(active_tier_id="T5", normalized_text="finalement le bidon de 10 litres")
        llm = ScriptedLLM({"disposition": "ACTION", "selection_index": 2, "confidence": 0.9})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["extracted_entities"] == {
            "agent_action": "SELECT_PRICING_TIER",
            "action_pricing_tier_id": "T10",
        }

    def test_package_count_still_resolves_when_the_model_omits_the_action_field(self):
        # Contrepreuve symétrique : l'inférence doit aussi fonctionner dans
        # l'autre sens (motif package_count sans action explicite).
        interp = make_input_interpreter("BUYER")
        state = _tier_state(active_tier_id="T5", normalized_text="j'en veux trois")
        llm = ScriptedLLM({"disposition": "ACTION", "package_count": 3, "confidence": 0.9})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["extracted_entities"] == {
            "agent_action": "SET_PACKAGE_COUNT",
            "action_package_count": 3,
        }

    def test_ambiguous_pattern_with_no_action_field_is_not_guessed(self):
        # Ni sélection ni package_count posés : l'inférence ne doit RIEN
        # deviner — reste invalide, correctement rejeté (repair, puis
        # UNKNOWN si le repair échoue aussi).
        interp = make_input_interpreter("BUYER")
        state = _tier_state(active_tier_id="T5", normalized_text="hein")
        llm = ScriptedLLM({"disposition": "ACTION", "confidence": 0.9})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["interpreted_event"] == "UNKNOWN"
        assert result["extracted_entities"] == {}

    def test_tier_change_resolves_when_the_model_redundantly_fills_both_index_and_label(self):
        # Incident réel D.1 bis (2026-09-13, découvert en LIVE après le
        # premier correctif) : le modèle pose bien "action" ET
        # "selection_index" cette fois, mais ajoute EN PLUS "selected_value"
        # (le label humain, en toute bonne foi) — ce qui viole la règle
        # "exactement un de selection_index/selected_value" alors que les
        # deux champs décrivent la MÊME option, sans aucun conflit. Avant
        # `_drop_redundant_selected_value`, ce motif déclenchait un repair
        # qui faisait chuter la confiance à 0.0 → UNKNOWN à tort, 0/10 en
        # validation live. Corrigé en normalisant AVANT Pydantic, jamais en
        # affaiblissant la règle "one semantic action" elle-même.
        interp = make_input_interpreter("BUYER")
        state = _tier_state(active_tier_id="T5", normalized_text="finalement le bidon de 10 L")
        llm = ScriptedLLM({
            "disposition": "ACTION", "action": "SELECT_PRICING_TIER",
            "selection_index": 2, "selected_value": "10.0 L (bidon) — 800.0 FCFA",
            "confidence": 0.99,
        })
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["extracted_entities"] == {
            "agent_action": "SELECT_PRICING_TIER",
            "action_pricing_tier_id": "T10",
        }

    def test_contradictory_index_and_label_is_not_guessed(self):
        # Contrepreuve de sécurité : si "selected_value" ne correspond PAS
        # au label de l'option désignée par "selection_index", ce n'est PAS
        # une redondance bénigne mais une vraie contradiction — ne JAMAIS
        # deviner lequel des deux champs est le bon, laisser le validator
        # Pydantic rejeter (repair, puis UNKNOWN si le repair échoue aussi).
        interp = make_input_interpreter("BUYER")
        state = _tier_state(active_tier_id="T5", normalized_text="incohérent")
        llm = ScriptedLLM({
            "disposition": "ACTION", "action": "SELECT_PRICING_TIER",
            "selection_index": 2, "selected_value": "20.0 L (bidon) — 1500.0 FCFA",
            "confidence": 0.9,
        })
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["interpreted_event"] == "UNKNOWN"
        assert result["extracted_entities"] == {}

    def test_select_producer_infers_the_only_possible_action(self):
        # Pour SELECT_PRODUCER (et SELECT_PRICING_TIER/SET_QUANTITY hors
        # SET_PACKAGE_COUNT), une seule action est possible dans ce
        # contexte — l'omission y est TOUJOURS non-ambiguë à combler.
        interp = make_input_interpreter("BUYER")
        state = _producer_state(normalized_text="le premier")
        llm = ScriptedLLM({"disposition": "ACTION", "selection_index": 1, "confidence": 0.9})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["extracted_entities"] == {
            "agent_action": "SELECT_PRODUCER",
            "action_offer_id": "P1#1",
            "action_producer_id": "P1",
        }


# =====================================================================
# D. SET_QUANTITY (spec §14/§29/§39)
# =====================================================================


class TestSetQuantity:
    def test_bare_number_means_quantity_never_an_index(self):
        interp = make_input_interpreter("BUYER")
        state = _quantity_state(normalized_text="j'en veux vingt kilos")
        llm = ScriptedLLM({"disposition": "ACTION", "quantity": 20, "unit": "KG", "action": "SET_QUANTITY", "confidence": 0.9})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["extracted_entities"] == {
            "agent_action": "SET_QUANTITY",
            "action_quantity": 20,
            "action_unit": "KG",
        }

    def test_no_unit_expressed_means_unit_is_not_invented(self):
        interp = make_input_interpreter("BUYER")
        state = _quantity_state(normalized_text="environ 15")
        llm = ScriptedLLM({"disposition": "ACTION", "action": "SET_QUANTITY", "quantity": 15, "unit": None, "confidence": 0.85})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert "action_unit" not in result["extracted_entities"]
        assert result["extracted_entities"]["action_quantity"] == 15


# =====================================================================
# E. CHIFFRES NUS SELON L'ACTION (spec §33/§34)
#
# NB : pour SELECT_PRODUCER/SELECT_PRICING_TIER/SET_PACKAGE_COUNT, un
# chiffre NU est déjà intercepté AVANT le LLM par le fast-path déterministe
# existant (`domain/selection_actions.py::fast_path_action`, inchangé par
# cet incrément) — ces tests le confirment au niveau du pipeline complet
# (`ForbiddenLLM` prouve qu'aucun appel réseau n'a lieu). Seul SET_QUANTITY
# n'a pas de fast-path pour un chiffre nu — il atteint réellement le
# micro-prompt.
# =====================================================================


class TestBareDigitsPerAction:
    def test_select_producer_bare_digit_is_deterministic_no_llm(self):
        interp = make_input_interpreter("BUYER")
        state = _producer_state(normalized_text="2")
        result = run(interp(state, StubRuntime(llm=ForbiddenLLM())))
        assert result["extracted_entities"] == {
            "agent_action": "SELECT_PRODUCER",
            "action_offer_id": "P2#2",
            "action_producer_id": "P2",
        }
        assert result["raw_analysis"]["path"] == "fast_path_selection_action"

    def test_select_pricing_tier_bare_digit_is_deterministic_no_llm(self):
        interp = make_input_interpreter("BUYER")
        state = _tier_state(normalized_text="2")
        result = run(interp(state, StubRuntime(llm=ForbiddenLLM())))
        assert result["extracted_entities"] == {
            "agent_action": "SELECT_PRICING_TIER",
            "action_pricing_tier_id": "T10",
        }

    def test_set_package_count_bare_digit_is_deterministic_no_llm(self):
        interp = make_input_interpreter("BUYER")
        state = _tier_state(active_tier_id="T5", normalized_text="2")
        result = run(interp(state, StubRuntime(llm=ForbiddenLLM())))
        assert result["extracted_entities"] == {
            "agent_action": "SET_PACKAGE_COUNT",
            "action_package_count": 2.0,
        }

    def test_set_quantity_bare_digit_reaches_the_microprompt_and_means_quantity(self):
        interp = make_input_interpreter("BUYER")
        state = _quantity_state(normalized_text="2")
        llm = ScriptedLLM({"disposition": "ACTION", "action": "SET_QUANTITY", "quantity": 2, "unit": None, "confidence": 0.9})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["extracted_entities"]["action_quantity"] == 2
        assert "action_producer_id" not in result["extracted_entities"]
        assert "action_pricing_tier_id" not in result["extracted_entities"]


# =====================================================================
# F. DEVIATION / REJECT / UNKNOWN (spec §7/§8/§29/§30/§31/§40/§41/§42)
# =====================================================================


class TestDispositionsOtherThanAction:
    def test_deviation_triggers_a_second_call_reclassified_as_new_task(self):
        llm = _SequencedLLM(
            [
                {"disposition": "DEVIATION", "confidence": 0.9},
                {
                    "disposition": "NEW_TASK",
                    "intent": "SALES_PUBLISH_PRODUCT",
                    "confidence": 0.8,
                    "entities": {"product": "riz"},
                },
            ]
        )
        interp = make_input_interpreter("BUYER")
        state = _tier_state(normalized_text="laisse ca je veux vendre du riz")
        result = run(interp(state, StubRuntime(llm=llm)))
        assert llm.calls == 2
        assert result["interpreted_event"] == "NEW_TASK"

    def test_bare_reject_does_not_trigger_a_second_call(self):
        interp = make_input_interpreter("BUYER")
        state = _tier_state(normalized_text="laisse tomber")
        llm = ScriptedLLM({"disposition": "REJECT", "confidence": 0.9})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["interpreted_event"] == "REJECT"
        assert result["extracted_entities"] == {}

    def test_reject_with_new_demand_is_deviation_not_reject(self):
        llm = _SequencedLLM(
            [
                {"disposition": "DEVIATION", "confidence": 0.9},
                {
                    "disposition": "NEW_TASK",
                    "intent": "BUYER_REQUEST",
                    "confidence": 0.8,
                    "entities": {"product": "oeufs"},
                },
            ]
        )
        interp = make_input_interpreter("BUYER")
        state = _tier_state(normalized_text="laisse tomber je veux acheter des oeufs")
        result = run(interp(state, StubRuntime(llm=llm)))
        assert llm.calls == 2
        assert result["interpreted_event"] == "NEW_TASK"

    def test_unknown_stays_unknown(self):
        # Texte non-numérique volontairement — un chiffre nu, même hors
        # bornes, retombe sur l'ancien fast-path générique de menu
        # (`Fast-path 1`, préexistant, inchangé par cet incrément), pas sur
        # ce micro-prompt.
        interp = make_input_interpreter("BUYER")
        state = _tier_state(normalized_text="hein peut-etre")
        llm = ScriptedLLM({"disposition": "UNKNOWN", "confidence": 0.2})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["interpreted_event"] == "UNKNOWN"
        assert result["extracted_entities"] == {}

    def test_low_confidence_action_is_unknown_not_a_guess(self):
        # Spec §46 : "il vaut mieux UNKNOWN qu'un mauvais producteur ou tier".
        interp = make_input_interpreter("BUYER")
        state = _tier_state(normalized_text="peut-etre le premier")
        llm = ScriptedLLM({"disposition": "ACTION", "action": "SELECT_PRICING_TIER", "selection_index": 1, "confidence": 0.2})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["interpreted_event"] == "UNKNOWN"
        assert result["extracted_entities"] == {}

    def test_out_of_range_index_is_unknown_not_an_exception(self):
        interp = make_input_interpreter("BUYER")
        state = _tier_state(normalized_text="le troisieme")
        llm = ScriptedLLM({"disposition": "ACTION", "action": "SELECT_PRICING_TIER", "selection_index": 99, "confidence": 0.9})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["interpreted_event"] == "UNKNOWN"
        assert result["extracted_entities"] == {}


# =====================================================================
# G. ANTI-ID (spec §15/§43)
# =====================================================================


class TestNoTechnicalIdEverLeavesTheMicroprompt:
    def test_a_producer_id_the_model_tries_to_emit_is_silently_ignored(self):
        interpretation = StructuredActionDecision.model_validate(
            {
                "disposition": "ACTION",
                "action": "SELECT_PRODUCER",
                "selection_index": 1,
                "confidence": 0.9,
                "producer_id": "FAKE-ID",
            }
        )
        assert not hasattr(interpretation, "producer_id")

    def test_a_pricing_tier_id_the_model_tries_to_emit_is_silently_ignored(self):
        interpretation = StructuredActionDecision.model_validate(
            {
                "disposition": "ACTION",
                "action": "SELECT_PRICING_TIER",
                "selection_index": 1,
                "confidence": 0.9,
                "pricing_tier_id": "FAKE-ID",
            }
        )
        assert not hasattr(interpretation, "pricing_tier_id")

    def test_end_to_end_a_fake_id_from_the_model_never_reaches_the_canonical_result(self):
        interp = make_input_interpreter("BUYER")
        state = _tier_state(normalized_text="le premier")
        llm = ScriptedLLM(
            {
                "disposition": "ACTION",
                "action": "SELECT_PRICING_TIER",
                "selection_index": 1,
                "pricing_tier_id": "FAKE-INJECTED-ID",
                "confidence": 0.9,
            }
        )
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["extracted_entities"]["action_pricing_tier_id"] == "T5"  # résolu par Python, jamais "FAKE-INJECTED-ID"


# =====================================================================
# H. DISPATCH RÉEL PAR ROUTE
# =====================================================================


class TestOnlyStructuredActionRouteUsesTheMicroprompt:
    def test_structured_action_route_calls_the_microprompt(self, monkeypatch):
        called = {"value": False}

        async def _spy(*args, **kwargs):
            called["value"] = True
            return structured_action_micro_module.StructuredActionOutcome.RESULT, {
                "interpreted_event": "SELECTION",
                "detected_intent": "BUYER_REQUEST",
                "interpreter_confidence": 0.9,
                "extracted_entities": {"agent_action": "SELECT_PRICING_TIER", "action_pricing_tier_id": "T5"},
                "raw_analysis": {"path": "structured_action_micro"},
            }

        monkeypatch.setattr(
            structured_action_micro_module, "run_structured_action_microprompt", _spy
        )
        interp = make_input_interpreter("BUYER")
        state = _tier_state()
        run(interp(state, StubRuntime(llm=ForbiddenLLM())))
        assert called["value"] is True

    def test_active_slot_never_calls_the_structured_action_microprompt(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("structured_action_micro ne doit pas être appelé pour ACTIVE_SLOT")

        monkeypatch.setattr(
            structured_action_micro_module, "run_structured_action_microprompt", _boom
        )
        interp = make_input_interpreter("PRODUCER")
        state = make_state(
            normalized_text="250",
            expected_input="PRICE",
            current_goal="SALES_PUBLISH_PRODUCT",
            user_role="PRODUCER",
            transaction_payload={},
        )
        llm = ScriptedLLM({"disposition": "ANSWER", "extracted_entities": {"price": 250}, "confidence": 0.9})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["interpreted_event"] == "ANSWER"


# =====================================================================
# I. PROFIL LLM DÉDIÉ (Phase B.1 réutilisé, pas de 4e profil)
# =====================================================================


class TestStructuredActionUsesTheDedicatedInterpreterProfile:
    def test_calls_the_gateway_with_the_interpreter_profile(self, monkeypatch):
        import ladini.graphs.agents.market_coach.llm_gateway as llm_gateway_module
        from ladini.graphs.agents.market_coach.llm_gateway.types import LLMProfile

        calls: List[Dict[str, Any]] = []

        class _CapturingGateway:
            def primary_model_name(self, profile):
                return "openai/gpt-oss-20b"

            async def complete(self, **kwargs: Any):
                calls.append(kwargs)
                payload = {"disposition": "ACTION", "action": "SELECT_PRICING_TIER", "selection_index": 1, "confidence": 0.9}
                return _Completion(json.dumps(payload), model="openai/gpt-oss-20b")

        gateway = _CapturingGateway()
        monkeypatch.setattr(llm_gateway_module, "resolve_gateway", lambda mc_runtime: gateway)

        interp = make_input_interpreter("BUYER")
        state = _tier_state()
        run(interp(state, StubRuntime(llm=ForbiddenLLM())))

        assert len(calls) == 1
        assert calls[0]["profile"] == LLMProfile.INTERPRETER
        assert calls[0]["profile"] != LLMProfile.FAST
        assert calls[0]["profile"] != LLMProfile.REASONING
        assert calls[0]["max_tokens"] is not None
        assert calls[0]["max_tokens"] >= 400


# =====================================================================
# J. GARDE STRUCTURELLE ANTI-CATALOGUE (spec §44)
# =====================================================================


class TestStructuredActionPromptNeverLeaksTheFullCatalog:
    def test_prompt_does_not_reference_unrelated_business_intents(self):
        context = SelectionContext(expected_action=ActionType.SELECT_PRICING_TIER, tier_options=TIERS)
        prompt_context = build_structured_action_prompt_context(context)
        prompt = build_structured_action_user_prompt(
            prompt_context=prompt_context, goal="BUYER_ADD_TO_CART", normalized_text="le premier"
        )
        for forbidden_intent in ("SALES_PUBLISH_PRODUCT", "PRODUCTION_DECLARE_FUTURE", "PROCUREMENT_CREATE_REQUEST"):
            assert forbidden_intent not in prompt

    def test_prompt_never_contains_a_real_tier_or_producer_id(self):
        context = SelectionContext(expected_action=ActionType.SELECT_PRICING_TIER, tier_options=TIERS)
        prompt_context = build_structured_action_prompt_context(context)
        prompt = build_structured_action_user_prompt(
            prompt_context=prompt_context, goal="BUYER_ADD_TO_CART", normalized_text="le premier"
        )
        assert "T5" not in prompt
        assert "T10" not in prompt

    def test_prompt_version_is_distinct_from_other_families(self):
        assert STRUCTURED_ACTION_PROMPT_VERSION == "structured_action_v4"
        assert STRUCTURED_ACTION_PROMPT_VERSION not in ("selection_v1", "active_slot_v2", "interpreter_v1")
