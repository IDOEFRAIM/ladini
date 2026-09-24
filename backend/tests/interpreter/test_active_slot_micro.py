"""Chantier "State Router + micro-prompts" — Phase C (2026-09-12) :
micro-prompt ACTIVE_SLOT.

Placé dans `tests/interpreter/` (pas `tests/unit/`) pour rester cohérent
avec `test_selection_microprompt.py`/`test_state_router.py` — même famille
de tests interpréteur, même conventions/doubles de test (spec Phase C : "
Adapter cette proposition aux conventions actuelles du repo").

Couvre : réponse simple à un slot, réponse composée multi-champs (spec §9,
anti-amputation), correction d'un AUTRE slot (spec §10), déviation vers
NEW_TASK (spec §11/§12), ambiguïté, profil LLM dédié, max_tokens explicite,
garde structurelle anti-catalogue, et un test pipeline complet prouvant
qu'une déviation NE bypass PAS `cognitive_guard` comme un faux ANSWER
(spec §19, sécurité critique)."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import ladini.graphs.agents.market_coach.interpreter.active_slot_micro as active_slot_micro_module
from ladini.graphs.agents.market_coach.core.policies import FastPathPolicy
from ladini.graphs.agents.market_coach.interpreter.active_slot_contract import (
    ActiveSlotContext,
)
from ladini.graphs.agents.market_coach.interpreter.active_slot_prompts import (
    ACTIVE_SLOT_PROMPT_VERSION,
    build_active_slot_user_prompt,
)
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
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
    """Renvoie un payload différent à chaque appel — nécessaire pour tester
    repair-retry et déviation→NEW_TASK (2e appel, contrat JSON différent).
    Même utilité que dans `test_selection_microprompt.py`."""

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


def _slot_state(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = dict(
        normalized_text="tomates",
        expected_input="PRODUCT",
        current_goal="SALES_PUBLISH_PRODUCT",
        user_role="PRODUCER",
        message_sid=None,
        transaction_payload={},
    )
    base.update(overrides)
    return make_state(**base)


# =====================================================================
# A. RÉPONSE SIMPLE / COMPOSÉE (spec §9, §24)
# =====================================================================


class TestAnswerExtraction:
    def test_simple_product_answer(self):
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(normalized_text="tomates", expected_input="PRODUCT")
        llm = ScriptedLLM(
            {"disposition": "ANSWER", "extracted_entities": {"product": "tomates"}, "confidence": 0.95}
        )
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["interpreted_event"] == "ANSWER"
        assert result["detected_intent"] == "SALES_PUBLISH_PRODUCT"
        assert result["extracted_entities"] == {"product": "tomates"}

    def test_compound_answer_is_never_amputed_to_a_single_field(self):
        # Incident réel de référence (spec §9) : une réponse composée ne
        # doit JAMAIS être amputée pour ne garder que le slot attendu
        # (ici PRODUCT) — les 4 champs explicitement donnés doivent
        # ressortir intacts.
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(
            normalized_text="tomates, 500 kg a 200 FCFA/kg", expected_input="PRODUCT"
        )
        llm = ScriptedLLM(
            {
                "disposition": "ANSWER",
                "extracted_entities": {
                    "product": "tomates",
                    "quantity": 500,
                    "unit": "KG",
                    "price": 200,
                },
                "confidence": 0.92,
            }
        )
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["interpreted_event"] == "ANSWER"
        assert result["extracted_entities"] == {
            "product": "tomates",
            "quantity": 500,
            "unit": "KG",
            "price": 200,
        }

    def test_quantity_answer(self):
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(normalized_text="20 sacs", expected_input="QUANTITY")
        llm = ScriptedLLM(
            {"disposition": "ANSWER", "extracted_entities": {"quantity": 20, "unit": "SAC"}, "confidence": 0.9}
        )
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["interpreted_event"] == "ANSWER"
        assert result["extracted_entities"] == {"quantity": 20, "unit": "SAC"}

    def test_price_answer(self):
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(normalized_text="36500", expected_input="PRICE")
        llm = ScriptedLLM(
            {"disposition": "ANSWER", "extracted_entities": {"price": 36500}, "confidence": 0.9}
        )
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["interpreted_event"] == "ANSWER"
        assert result["extracted_entities"] == {"price": 36500}


# =====================================================================
# B. CORRECTION D'UN AUTRE SLOT (spec §10)
# =====================================================================


class TestUpdateOfADifferentSlot:
    def test_correcting_price_while_date_is_expected_never_invents_a_date(self):
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(
            normalized_text="le prix est plutot 36500", expected_input="DATE"
        )
        llm = ScriptedLLM(
            {"disposition": "UPDATE", "extracted_entities": {"price": 36500}, "confidence": 0.9}
        )
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["interpreted_event"] == "UPDATE"
        assert result["extracted_entities"] == {"price": 36500}
        assert "value" not in result["extracted_entities"]
        assert "date" not in result["extracted_entities"]


# =====================================================================
# C. DÉVIATION → NEW_TASK (spec §11/§12/§18)
# =====================================================================


class TestDeviationFallsThroughToNewTaskClassifier:
    def test_deviation_triggers_a_second_call_reclassified_as_new_task(self):
        llm = _SequencedLLM(
            [
                {"disposition": "DEVIATION", "extracted_entities": {}, "confidence": 0.9},
                {
                    "disposition": "NEW_TASK",
                    "intent": "BUYER_LIST_ORDERS",
                    "confidence": 0.85,
                    "entities": {},
                },
            ]
        )
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(
            normalized_text="montre-moi mes commandes", expected_input="QUANTITY"
        )
        result = run(interp(state, StubRuntime(llm=llm)))

        assert llm.calls == 2
        assert result["interpreted_event"] == "NEW_TASK"
        assert result["detected_intent"] == "BUYER_LIST_ORDERS"

    def test_deviation_never_invents_a_quantity(self):
        llm = _SequencedLLM(
            [{"disposition": "DEVIATION", "extracted_entities": {}, "confidence": 0.9}]
        )
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(normalized_text="au fait je cherche des oeufs")
        result = run(interp(state, StubRuntime(llm=llm)))
        assert "quantity" not in result.get("extracted_entities", {})

    def test_low_confidence_answer_is_treated_as_unknown_never_a_blind_second_call(self):
        # (2026-09-21, correctif UX réel — "j ai" pendant la collecte de
        # quantité) : AVANT ce correctif, une ANSWER peu sûre retombait sur
        # `ActiveSlotOutcome.DEVIATION`, déclenchant un 2e appel LLM
        # "classifier NEW_TASK" totalement AVEUGLE au slot en attente
        # (`expected_input` remis à "NONE"). Pour un fragment incomplet, ce
        # 2e appel devine dans le vide : selon ce qu'il renvoie, l'utilisateur
        # voyait tantôt la bannière de reprise cohérente, tantôt un texte de
        # clarification générique donnant l'impression que l'agent a perdu
        # le fil — même entrée, comportement non déterministe.
        #
        # Invariant de sécurité INCHANGÉ (spec §19/§20, toujours vrai) : une
        # ANSWER peu sûre ne doit JAMAIS bypasser `cognitive_guard` comme un
        # faux ANSWER — vérifié ici par `interpreted_event == "UNKNOWN"` (pas
        # "ANSWER") ET par l'absence de la quantité peu fiable dans les
        # entités extraites. Ce qui CHANGE : plus de 2e appel LLM (`llm.calls
        # == 1`), et le résultat va directement vers le chemin de reprise
        # déterministe de `cognitive_guard` (`event=="UNKNOWN" and in_tunnel`
        # -> `recover_active_tunnel`) plutôt que vers un pari NEW_TASK.
        llm = _SequencedLLM(
            [
                {
                    "disposition": "ANSWER",
                    "extracted_entities": {"quantity": 5},
                    "confidence": 0.2,
                },
            ]
        )
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(normalized_text="5 peut-etre ? pas sur", expected_input="QUANTITY")
        result = run(interp(state, StubRuntime(llm=llm)))

        assert llm.calls == 1
        assert result["interpreted_event"] == "UNKNOWN"
        assert "quantity" not in result.get("extracted_entities", {})

    def test_current_goal_is_never_changed_by_the_active_slot_microprompt(self):
        # `current_goal` reste un champ d'ÉTAT géré par `goal_planner` — le
        # patch renvoyé par `input_interpreter` (SELECTION/ACTIVE_SLOT/
        # NEW_TASK, y compris après déviation) ne doit JAMAIS porter cette
        # clé lui-même (spec §11 : "il ne doit pas... changer current_goal").
        llm = _SequencedLLM(
            [
                {"disposition": "DEVIATION", "extracted_entities": {}, "confidence": 0.9},
                {
                    "disposition": "NEW_TASK",
                    "intent": "BUYER_LIST_ORDERS",
                    "confidence": 0.85,
                    "entities": {},
                },
            ]
        )
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(normalized_text="montre-moi mes commandes")
        patch = run(interp(state, StubRuntime(llm=llm)))
        assert "current_goal" not in patch


# =====================================================================
# C.1 — FRONTIÈRE DEVIATION vs UPDATE vs REJECT (2026-09-12)
#
# Incident réel de validation live : "laisse ça je veux vendre du riz"
# (slot QUANTITY actif, product=mais déjà connu) était classé
# UPDATE{product: riz} au lieu de DEVIATION — le prompt v1 ne pesait pas le
# signal d'abandon + nouvelle demande contre la règle mécanique "corrige un
# champ déjà connu". Corrigé en `active_slot_v2` (règle de priorité +
# exemples contrastifs, jamais une liste de déclencheurs lexicaux).
#
# Ces tests prouvent le WIRING (une fois la disposition produite par le
# LLM, le pipeline la propage correctement) — la compréhension linguistique
# RÉELLE du modèle est validée séparément par le replay live (spec §7),
# jamais par un double scripté qui renvoie toujours la même réponse.
# =====================================================================


class TestDeviationVsUpdateVsRejectSemanticBoundary:
    def test_explicit_new_task_wins_over_a_same_named_field_correction(self):
        # Cas réel corrigé : "riz" réutilise le nom de champ "product" déjà
        # connu (mais=mais), mais la vraie sémantique est un abandon +
        # nouvelle vente — pas une correction de LA MÊME transaction.
        llm = _SequencedLLM(
            [
                {"disposition": "DEVIATION", "extracted_entities": {}, "confidence": 0.9},
                {
                    "disposition": "NEW_TASK",
                    "intent": "SALES_PUBLISH_PRODUCT",
                    "confidence": 0.85,
                    "entities": {"product": "riz"},
                },
            ]
        )
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(
            normalized_text="laisse ca je veux vendre du riz",
            expected_input="QUANTITY",
            transaction_payload={"product": "mais"},
        )
        result = run(interp(state, StubRuntime(llm=llm)))
        assert llm.calls == 2
        assert result["interpreted_event"] == "NEW_TASK"
        # `additional_items`/`ambiguous_groups` sont désormais TOUJOURS présentes (même `[]`) dans
        # la sortie du micro-prompt NEW_TASK — voir `interpreter/new_task_micro.py::_finalize`
        # (bug réel production 2026-09-24 : ces deux clés, sur un canal `merge_dict`, restaient
        # sinon "collantes" d'un tour à l'autre dès qu'elles étaient omises).
        assert result["extracted_entities"] == {"product": "riz", "additional_items": [], "ambiguous_groups": []}

    def test_bare_abandonment_with_nothing_else_is_reject_not_deviation(self):
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(normalized_text="laisse tomber", expected_input="PRICE")
        llm = ScriptedLLM({"disposition": "REJECT", "extracted_entities": {}, "confidence": 0.9})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["interpreted_event"] == "REJECT"
        assert result["extracted_entities"] == {}

    def test_correction_within_the_same_transaction_is_update_not_deviation(self):
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(normalized_text="non plutot 300", expected_input="PRICE")
        llm = ScriptedLLM({"disposition": "UPDATE", "extracted_entities": {"price": 300}, "confidence": 0.9})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["interpreted_event"] == "UPDATE"
        assert result["extracted_entities"] == {"price": 300}

    def test_a_different_business_topic_is_deviation(self):
        llm = _SequencedLLM(
            [
                {"disposition": "DEVIATION", "extracted_entities": {}, "confidence": 0.9},
                {
                    "disposition": "NEW_TASK",
                    "intent": "BUYER_REQUEST",
                    "confidence": 0.8,
                    "entities": {"product": "oeufs"},
                },
            ]
        )
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(normalized_text="au fait je cherche des oeufs", expected_input="PRICE")
        result = run(interp(state, StubRuntime(llm=llm)))
        assert llm.calls == 2
        assert result["interpreted_event"] == "NEW_TASK"

    def test_wanting_to_check_orders_is_deviation(self):
        llm = _SequencedLLM(
            [
                {"disposition": "DEVIATION", "extracted_entities": {}, "confidence": 0.9},
                {
                    "disposition": "NEW_TASK",
                    "intent": "BUYER_LIST_ORDERS",
                    "confidence": 0.85,
                    "entities": {},
                },
            ]
        )
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(normalized_text="je veux consulter mes commandes", expected_input="PRICE")
        result = run(interp(state, StubRuntime(llm=llm)))
        assert llm.calls == 2
        assert result["interpreted_event"] == "NEW_TASK"

    def test_bare_no_is_reject_when_the_context_justifies_it(self):
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(normalized_text="non", expected_input="PRICE")
        llm = ScriptedLLM({"disposition": "REJECT", "extracted_entities": {}, "confidence": 0.85})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["interpreted_event"] == "REJECT"

    def test_i_dont_know_is_unknown_or_a_coherent_current_disposition(self):
        # Contrat métier retenu ici (pas de comportement figé imposé) :
        # une incertitude explicite ("je ne sais pas") reste UNKNOWN — ne
        # doit JAMAIS être promue en ANSWER ni en DEVIATION.
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(normalized_text="je ne sais pas", expected_input="PRICE")
        llm = ScriptedLLM({"disposition": "UNKNOWN", "extracted_entities": {}, "confidence": 0.2})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["interpreted_event"] == "UNKNOWN"
        assert result["extracted_entities"] == {}

    def test_messy_phrasing_deviation_is_still_wired_correctly(self):
        # Robustesse linguistique (formulations "sales") — testée comme
        # preuve de WIRING seulement (le double scripté ne "comprend" rien,
        # la compréhension réelle est validée en live) : si le LLM produit
        # DEVIATION pour une formulation dégradée, le pipeline doit se
        # comporter EXACTEMENT comme pour une formulation propre.
        llm = _SequencedLLM(
            [
                {"disposition": "DEVIATION", "extracted_entities": {}, "confidence": 0.8},
                {
                    "disposition": "NEW_TASK",
                    "intent": "SALES_PUBLISH_PRODUCT",
                    "confidence": 0.7,
                    "entities": {"product": "riz"},
                },
            ]
        )
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(normalized_text="laisse sa moi je veux riz", expected_input="QUANTITY")
        result = run(interp(state, StubRuntime(llm=llm)))
        assert llm.calls == 2
        assert result["interpreted_event"] == "NEW_TASK"

    def test_prompt_still_contains_the_deviation_priority_rule(self):
        # Garde structurelle légère : la correction v2 doit rester DANS le
        # prompt (pas silencieusement retirée par une future édition) sans
        # pour autant verrouiller le texte exact (le libellé peut évoluer).
        prompt = build_active_slot_user_prompt(
            goal="SALES_PUBLISH_PRODUCT",
            category="QUANTITY",
            field_name="quantity",
            known_entities={"product": "mais"},
            normalized_text="laisse ca je veux vendre du riz",
        )
        assert "prévaut" in prompt
        assert "DEVIATION" in prompt


# =====================================================================
# D. AMBIGU (spec §26)
# =====================================================================


class TestUnknownStaysUnknown:
    def test_ambiguous_message_is_unknown(self):
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(normalized_text="peut-etre", expected_input="QUANTITY")
        llm = ScriptedLLM({"disposition": "UNKNOWN", "extracted_entities": {}, "confidence": 0.3})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["interpreted_event"] == "UNKNOWN"
        assert result["extracted_entities"] == {}


# =====================================================================
# E. DISPATCH RÉEL PAR ROUTE
# =====================================================================


class TestOnlyActiveSlotRouteUsesTheMicroprompt:
    def test_active_slot_route_calls_the_microprompt(self, monkeypatch):
        called = {"value": False}

        async def _spy(*args, **kwargs):
            called["value"] = True
            return active_slot_micro_module.ActiveSlotOutcome.RESULT, {
                "interpreted_event": "ANSWER",
                "detected_intent": "SALES_PUBLISH_PRODUCT",
                "interpreter_confidence": 0.9,
                "extracted_entities": {"product": "tomates"},
                "raw_analysis": {"path": "active_slot_micro"},
            }

        monkeypatch.setattr(active_slot_micro_module, "run_active_slot_microprompt", _spy)
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state()
        run(interp(state, StubRuntime(llm=ForbiddenLLM())))
        assert called["value"] is True

    def test_new_task_route_never_calls_the_active_slot_microprompt(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("active_slot_micro ne doit pas être appelé pour NEW_TASK")

        monkeypatch.setattr(active_slot_micro_module, "run_active_slot_microprompt", _boom)
        interp = make_input_interpreter("BUYER")
        state = make_state(
            normalized_text="je veux acheter du riz",
            expected_input="NONE",
            current_goal=None,
            user_role="BUYER",
        )
        new_task_payload = {
            "disposition": "NEW_TASK",
            "intent": "BUYER_REQUEST",
            "confidence": 0.9,
            "entities": {"product": "riz"},
        }
        result = run(interp(state, StubRuntime(llm=ScriptedLLM(new_task_payload))))
        assert result["interpreted_event"] == "NEW_TASK"

    def test_confirmation_route_never_calls_the_active_slot_microprompt(self, monkeypatch):
        # Corrige le classement de l'Incrément A : CONFIRMATION n'est plus
        # ACTIVE_SLOT (voir test_state_router.py) — vérifie ici que le
        # comportement RÉEL de l'interpréteur en tient bien compte.
        def _boom(*args, **kwargs):
            raise AssertionError("active_slot_micro ne doit pas être appelé pour CONFIRMATION")

        monkeypatch.setattr(active_slot_micro_module, "run_active_slot_microprompt", _boom)
        interp = make_input_interpreter("BUYER")
        state = make_state(
            normalized_text="oui",
            expected_input="CONFIRMATION",
            current_goal="PROCUREMENT_CREATE_REQUEST",
            user_role="BUYER",
        )
        legacy_payload = {
            "interpreted_event": "CONFIRM",
            "detected_intent": "PROCUREMENT_CREATE_REQUEST",
            "interpreter_confidence": 0.95,
            "validation_status": "VALID",
            "extracted_entities": {},
        }
        result = run(interp(state, StubRuntime(llm=ScriptedLLM(legacy_payload))))
        assert result["interpreted_event"] == "CONFIRM"


# =====================================================================
# F. PROFIL LLM DÉDIÉ (Phase B.1 réutilisé, pas de 4e profil)
# =====================================================================


class TestActiveSlotUsesTheDedicatedInterpreterProfile:
    def test_active_slot_calls_the_gateway_with_the_interpreter_profile(self, monkeypatch):
        import ladini.graphs.agents.market_coach.llm_gateway as llm_gateway_module
        from ladini.graphs.agents.market_coach.llm_gateway.types import LLMProfile

        calls: List[Dict[str, Any]] = []

        class _CapturingGateway:
            def primary_model_name(self, profile):
                return "openai/gpt-oss-20b"

            async def complete(self, **kwargs: Any):
                calls.append(kwargs)
                payload = {"disposition": "ANSWER", "extracted_entities": {"product": "tomates"}, "confidence": 0.9}
                return _Completion(json.dumps(payload), model="openai/gpt-oss-20b")

        gateway = _CapturingGateway()
        monkeypatch.setattr(llm_gateway_module, "resolve_gateway", lambda mc_runtime: gateway)

        interp = make_input_interpreter("PRODUCER")
        state = _slot_state()
        run(interp(state, StubRuntime(llm=ForbiddenLLM())))

        assert len(calls) == 1
        assert calls[0]["profile"] == LLMProfile.INTERPRETER
        assert calls[0]["profile"] != LLMProfile.FAST
        assert calls[0]["profile"] != LLMProfile.REASONING

    def test_max_tokens_is_explicit_and_large_enough(self, monkeypatch):
        import ladini.graphs.agents.market_coach.llm_gateway as llm_gateway_module

        calls: List[Dict[str, Any]] = []

        class _CapturingGateway:
            def primary_model_name(self, profile):
                return "openai/gpt-oss-20b"

            async def complete(self, **kwargs: Any):
                calls.append(kwargs)
                payload = {"disposition": "ANSWER", "extracted_entities": {"product": "tomates"}, "confidence": 0.9}
                return _Completion(json.dumps(payload), model="openai/gpt-oss-20b")

        gateway = _CapturingGateway()
        monkeypatch.setattr(llm_gateway_module, "resolve_gateway", lambda mc_runtime: gateway)

        interp = make_input_interpreter("PRODUCER")
        state = _slot_state()
        run(interp(state, StubRuntime(llm=ForbiddenLLM())))

        assert len(calls) == 1
        assert calls[0]["max_tokens"] is not None
        assert calls[0]["max_tokens"] >= 400


# =====================================================================
# G. GARDE STRUCTURELLE ANTI-CATALOGUE
# =====================================================================


class TestActiveSlotPromptNeverLeaksTheFullCatalog:
    def test_prompt_does_not_reference_unrelated_business_intents(self):
        prompt = build_active_slot_user_prompt(
            goal="SALES_PUBLISH_PRODUCT",
            category="PRICE",
            field_name="price",
            known_entities={"product": "mais"},
            normalized_text="250",
        )
        for forbidden_intent in (
            "BUYER_REQUEST",
            "PRODUCTION_DECLARE_FUTURE",
            "PROCUREMENT_CREATE_REQUEST",
        ):
            assert forbidden_intent not in prompt

    def test_prompt_family_and_version_are_distinct_from_selection_and_unified(self):
        assert ACTIVE_SLOT_PROMPT_VERSION == "active_slot_v4"
        assert ACTIVE_SLOT_PROMPT_VERSION not in ("selection_v1", "interpreter_v1")

    def test_prompt_explains_direct_quantity_is_independent_from_pricing_tiers(self):
        # (2026-09-21, incident réel récurrent) : "600 L de lait... Le bidon
        # de 5 L coûte 500 fcfa et celui de 10 L coûte 900 fcfa" perdait la
        # quantité globale — le prompt n'expliquait QUE le cas "quantity
        # dérivée d'une somme de groupes de conditionnements", jamais celui
        # d'une quantité globale énoncée DIRECTEMENT à côté de tarifs par
        # conditionnement indépendants. Verrouille la présence de la règle
        # de non-réconciliation ET de l'exemple travaillé qui reproduit
        # l'incident exact, pour qu'un futur changement de prompt ne les
        # retire pas silencieusement.
        prompt = build_active_slot_user_prompt(
            goal="SALES_PUBLISH_PRODUCT",
            category="QUANTITY",
            field_name="quantity",
            known_entities={"product": "lait"},
            normalized_text="600 L de lait",
        )
        assert "600 L de lait" in prompt
        assert "500 FCFA" in prompt and "900 FCFA" in prompt
        assert '"quantity": 600.0' in prompt
        assert "pricing_tiers" in prompt
        assert "INDÉPENDANTES" in prompt or "indépendant" in prompt.lower()


# =====================================================================
# H. PIPELINE COMPLET — interpreter → FastPathPolicy (spec §19/§25)
# =====================================================================


class TestFullPipelineSafety:
    def test_direct_quantity_with_independent_pricing_tiers_is_never_lost(self):
        # (2026-09-21, incident réel récurrent — "600 L de lait... Le bidon
        # de 5 L coûte 500 fcfa et celui de 10 L coûte 900 fcfa" pendant la
        # collecte de quantité) : ce message EXACT est en fait intercepté
        # AVANT tout appel LLM par le fast-path déterministe
        # `fast_path_deterministic_pricing_tiers` (2+ nombres près d'une
        # devise) — voir `interpreter/routing.py`. Deux bugs empilés y
        # faisaient perdre la quantité globale (600) :
        #   1. `domain/quantity_unit.py::scan_number_candidates` ne pouvait
        #      JAMAIS attacher l'unité "L" à un nombre qui la précède
        #      immédiatement (fenêtre `after` excluant structurellement le
        #      chiffre scanné) — "600 L" ressortait sans unité, "500"/"900"
        #      (les PRIX) récupéraient l'unité à tort (voir
        #      `test_scan_number_candidates_livestock_price_bug.py`).
        #   2. Même une fois ce premier bug corrigé, le fast-path
        #      `fast_path_deterministic_pricing_tiers` lui-même ne
        #      regardait jamais s'il restait une quantité globale NON
        #      ambiguë (`_qty_unit_candidates`) à côté des tarifs déjà
        #      extraits — il renvoyait `pricing_tiers` SEUL.
        # Ce test verrouille bout en bout, sur le message EXACT de
        # l'incident : ZÉRO appel LLM (déterministe, donc rapide ET fiable
        # à 100%, pas de pari sur un modèle), événement ANSWER, et quantity
        # ET pricing_tiers ressortent TOUS LES DEUX intacts — jamais l'un
        # amputé au profit de l'autre.
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(
            normalized_text=(
                "600 L de lait... Le bidon de 5 L coute 500 fcfa et celui "
                "de 10 L coute 900 fcfa"
            ),
            expected_input="QUANTITY",
        )
        result = run(interp(state, StubRuntime(llm=ForbiddenLLM())))

        assert result["raw_analysis"]["path"] == "fast_path_deterministic_pricing_tiers"
        assert result["interpreted_event"] == "ANSWER"
        entities = result["extracted_entities"]
        assert entities["quantity"] == 600
        assert len(entities["pricing_tiers"]) == 2

    def test_multi_tier_resolution_does_not_depend_on_llm_availability(self):
        """(2026-09-21) La résolution déterministe des paliers était enfouie
        sous `skip_numeric_shortcut` — un drapeau qui signifie seulement
        "un LLM est disponible sur ce runtime". Sans LLM, le même message
        retombait sur le pick "premier nombre près d'une devise" et publiait
        un prix FAUX (mesuré : 5 FCFA/L au lieu des deux paliers, quantité
        globale perdue). Une extraction 100% déterministe et complète n'a
        aucune raison de dépendre de la présence d'un LLM : c'est au
        contraire le cas où elle est la plus nécessaire."""
        from ladini.graphs.agents.market_coach.interpreter.routing import (
            _interpret_fast_path,
        )

        text = (
            "600 L de lait... Le bidon de 5 L coute 500 fcfa et celui "
            "de 10 L coute 900 fcfa"
        )
        state = _slot_state(normalized_text=text, expected_input="QUANTITY")

        for llm_available in (True, False):
            result = _interpret_fast_path(
                state,
                text,
                skip_numeric_shortcut=llm_available,
                llm_available=llm_available,
            )
            assert result is not None, f"llm_available={llm_available}"
            assert (
                result["raw_analysis"]["path"]
                == "fast_path_deterministic_pricing_tiers"
            ), f"llm_available={llm_available} -> {result['raw_analysis']}"
            entities = result["extracted_entities"]
            assert entities["quantity"] == 600
            assert entities["unit"] == "LITRE"
            assert len(entities["pricing_tiers"]) == 2
            assert "price" not in entities, (
                f"un prix scalaire inventé est réapparu : {entities!r}"
            )

    def test_an_incomplete_deterministic_tier_extraction_falls_back_to_the_llm(
        self, monkeypatch
    ):
        # (2026-09-21, refonte STRUCTURELLE) Ce test ne rejoue PAS un
        # incident déjà vu : il prouve que le DOUBLE FILET du fast-path
        # protège contre une FUTURE forme de perte, pas encore rencontrée.
        # Simule, via monkeypatch, un moteur `parse_packaging_message`
        # BOGUÉ qui "oublie" silencieusement le second palier (10 L/900)
        # ET qui se déclare pourtant complet (`unexplained_numbers` vide,
        # `ambiguous` faux) — soit exactement le mode de défaillance que le
        # moteur seul ne peut pas détecter. Le recoupement avec
        # `scan_number_candidates` (une segmentation DIFFÉRENTE du même
        # texte) doit alors rattraper l'incohérence et faire RENONCER le
        # fast-path : jamais un `pricing_tiers` tronqué publié en silence,
        # le message atteint le micro-prompt ACTIVE_SLOT.
        import ladini.graphs.agents.market_coach.interpreter.routing as routing_module
        from ladini.domain.quantity_unit import PackagingParse

        def _incomplete_parse(_text):
            return PackagingParse(
                pricing_tiers=(
                    {
                        "quantity": 5.0,
                        "unit": "L",
                        "price": 500.0,
                        "packaging": "bidon",
                    },
                ),
            )

        monkeypatch.setattr(
            routing_module, "parse_packaging_message", _incomplete_parse
        )

        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(
            normalized_text=(
                "Le bidon de 5 L coute 500 fcfa et celui de 10 L coute 900 fcfa"
            ),
            expected_input="QUANTITY",
        )
        llm = ScriptedLLM(
            {
                "disposition": "ANSWER",
                "extracted_entities": {
                    "pricing_tiers": [
                        {"quantity": 5.0, "unit": "L", "price": 500.0, "packaging": "bidon"},
                        {"quantity": 10.0, "unit": "L", "price": 900.0, "packaging": "bidon"},
                    ],
                },
                "confidence": 0.9,
            }
        )
        result = run(interp(state, StubRuntime(llm=llm)))

        # Le fast-path déterministe a renoncé — la résolution est bien
        # passée par le LLM (au moins un appel), jamais par le résultat
        # tronqué du fast-path.
        assert llm.calls >= 1, (
            "le fast-path déterministe aurait dû renoncer face à une "
            "extraction incomplète, forçant un appel LLM — aucun appel "
            "n'a eu lieu, le résultat tronqué a probablement été accepté "
            "tel quel"
        )
        assert result["raw_analysis"]["path"] != "fast_path_deterministic_pricing_tiers"
        assert len(result["extracted_entities"].get("pricing_tiers") or []) == 2

    def test_a_real_slot_answer_keeps_the_same_goal_and_extracts_entities(self):
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(normalized_text="500 kg", expected_input="QUANTITY")
        llm = ScriptedLLM(
            {"disposition": "ANSWER", "extracted_entities": {"quantity": 500, "unit": "KG"}, "confidence": 0.92}
        )
        merged = {**state, **run(interp(state, StubRuntime(llm=llm)))}
        assert merged["interpreted_event"] == "ANSWER"
        assert merged["current_goal"] == "SALES_PUBLISH_PRODUCT"
        assert merged["extracted_entities"] == {"quantity": 500, "unit": "KG"}

    def test_a_deviation_never_reaches_fastpathpolicy_as_a_bare_answer(self):
        # Sécurité critique (spec §19) : une déviation ne doit JAMAIS
        # ressortir comme un événement ANSWER — sinon FastPathPolicy (rôle
        # BUYER) bypasserait cognitive_guard, empêchant toute détection
        # d'interruption pour une vraie nouvelle tâche.
        llm = _SequencedLLM(
            [
                {"disposition": "DEVIATION", "extracted_entities": {}, "confidence": 0.9},
                {
                    "disposition": "NEW_TASK",
                    "intent": "BUYER_LIST_ORDERS",
                    "confidence": 0.85,
                    "entities": {},
                },
            ]
        )
        interp = make_input_interpreter("BUYER")
        state = _slot_state(
            normalized_text="montre-moi mes commandes",
            expected_input="QUANTITY",
            current_goal="BUYER_REQUEST",
            user_role="BUYER",
        )
        merged = {**state, **run(interp(state, StubRuntime(llm=llm)))}

        assert merged["interpreted_event"] != "ANSWER"
        assert merged["interpreted_event"] == "NEW_TASK"

        policy = FastPathPolicy.for_buyer()
        route = policy.route(merged)
        # NEW_TASK n'est jamais dans les events fast-path (ANSWER/SELECTION
        # uniquement) — la déviation est donc correctement forcée vers
        # cognitive_guard, jamais vers un bypass memory_update direct.
        assert route == "to_cognitive"

    def test_incomplete_fragment_deterministically_reaches_recovery_not_generic_clarify(self):
        # (2026-09-21, correctif UX réel) — bout en bout : interpréteur ->
        # `cognitive_guard`, pour EXACTEMENT le scénario signalé (fragment
        # incomplet "j ai" pendant la collecte de quantité en cours de
        # tunnel). AVANT ce correctif, la sortie dépendait d'un 2e appel LLM
        # aveugle — non déterministe, parfois la bonne bannière de reprise,
        # parfois un texte de clarification générique donnant l'impression
        # que l'agent avait perdu le fil. Verrouille ici que le chemin est
        # désormais TOUJOURS le même pour ce cas : un seul appel LLM, et
        # `cognitive_decision.action == "recover_active_tunnel"` (jamais
        # `CLARIFY`/`START_OR_PLAN_GOAL`).
        from ladini.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
        from tests.conftest import make_state

        llm = ScriptedLLM(
            {
                "disposition": "ANSWER",
                "extracted_entities": {},
                "confidence": 0.15,
            }
        )
        interp = make_input_interpreter("PRODUCER")
        state = _slot_state(normalized_text="j ai", expected_input="QUANTITY")
        interp_result = run(interp(state, StubRuntime(llm=llm)))

        assert llm.calls == 1  # jamais un 2e appel de classification aveugle
        assert interp_result["interpreted_event"] == "UNKNOWN"

        merged_state = make_state(**{**state, **interp_result})
        decision = run(cognitive_guard(merged_state, None))

        assert decision["cognitive_decision"]["action"] == "recover_active_tunnel"
        assert decision["response_strategy"] == "RECOVERY"
        # Le tunnel/goal en cours n'est jamais abandonné pour un 1er essai
        # flou — l'utilisateur reste dans SON flux "mise en vente", juste
        # re-sollicité sur le même champ.
        assert decision["current_goal"] == "SALES_PUBLISH_PRODUCT"
