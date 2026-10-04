"""Chantier "State Router + micro-prompts" — INCRÉMENT B (2026-09-12) :
micro-prompt SELECTION.

Couvre l'audit du chemin SELECTION (assertions de non-régression sur les
mécanismes réutilisés), le dispatch réel par route (spec §30), la
compréhension de formulations libres (§31), l'interruption (§32), l'UNKNOWN
(§33), les bornes d'index + repair (§34), l'anti-ID (§35), et la garde
structurelle contre une fuite du catalogue (§36).

Toutes les autres routes (NEW_TASK/ACTIVE_SLOT/STRUCTURED_ACTION) doivent
continuer à utiliser l'interpréteur unifié historique, inchangé — voir
`TestOnlySelectionRouteUsesTheMicroprompt`."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import ladini.graphs.agents.market_coach.interpreter.selection_micro as selection_micro_module
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from ladini.graphs.agents.market_coach.interpreter.selection_contract import (
    SelectionEvent,
    SelectionInterpretation,
    adapt_selection_to_canonical,
    index_within_bounds,
)
from ladini.graphs.agents.market_coach.interpreter.selection_prompts import (
    SELECTION_PROMPT_VERSION,
    build_selection_user_prompt,
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
    """Renvoie un payload DIFFÉRENT à chaque appel successif — nécessaire
    pour tester repair-retry (2 appels, 2 réponses différentes) et
    interruption→NEW_TASK (le 2e appel utilise un tout autre contrat JSON
    que le 1er). `ScriptedLLM` (conftest) renvoie toujours LE MÊME payload,
    insuffisant ici."""

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


def _selection_state(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = dict(
        normalized_text="le deuxieme",
        expected_input="SELECTION",
        expected_candidates=["Producteur Diallo — 450 FCFA/kg", "Producteur Ouedraogo — 475 FCFA/kg"],
        current_goal="BUYER_REQUEST",
        user_role="BUYER",
        message_sid=None,
    )
    base.update(overrides)
    return make_state(**base)


# =====================================================================
# A. AUDIT DE NON-RÉGRESSION : les mécanismes réutilisés se comportent
#    exactement comme documenté (spec §3/§4) — pas de deuxième source de
#    vérité créée par ce module.
# =====================================================================


class TestContractReusesExistingConventions:
    def test_selection_adapter_uses_the_same_detected_intent_convention_as_the_legacy_fast_path(self):
        # Le fast-path numérique historique (routing.py, "Fast-path 1") et le
        # bypass interactif renvoient tous deux detected_intent="UNKNOWN"
        # pour un événement SELECTION — aucun consommateur downstream
        # (`goal_planner.py`, `nodes/memory.py`) ne lit ce champ pour
        # SELECTION (confirmé par audit). Le nouvel adapter doit suivre EXACTEMENT
        # la même convention, pas en inventer une nouvelle.
        interpretation = SelectionInterpretation(event=SelectionEvent.SELECTION, selection_index=1)
        result = adapt_selection_to_canonical(interpretation)
        assert result["detected_intent"] == "UNKNOWN"
        assert result["interpreted_event"] == "SELECTION"

    def test_out_of_range_index_is_rejected_before_reaching_downstream_flows(self):
        interpretation = SelectionInterpretation(event=SelectionEvent.SELECTION, selection_index=7)
        assert index_within_bounds(interpretation, num_candidates=3) is False

    def test_in_range_index_is_accepted(self):
        interpretation = SelectionInterpretation(event=SelectionEvent.SELECTION, selection_index=3)
        assert index_within_bounds(interpretation, num_candidates=3) is True


# =====================================================================
# B. DISPATCH RÉEL PAR ROUTE (spec §30)
# =====================================================================


class TestSelectionUsesTheDedicatedInterpreterProfile:
    """Phase B.1 (2026-09-12) : SELECTION doit résoudre vers
    `LLMProfile.INTERPRETER` — PAS `LLMProfile.FAST` (partagé avec
    INPUT_NORMALIZATION/SECURITY_MODERATION/STATE_CLEANER) ni
    `LLMProfile.REASONING`. Ce test échoue si SELECTION reprend
    accidentellement un autre profil (spec §6)."""

    def test_selection_calls_the_gateway_with_the_interpreter_profile(self, monkeypatch):
        import ladini.graphs.agents.market_coach.llm_gateway as llm_gateway_module
        from ladini.graphs.agents.market_coach.llm_gateway.types import LLMProfile

        calls: List[Dict[str, Any]] = []

        class _CapturingGateway:
            def primary_model_name(self, profile):
                return "openai/gpt-oss-20b"

            async def complete(self, **kwargs: Any):
                calls.append(kwargs)
                payload = {"event": "SELECTION", "selection_index": 1, "selected_value": None}
                return _Completion(json.dumps(payload), model="openai/gpt-oss-20b")

        gateway = _CapturingGateway()
        # `selection_micro.py` fait `from ladini.graphs.agents.market_coach.
        # llm_gateway import resolve_gateway` LOCALEMENT à chaque appel — on
        # patche donc le module SOURCE (même pattern que
        # `test_interpreter_langfuse_metadata.py`), pas un attribut inexistant
        # sur `selection_micro_module`.
        monkeypatch.setattr(llm_gateway_module, "resolve_gateway", lambda mc_runtime: gateway)

        interp = make_input_interpreter("BUYER")
        state = _selection_state()
        run(interp(state, StubRuntime(llm=ForbiddenLLM())))

        assert len(calls) == 1
        assert calls[0]["profile"] == LLMProfile.INTERPRETER
        assert calls[0]["profile"] != LLMProfile.FAST
        assert calls[0]["profile"] != LLMProfile.REASONING

    def test_max_tokens_is_explicit_and_large_enough_for_a_reasoning_models_hidden_preamble(
        self, monkeypatch
    ):
        # Phase B.1, validation live (2026-09-12) : les modèles Groq
        # RÉELLEMENT disponibles pour ce profil (`openai/gpt-oss-*`) sont
        # des modèles de raisonnement qui consomment des tokens de
        # "réflexion" internes AVANT le JSON final, décomptés de
        # `max_tokens`. `max_tokens=120` (valeur initiale de la spec)
        # échouait à 100% en conditions réelles (Groq HTTP 400 "max
        # completion tokens reached before generating a valid document") —
        # ce test verrouille un plafond explicite et suffisant (jamais
        # illimité, spec §28) pour éviter une régression silencieuse.
        import ladini.graphs.agents.market_coach.llm_gateway as llm_gateway_module

        calls: List[Dict[str, Any]] = []

        class _CapturingGateway:
            def primary_model_name(self, profile):
                return "openai/gpt-oss-20b"

            async def complete(self, **kwargs: Any):
                calls.append(kwargs)
                payload = {"event": "SELECTION", "selection_index": 1, "selected_value": None}
                return _Completion(json.dumps(payload), model="openai/gpt-oss-20b")

        gateway = _CapturingGateway()
        monkeypatch.setattr(llm_gateway_module, "resolve_gateway", lambda mc_runtime: gateway)

        interp = make_input_interpreter("BUYER")
        state = _selection_state()
        run(interp(state, StubRuntime(llm=ForbiddenLLM())))

        assert len(calls) == 1
        assert calls[0]["max_tokens"] is not None
        assert calls[0]["max_tokens"] >= 400


class TestOnlySelectionRouteUsesTheMicroprompt:
    def test_selection_route_calls_the_microprompt(self, monkeypatch):
        called = {"value": False}

        async def _spy(*args, **kwargs):
            called["value"] = True
            return selection_micro_module.SelectionOutcome.RESULT, {
                "interpreted_event": "SELECTION",
                "detected_intent": "UNKNOWN",
                "interpreter_confidence": 0.95,
                "extracted_entities": {"selection_index": 1},
                "raw_analysis": {"path": "selection_microprompt"},
            }

        monkeypatch.setattr(selection_micro_module, "run_selection_microprompt", _spy)
        interp = make_input_interpreter("BUYER")
        state = _selection_state()
        run(interp(state, StubRuntime(llm=ForbiddenLLM())))
        assert called["value"] is True

    def test_active_slot_route_never_calls_the_microprompt(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("le micro-prompt SELECTION ne doit pas être appelé pour ACTIVE_SLOT")

        monkeypatch.setattr(selection_micro_module, "run_selection_microprompt", _boom)
        interp = make_input_interpreter("PRODUCER")
        state = make_state(
            normalized_text="250 francs le kilo",
            expected_input="PRICE",
            current_goal="SALES_PUBLISH_PRODUCT",
            user_role="PRODUCER",
        )
        legacy_payload = {
            "interpreted_event": "ANSWER",
            "detected_intent": "UNKNOWN",
            "interpreter_confidence": 0.9,
            "validation_status": "VALID",
            "extracted_entities": {"price": 250, "price_unit": "KG"},
        }
        result = run(interp(state, StubRuntime(llm=ScriptedLLM(legacy_payload))))
        assert result["interpreted_event"] == "ANSWER"

    def test_new_task_route_never_calls_the_microprompt(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("le micro-prompt SELECTION ne doit pas être appelé pour NEW_TASK")

        monkeypatch.setattr(selection_micro_module, "run_selection_microprompt", _boom)
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

    def test_structured_action_route_never_calls_the_microprompt(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError(
                "le micro-prompt SELECTION ne doit pas être appelé pour STRUCTURED_ACTION"
            )

        monkeypatch.setattr(selection_micro_module, "run_selection_microprompt", _boom)
        interp = make_input_interpreter("BUYER")
        state = make_state(
            normalized_text="celui de Ouedraogo",
            expected_input="SELECTION",
            current_goal="BUYER_REQUEST",
            user_role="BUYER",
        )
        # Tunnel producteur actif (STRUCTURED_ACTION) — prime sur SELECTION,
        # exactement comme le State Router de l'Incrément A (voir
        # test_state_router.py::TestStructuredActionHasTopPriority).
        state["vendor_selection_context"] = {
            "vendors": [
                {"producer_id": "p1", "vendor_name": "Diallo", "price": 250, "unit": "KG"},
                {"producer_id": "p2", "vendor_name": "Ouedraogo", "price": 240, "unit": "KG"},
            ]
        }
        result = run(interp(state, StubRuntime(llm=ForbiddenLLM())))
        # Fast-path déterministe de l'action structurée (aucun besoin de LLM
        # pour "celui de Ouedraogo" ? non — texte libre, donc pas de
        # fast-path ; mais le micro-prompt SELECTION ne doit jamais être
        # invoqué : seule l'assertion `_boom` ci-dessus le garantit si le
        # test n'a pas levé.
        assert result.get("raw_analysis", {}).get("path") != "selection_microprompt"


# =====================================================================
# C. COMPRÉHENSION DE FORMULATIONS LIBRES (spec §31) — le LLM (scripté ici)
#    reste responsable de la compréhension ; ces tests prouvent que le
#    WIRING (prompt → contrat → adapter → contrat canonique) transmet
#    fidèlement sa décision, pas que le LLM "comprend" (hors de portée d'un
#    test scripté déterministe).
# =====================================================================


class TestFreeFormSelectionIsAdaptedCorrectly:
    def _run_with(self, llm_payload: Dict[str, Any]) -> Dict[str, Any]:
        # `message_sid=None` (défaut de `_selection_state`) : ces tests
        # vérifient l'adaptation event→contrat canonique, pas le cache —
        # pas besoin de patcher Redis (voir `_patch_selection_cache` pour
        # les tests qui testent spécifiquement le cache).
        interp = make_input_interpreter("BUYER")
        state = _selection_state()
        return run(interp(state, StubRuntime(llm=ScriptedLLM(llm_payload))))

    def test_ordinal_resolves_to_an_index(self):
        result = self._run_with({"event": "SELECTION", "selection_index": 2, "selected_value": None})
        assert result["interpreted_event"] == "SELECTION"
        assert result["extracted_entities"] == {"selection_index": 2}

    def test_descriptive_choice_resolves_to_a_selected_value_when_no_index_is_certain(self):
        result = self._run_with(
            {"event": "SELECTION", "selection_index": None, "selected_value": "le moins cher"}
        )
        assert result["interpreted_event"] == "SELECTION"
        assert result["extracted_entities"] == {"selected_value": "le moins cher"}

    def test_named_producer_resolves_to_the_matching_index(self):
        result = self._run_with({"event": "SELECTION", "selection_index": 2, "selected_value": None})
        assert result["extracted_entities"]["selection_index"] == 2


# =====================================================================
# D. INTERRUPTION (spec §12/§13/§14/§18/§32) — 2e appel via NEW_TASK
# =====================================================================


class TestInterruptionFallsThroughToNewTaskClassifier:
    def test_interruption_triggers_a_second_call_reclassified_as_new_task(self):
        llm = _SequencedLLM(
            [
                {"event": "INTERRUPTION", "selection_index": None, "selected_value": None},
                {
                    "disposition": "NEW_TASK",
                    "intent": "SALES_PUBLISH_PRODUCT",
                    "confidence": 0.85,
                    "entities": {"product": "mais"},
                },
            ]
        )
        interp = make_input_interpreter("PRODUCER")
        state = _selection_state(
            normalized_text="laisse ça, je veux vendre du mais",
            current_goal="BUYER_LIST_ORDERS",
            user_role="PRODUCER",
        )
        result = run(interp(state, StubRuntime(llm=llm)))

        assert llm.calls == 2
        assert result["interpreted_event"] == "NEW_TASK"
        assert result["detected_intent"] == "SALES_PUBLISH_PRODUCT"

    def test_a_bare_action_verb_reclassifies_to_the_producer_intent_when_the_llm_cooperates(
        self,
    ):
        """Incident réel (2026-09-13, WhatsApp production, double-rôle) : un
        producteur avec le menu "mes achats" (acheteur, sa propre commande,
        UNE seule option affichée) encore actif tapait "confirmer" pour agir
        sur SES VENTES — le LLM a classé ça `event=SELECTION` (matchant la
        seule option affichée) au lieu de `INTERRUPTION`, et le tour
        re-affichait indéfiniment le même récapitulatif. Ce test ne peut pas
        forcer le VRAI modèle Groq à bien juger (non-déterminisme documenté
        ailleurs dans cette suite) — il prouve seulement que LA PLOMBERIE
        fonctionne quand le LLM suit l'instruction ajoutée au prompt
        (`selection_v2` : un verbe d'action seul, sans référence à l'option
        affichée, est TOUJOURS une interruption)."""
        llm = _SequencedLLM(
            [
                {"event": "INTERRUPTION", "selection_index": None, "selected_value": None},
                {
                    "disposition": "NEW_TASK",
                    "intent": "PRODUCER_CONFIRM_ORDER",
                    "confidence": 0.9,
                    "entities": {},
                },
            ]
        )
        interp = make_input_interpreter("PRODUCER")
        state = _selection_state(
            normalized_text="confirmer",
            current_goal="BUYER_LIST_ORDERS",
            user_role="PRODUCER",
            expected_candidates=["Commande #65280745"],
        )
        result = run(interp(state, StubRuntime(llm=llm)))

        assert llm.calls == 2
        assert result["interpreted_event"] == "NEW_TASK"
        assert result["detected_intent"] == "PRODUCER_CONFIRM_ORDER"

    def test_a_contradictory_reclassification_falls_back_to_the_legacy_interpreter(
        self,
    ):
        """Incident réel suivant (2026-09-13, WhatsApp production) : le
        modèle de repli (moins fiable que le primaire) reclassait parfois
        "confirmer" en `disposition=CONFIRM` visant `locked_goal` LUI-MÊME
        (BUYER_LIST_ORDERS) — alors que la route SELECTION venait de juger
        ce même message comme une INTERRUPTION (sans rapport avec ce but).
        Les deux jugements du LLM se contredisent : ce n'est pas un résultat
        fiable à exécuter (l'utilisateur était renvoyé indéfiniment au même
        menu). `interpreter/routing.py` doit détecter cette contradiction
        (event/intent == locked_goal juste après une interruption confirmée)
        et retenter via l'interpréteur unifié plutôt que d'agir dessus."""
        llm = _SequencedLLM(
            [
                {"event": "INTERRUPTION", "selection_index": None, "selected_value": None},
                {
                    "disposition": "CONFIRM",
                    "intent": None,
                    "confidence": 0.8,
                    "entities": {},
                },
                {
                    "interpreted_event": "NEW_TASK",
                    "detected_intent": "PRODUCER_CONFIRM_ORDER",
                    "interpreter_confidence": 0.9,
                    "validation_status": "VALID",
                    "extracted_entities": {},
                },
            ]
        )
        interp = make_input_interpreter("PRODUCER")
        state = _selection_state(
            normalized_text="confirmer",
            current_goal="BUYER_LIST_ORDERS",
            user_role="PRODUCER",
            expected_candidates=["Commande #65280745"],
        )
        result = run(interp(state, StubRuntime(llm=llm)))

        # 3 appels : SELECTION, NEW_TASK (contradictoire, écarté), legacy
        # unifié (le tie-breaker) — jamais un simple retour de la
        # contradiction elle-même.
        assert llm.calls == 3
        assert result["detected_intent"] != "BUYER_LIST_ORDERS"
        assert result["interpreted_event"] != "CONFIRM"

    def test_the_prompt_explicitly_guides_a_bare_action_verb_to_interruption(self):
        """Garde-fou du correctif lui-même : si cette clarification est
        retirée du prompt par erreur, ce test doit échouer bruyamment."""
        prompt = build_selection_user_prompt(
            current_goal="BUYER_LIST_ORDERS",
            last_agent_question="Répondez avec le numéro de votre commande",
            candidates=["Commande #65280745"],
            normalized_text="confirmer",
        )
        assert "verbe d'action seul" in prompt
        assert "TOUJOURS" in prompt

    def test_interruption_never_invents_a_business_intent_itself(self):
        # Le micro-prompt SELECTION ne doit JAMAIS classifier lui-même
        # BUYER_REQUEST/SALES_PUBLISH_PRODUCT/etc. — seul le classifier
        # NEW_TASK (2e appel) a ce droit (spec §12).
        llm = _SequencedLLM(
            [{"event": "INTERRUPTION", "selection_index": None, "selected_value": None}]
        )
        # Le 2e appel (NEW_TASK) retombe ici sur UNKNOWN faute de 2e payload
        # scripté distinct — ce test vérifie seulement que le 1er résultat
        # scripté (INTERRUPTION) n'a jamais été adapté en un intent business
        # par le micro-prompt lui-même.
        with_index_error = False
        try:
            interp = make_input_interpreter("PRODUCER")
            state = _selection_state(normalized_text="au fait je cherche des oeufs")
            result = run(interp(state, StubRuntime(llm=llm)))
        except Exception:
            with_index_error = True
        assert not with_index_error
        assert result["detected_intent"] != "BUYER_REQUEST"

    def test_an_unresolved_reclassification_flags_the_menu_render_guard(self):
        """Incident réel (2026-09-14) : quand la route SELECTION a déjà jugé
        le message sans rapport avec le menu (interruption), et que la
        reclassification NEW_TASK ne parvient PAS non plus à identifier une
        intention métier (UNKNOWN), le résultat doit porter
        `interruption_unresolved=True` — sinon `nodes/rendering/menus.py`
        génère une note d'accompagnement laissant croire à tort que le menu
        réaffiché répond au message (voir
        test_rendering_menus_feedback_common.py pour le rendu)."""
        llm = _SequencedLLM(
            [
                {"event": "INTERRUPTION", "selection_index": None, "selected_value": None},
                {"disposition": "UNKNOWN", "intent": None, "confidence": 0.0, "entities": {}},
            ]
        )
        interp = make_input_interpreter("PRODUCER")
        state = _selection_state(
            normalized_text="confirmer",
            current_goal="BUYER_LIST_ORDERS",
            user_role="PRODUCER",
            expected_candidates=["Commande #65280745"],
        )
        result = run(interp(state, StubRuntime(llm=llm)))

        assert result["interpreted_event"] == "UNKNOWN"
        assert result.get("interruption_unresolved") is True


# =====================================================================
# E. UNKNOWN (spec §14/§33)
# =====================================================================


class TestUnknownStaysUnknown:
    def test_ambiguous_message_with_no_useful_context_is_unknown(self):
        interp = make_input_interpreter("BUYER")
        state = _selection_state(normalized_text="celui-la")
        llm = ScriptedLLM({"event": "UNKNOWN", "selection_index": None, "selected_value": None})
        result = run(interp(state, StubRuntime(llm=llm)))
        assert result["interpreted_event"] == "UNKNOWN"
        assert result["extracted_entities"] == {}


# =====================================================================
# F. BORNES D'INDEX + REPAIR (spec §9/§24/§34)
# =====================================================================


class TestIndexBoundsAndRepair:
    def test_out_of_range_index_triggers_a_repair_retry_that_succeeds(self):
        llm = _SequencedLLM(
            [
                {"event": "SELECTION", "selection_index": 12, "selected_value": None},
                {"event": "SELECTION", "selection_index": 2, "selected_value": None},
            ]
        )
        interp = make_input_interpreter("BUYER")
        state = _selection_state()
        result = run(interp(state, StubRuntime(llm=llm)))
        assert llm.calls == 2
        assert result["interpreted_event"] == "SELECTION"
        assert result["extracted_entities"] == {"selection_index": 2}

    def test_repair_failure_gives_up_to_unknown_never_a_third_call(self):
        llm = _SequencedLLM(
            [
                {"event": "SELECTION", "selection_index": 99, "selected_value": None},
                {"event": "SELECTION", "selection_index": 42, "selected_value": None},
                {"event": "SELECTION", "selection_index": 2, "selected_value": None},
            ]
        )
        interp = make_input_interpreter("BUYER")
        state = _selection_state()
        result = run(interp(state, StubRuntime(llm=llm)))
        # Max 1 repair (spec §24/§31) : 2 appels au total, jamais 3 — même si
        # un 3e payload valide était disponible, il ne doit jamais être atteint.
        assert llm.calls == 2
        assert result["interpreted_event"] == "UNKNOWN"

    def test_syntactically_invalid_json_also_triggers_repair(self):
        llm = _SequencedLLM(
            [
                "ceci n'est pas du json",
                {"event": "SELECTION", "selection_index": 1, "selected_value": None},
            ]
        )
        interp = make_input_interpreter("BUYER")
        state = _selection_state()
        result = run(interp(state, StubRuntime(llm=llm)))
        assert llm.calls == 2
        assert result["interpreted_event"] == "SELECTION"


# =====================================================================
# G. ANTI-ID (spec §11/§35)
# =====================================================================


class TestNoTechnicalIdEverLeavesTheMicroprompt:
    def test_a_technical_looking_selected_value_is_never_promoted_to_a_real_id_field(self):
        llm = ScriptedLLM(
            {
                "event": "SELECTION",
                "selection_index": None,
                "selected_value": "producer_id=3428801",
            }
        )
        interp = make_input_interpreter("BUYER")
        state = _selection_state()
        result = run(interp(state, StubRuntime(llm=llm)))
        entities = result["extracted_entities"]
        # Le texte brut passe tel quel (résolu par les flows métier
        # EXISTANTS, spec §10) — mais AUCUNE clé technique n'est créée par
        # ce module : seules selection_index/selected_value sont possibles.
        assert set(entities.keys()) <= {"selection_index", "selected_value"}
        assert "producer_id" not in entities
        assert "pricing_tier_id" not in entities

    def test_schema_rejects_a_raw_id_field_if_the_model_tries_to_emit_one(self):
        # Si le LLM (en dépit du prompt) tentait de renvoyer un champ
        # producer_id/pricing_tier_id supplémentaire, Pydantic l'ignore
        # silencieusement (champ non déclaré) — il n'atteint JAMAIS le
        # contrat canonique.
        interpretation = SelectionInterpretation.model_validate(
            {
                "event": "SELECTION",
                "selection_index": 1,
                "selected_value": None,
                "producer_id": "3428801",
            }
        )
        assert not hasattr(interpretation, "producer_id")
        result = adapt_selection_to_canonical(interpretation)
        assert "producer_id" not in result["extracted_entities"]


# =====================================================================
# H. GARDE STRUCTURELLE ANTI-CATALOGUE (spec §36)
# =====================================================================


class TestSelectionPromptNeverLeaksTheFullCatalog:
    def test_prompt_does_not_reference_unrelated_business_intents(self):
        prompt = build_selection_user_prompt(
            current_goal="BUYER_LIST_ORDERS",
            last_agent_question="Quel numéro de commande ?",
            candidates=["Commande #ABC", "Commande #DEF"],
            normalized_text="le deuxieme",
        )
        for forbidden_intent in (
            "SALES_PUBLISH_PRODUCT",
            "PRODUCTION_DECLARE_FUTURE",
            "PROCUREMENT_CREATE_REQUEST",
        ):
            assert forbidden_intent not in prompt

    def test_prompt_family_and_version_are_distinct_from_the_unified_interpreter(self):
        # (2026-09-13) : v2 — clarification INTERRUPTION pour un verbe
        # d'action seul (incident réel double-rôle "confirmer"/"annuler"
        # mal classé SELECTION face à un menu à une seule option).
        assert SELECTION_PROMPT_VERSION == "selection_v3"
        assert SELECTION_PROMPT_VERSION != "interpreter_v1"


# =====================================================================
# I. LEGACY FALLBACK (spec §26) — tracé, jamais silencieux
# =====================================================================


class TestLegacyFallbackOnInfrastructureFailure:
    def test_a_crash_in_the_microprompt_falls_back_to_the_legacy_unified_interpreter(
        self, monkeypatch
    ):
        async def _boom(*args, **kwargs):
            raise RuntimeError("groq injoignable")

        monkeypatch.setattr(selection_micro_module, "run_selection_microprompt", _boom)

        legacy_payload = {
            "interpreted_event": "SELECTION",
            "detected_intent": "UNKNOWN",
            "interpreter_confidence": 0.8,
            "validation_status": "VALID",
            "extracted_entities": {"selection_index": 1},
        }
        interp = make_input_interpreter("BUYER")
        state = _selection_state(normalized_text="le premier")
        llm = ScriptedLLM(legacy_payload)
        result = run(interp(state, StubRuntime(llm=llm)))

        assert llm.calls == 1
        assert result["interpreted_event"] == "SELECTION"
        assert result["extracted_entities"] == {"selection_index": 1}

    def test_legacy_fallback_is_traced_in_the_gateway_extra_metadata(self, monkeypatch):
        # `StubRuntime`/`LegacyOverrideGateway` (test double) ne fait aucune
        # télémétrie (voir sa docstring) — on vérifie donc que
        # `legacy_fallback=True` atteint bien l'appel gateway lui-même
        # (`extra_metadata`), le même point d'injection qu'utilise le VRAI
        # `LLMGateway` pour alimenter `record_generation` en production
        # (voir `test_interpreter_langfuse_metadata.py` pour ce pattern).
        import ladini.graphs.agents.market_coach.llm_gateway as llm_gateway_module

        async def _boom(*args, **kwargs):
            raise RuntimeError("groq injoignable")

        monkeypatch.setattr(selection_micro_module, "run_selection_microprompt", _boom)

        calls: List[Dict[str, Any]] = []

        class _CapturingGateway:
            def primary_model_name(self, profile):
                return "test-model-x"

            async def complete(self, **kwargs: Any):
                calls.append(kwargs)
                legacy_payload = {
                    "interpreted_event": "SELECTION",
                    "detected_intent": "UNKNOWN",
                    "interpreter_confidence": 0.8,
                    "validation_status": "VALID",
                    "extracted_entities": {"selection_index": 1},
                }
                return _Completion(json.dumps(legacy_payload), model="test-model-x")

        gateway = _CapturingGateway()
        monkeypatch.setattr(llm_gateway_module, "resolve_gateway", lambda mc_runtime: gateway)
        monkeypatch.setattr(llm_gateway_module, "resolve_profile", lambda mc_runtime: "REASONING")

        interp = make_input_interpreter("BUYER")
        state = _selection_state(normalized_text="le premier")
        run(interp(state, StubRuntime(llm=ForbiddenLLM())))

        assert len(calls) == 1
        assert calls[0]["extra_metadata"]["legacy_fallback"] is True


# =====================================================================
# J. CACHE / IDEMPOTENCE (spec §29/§56) — même primitives que l'interpréteur
#    unifié, espace de noms distinct.
# =====================================================================


class _FakeValueStore:
    """Même rôle que `test_interpreter_retry_safety.py::_FakeRedisValueStore`
    — un dict en mémoire remplaçant Redis pour la durée d'un test, JAMAIS
    une dépendance à un vrai serveur (indisponible dans cet environnement de
    test, voir sa docstring pour le contexte)."""

    def __init__(self) -> None:
        self._data: Dict[str, str] = {}

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value, *, ttl_seconds: int = 3600) -> None:
        if key:
            self._data[key] = value

    def increment(self, key, *, ttl_seconds: int = 3600):
        return None


def _patch_selection_cache(monkeypatch) -> _FakeValueStore:
    store = _FakeValueStore()
    monkeypatch.setattr(selection_micro_module, "get_cached", store.get)
    monkeypatch.setattr(selection_micro_module, "set_cached", store.set)
    monkeypatch.setattr(selection_micro_module, "increment", store.increment)
    return store


class TestSelectionCacheReusesExistingIdempotencyPrimitives:
    def test_a_repeated_call_with_the_same_message_sid_does_not_call_the_llm_twice(
        self, monkeypatch
    ):
        _patch_selection_cache(monkeypatch)
        interp = make_input_interpreter("BUYER")
        state = _selection_state(message_sid="SM_CACHE_SELECTION")
        llm = ScriptedLLM({"event": "SELECTION", "selection_index": 2, "selected_value": None})
        runtime = StubRuntime(llm=llm)

        first = run(interp(dict(state), runtime))
        second = run(interp(dict(state), runtime))

        assert llm.calls == 1
        assert first["extracted_entities"] == second["extracted_entities"] == {
            "selection_index": 2
        }

    def test_no_message_sid_means_no_cache_but_still_works(self, monkeypatch):
        _patch_selection_cache(monkeypatch)
        interp = make_input_interpreter("BUYER")
        state = _selection_state(message_sid=None)
        llm = ScriptedLLM({"event": "SELECTION", "selection_index": 1, "selected_value": None})
        runtime = StubRuntime(llm=llm)

        run(interp(dict(state), runtime))
        run(interp(dict(state), runtime))

        assert llm.calls == 2
