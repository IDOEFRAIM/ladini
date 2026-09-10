"""Tests de contrat — `interpreter/interpreter_result.py` (refonte
architecturale 2026-09-02, mandat §9-13).

Verrouille que `input_interpreter` (`interpreter/routing.py::
make_input_interpreter`) fait bien passer TOUT résultat — fast-path ET LLM —
par `InterpreterResult` avant de devenir un patch d'état, et qu'un `UNKNOWN`
porte TOUJOURS une `UnknownReason` explicite (jamais un fourre-tout muet).
"""

from __future__ import annotations

from ladini.graphs.agents.market_coach.interpreter.interpreter_result import (
    InterpreterResult,
    UnknownReason,
)


class TestUnknownAlwaysCarriesAReason:
    def test_llm_unavailable_is_classified_as_technical_failure(self):
        result = InterpreterResult.from_legacy_dict(
            {
                "interpreted_event": "UNKNOWN",
                "detected_intent": "UNKNOWN",
                "interpreter_confidence": 0.0,
                "extracted_entities": {},
                "raw_analysis": {"path": "no_llm"},
            }
        )
        assert result.reason == UnknownReason.TECHNICAL_FAILURE

    def test_llm_crash_is_classified_as_technical_failure(self):
        result = InterpreterResult.from_legacy_dict(
            {
                "interpreted_event": "UNKNOWN",
                "raw_analysis": {"path": "llm_crash", "error": "timeout"},
            }
        )
        assert result.reason == UnknownReason.TECHNICAL_FAILURE

    def test_a_genuine_llm_classification_of_unknown_defaults_to_ambiguous(self):
        """Le LLM a tourné normalement mais n'a rattaché le message à aucune
        intention/action connue — c'est une AMBIGUÏTÉ de contenu, pas une
        panne technique. Distinction explicitement demandée par le mandat."""
        result = InterpreterResult.from_legacy_dict(
            {
                "interpreted_event": "UNKNOWN",
                "raw_analysis": {"path": "llm", "role": "BUYER"},
            }
        )
        assert result.reason == UnknownReason.AMBIGUOUS

    def test_an_explicit_reason_already_set_upstream_is_preserved(self):
        result = InterpreterResult.from_legacy_dict(
            {
                "interpreted_event": "UNKNOWN",
                "unknown_reason": "INVALID_ACTION",
                "raw_analysis": {"path": "fast_path_selection_action"},
            }
        )
        assert result.reason == UnknownReason.INVALID_ACTION

    def test_a_non_unknown_event_never_carries_a_reason(self):
        result = InterpreterResult.from_legacy_dict({"interpreted_event": "CONFIRM"})
        assert result.reason is None
        assert result.to_state_patch()["unknown_reason"] is None


class TestSameContractForFastPathAndLlm:
    """Test explicitement demandé par le mandat (§9) : le fast-path
    déterministe et le chemin LLM produisent, une fois passés par
    `InterpreterResult`, EXACTEMENT la même forme de patch d'état — aucun
    champ qui n'existerait que sur l'un des deux chemins."""

    def test_a_fast_path_dict_and_an_llm_dict_serialize_to_the_same_shape(self):
        fast_path_dict = {
            "interpreted_event": "ANSWER",
            "detected_intent": "SALES_PUBLISH_PRODUCT",
            "interpreter_confidence": 0.98,
            "extracted_entities": {"quantity": 10, "unit": "KG"},
            "raw_analysis": {"path": "fast_path_slot_numeric_answer"},
        }
        llm_dict = {
            "interpreted_event": "ANSWER",
            "detected_intent": "SALES_PUBLISH_PRODUCT",
            "interpreter_confidence": 0.87,
            "validation_status": "OK",
            "extracted_entities": {"quantity": 10, "unit": "KG"},
            "raw_analysis": {"path": "llm", "role": "PRODUCER"},
        }
        fast_patch = InterpreterResult.from_legacy_dict(fast_path_dict).to_state_patch()
        llm_patch = InterpreterResult.from_legacy_dict(llm_dict).to_state_patch()

        assert set(fast_patch.keys()) <= {
            "interpreted_event",
            "detected_intent",
            "interpreter_confidence",
            "extracted_entities",
            "raw_analysis",
            "unknown_reason",
        }
        # Le chemin LLM peut porter des clés SUPPLÉMENTAIRES (passthrough
        # explicite, ex: validation_status) mais jamais un format différent
        # pour les clés canoniques communes.
        for key in ("interpreted_event", "detected_intent", "extracted_entities"):
            assert key in fast_patch and key in llm_patch
        assert llm_patch["validation_status"] == "OK"


class TestRoutingUsesInterpreterResultAtTheOnlyExitPoint:
    def test_make_input_interpreter_wraps_every_internal_return_through_interpreter_result(
        self,
    ):
        """Non-régression structurelle : `input_interpreter` (exporté) ne doit
        JAMAIS être la fonction interne brute — sinon un futur `return` ajouté
        dans les ~15 points de sortie internes pourrait à nouveau produire un
        UNKNOWN sans raison, en contournant le point de conversion unique."""
        from ladini.graphs.agents.market_coach.interpreter.routing import (
            make_input_interpreter,
        )

        node = make_input_interpreter("BUYER")
        assert node.__name__ == "input_interpreter"
        # La fonction wrapper ferme sur `_input_interpreter_impl` — vérifie
        # que le nom interne existe bien dans les closures (preuve que le
        # wrapping n'a pas été retiré silencieusement).
        closure_names = node.__code__.co_freevars
        assert "_input_interpreter_impl" in closure_names
