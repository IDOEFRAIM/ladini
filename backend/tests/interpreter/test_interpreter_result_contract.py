"""`InterpreterResult` — contrat minimal (clôture Bloc 1, mandat §9).

Le contrat exige : `interpreted_event`, `detected_intent`,
`interpreter_confidence`, `extracted_entities`, `unknown_reason`,
`interpretation_source`.

Décision (audit, pas de nouveau champ inventé) : `interpretation_source`
N'EST PAS un nouveau nom de champ — `raw_analysis["path"]` remplit déjà
EXACTEMENT ce rôle (`"llm"`, `"no_llm"`, `"llm_crash"`,
`"fast_path_selection_index"`, `"interactive_bypass_confirm"`,
`"degraded_buyer_product_fallback"`, ...), sur TOUS les chemins de
`input_interpreter` (fast-path déterministe, LLM, repli dégradé, bypass
interactif, panne). Ajouter un second champ dupliquerait exactement la
classe de bug que ce chantier élimine ailleurs (`transaction_payload.phone`
vs `user_phone`, `candidates` vs `options`) — voir le rapport de clôture.
Ce fichier verrouille que `raw_analysis["path"]` est bien TOUJOURS présent,
pour que cette décision reste vraie dans le temps."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from tests.conftest import ScriptedLLM, StubRuntime, make_state, run


class TestInterpretationSourceIsAlwaysRawAnalysisPath:
    def test_llm_success_path_sets_a_source(self):
        interp = make_input_interpreter("PRODUCER")
        rt = StubRuntime(llm=ScriptedLLM({
            "disposition": "NEW_TASK",
            "intent": "SALES_PUBLISH_PRODUCT",
            "confidence": 0.9,
            "entities": {},
        }))
        state = make_state(
            normalized_text="je vends du mil", expected_input="NONE", user_role="PRODUCER"
        )
        result = run(interp(state, rt))
        # (2026-09-13, Incrément F) : la route NEW_TASK passe désormais par
        # `new_task_micro.py` — "new_task_micro" remplace l'ancien "llm"
        # générique (qui désignait indifféremment NEW_TASK/ACTIVE_SLOT/
        # STRUCTURED_ACTION avant leur migration en micro-prompts dédiés).
        assert result["raw_analysis"]["path"] == "new_task_micro"

    def test_no_llm_available_sets_a_source(self):
        interp = make_input_interpreter("PRODUCER")
        rt = StubRuntime(llm=None)
        state = make_state(
            normalized_text="bonjour", expected_input="NONE", user_role="PRODUCER"
        )
        result = run(interp(state, rt))
        assert result["raw_analysis"]["path"] == "no_llm"

    def test_deterministic_fast_path_sets_a_source(self):
        interp = make_input_interpreter("BUYER")
        rt = StubRuntime(llm=None)
        state = make_state(
            normalized_text="2",
            expected_input="SELECTION",
            expected_candidates=["A", "B"],
            user_role="BUYER",
        )
        result = run(interp(state, rt))
        assert result["raw_analysis"]["path"] == "fast_path_selection_index"

    def test_interactive_bypass_sets_a_source(self):
        interp = make_input_interpreter("BUYER")
        rt = StubRuntime(llm=None)
        state = make_state(interactive_selection="CONFIRM", user_role="BUYER")
        result = run(interp(state, rt))
        assert result["raw_analysis"]["path"] == "interactive_bypass_confirm"

    def test_llm_crash_sets_a_source(self):
        interp = make_input_interpreter("PRODUCER")

        class _Crashing:
            @property
            def chat(self):
                return self

            @property
            def completions(self):
                return self

            def create(self, **kwargs):
                raise RuntimeError("down")

        rt = StubRuntime(llm=_Crashing())
        state = make_state(
            normalized_text="un message", expected_input="NONE", user_role="PRODUCER"
        )
        result = run(interp(state, rt))
        assert result["raw_analysis"]["path"] == "llm_crash"
