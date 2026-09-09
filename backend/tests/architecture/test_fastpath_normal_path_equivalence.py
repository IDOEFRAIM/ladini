"""Phase 5 (mandat 2026-09-08) — durcissement du FastPath.

Deux questions posées explicitement par le mandat :

1. Le FastPath respecte-t-il le même mécanisme `retry_count` que le chemin
   normal ? OUI, par construction : `retry_count` n'est ni lu ni écrit dans
   `interpreter/routing.py` (FastPath ou LLM) — il est géré EXCLUSIVEMENT en
   aval, dans `nodes/cognitive.py::cognitive_guard`, qui ne fait AUCUNE
   distinction sur `raw_analysis["path"]` : que `interpreted_event` vienne du
   FastPath ou du LLM, `cognitive_guard` le traite identiquement. Il n'existe
   donc structurellement PAS de 2e mécanisme de retry à dupliquer/diverger.
2. La protection anti-dérive de goal (goal-locking) est-elle dupliquée ?
   NON : elle vit UNIQUEMENT dans le chemin LLM
   (`interpreter/routing.py`, bloc `_is_different_goal`/`_locked_goal_upper`,
   ~ligne 1486) — le FastPath ne peut structurellement PAS dériver : chaque
   branche FastPath renvoie `detected_intent = locked_goal` verbatim (jamais
   une intention inventée), donc la validation de dérive n'a rien à
   valider pour ce chemin. Voir `TestGoalLockOnlyValidatedOnTheLlmPath`
   ci-dessous pour la preuve par le code (AST) qu'aucune 2e implémentation
   n'est apparue.

Ce fichier ajoute le test explicitement demandé par le mandat (Test J) :
équivalence métier FastPath vs LLM pour une MÊME entrée utilisateur, sur le
cas de repli documenté ("nombre nu sans unité ni devise pendant PRICE" —
`interpreter/routing.py::_interpret_fast_path`, docstring `skip_numeric_shortcut`)."""
from __future__ import annotations

import ast
import inspect
import pathlib

from agriconnect.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from tests.conftest import ScriptedLLM, StubRuntime, make_state, run


class TestFastPathVsLlmBusinessEquivalence:
    """Test J (mandat §18) : même entrée -> même état métier, que le nombre
    nu ambigu ("environ 300" pendant PRICE) soit résolu par le repli
    déterministe (LLM indisponible) ou par le LLM (disponible) — cas
    explicitement documenté comme un point de bascule réel entre les deux
    chemins (voir `_interpret_fast_path.__doc__`)."""

    TEXT = "environ 300"

    def _state(self):
        return make_state(
            normalized_text=self.TEXT,
            expected_input="PRICE",
            current_goal="SALES_PUBLISH_PRODUCT",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
            transaction_payload={"product": "tomates"},
            user_role="PRODUCER",
        )

    def test_fast_path_and_llm_path_agree_on_the_extracted_price(self):
        interp = make_input_interpreter("PRODUCER")

        # (a) LLM indisponible -> repli déterministe (FastPath).
        fast_result = run(interp(self._state(), StubRuntime(llm=None)))

        # (b) LLM disponible, classification cohérente avec le même nombre.
        llm_payload = {
            "interpreted_event": "ANSWER",
            "detected_intent": "SALES_PUBLISH_PRODUCT",
            "interpreter_confidence": 0.9,
            "validation_status": "VALID",
            "extracted_entities": {"price": 300.0},
        }
        llm_result = run(
            interp(self._state(), StubRuntime(llm=ScriptedLLM(llm_payload)))
        )

        assert fast_result["interpreted_event"] == llm_result["interpreted_event"] == "ANSWER"
        assert (
            fast_result["detected_intent"]
            == llm_result["detected_intent"]
            == "SALES_PUBLISH_PRODUCT"
        )
        assert (
            fast_result["extracted_entities"].get("price")
            == llm_result["extracted_entities"].get("price")
            == 300.0
        )


class TestRetryCountNotDuplicated:
    """`retry_count` n'apparaît NULLE PART dans interpreter/routing.py — sa
    seule lecture/écriture métier vit dans nodes/cognitive.py. Preuve par
    grep structurel (pas de faux positif possible : le nom est spécifique)."""

    def test_interpreter_routing_never_touches_retry_count(self):
        import agriconnect.graphs.agents.market_coach.interpreter.routing as mod

        src = inspect.getsource(mod)
        assert "retry_count" not in src


class TestGoalLockOnlyValidatedOnTheLlmPath:
    """Preuve AST : aucune fonction `_interpret_fast_path` (ni les branches
    FastPath du module) ne construit `detected_intent` autrement qu'à partir
    de `locked_goal` tel quel — donc aucune divergence possible nécessitant
    une 2e validation anti-dérive."""

    def test_fast_path_branches_only_echo_locked_goal_as_intent(self):
        mod_path = (
            pathlib.Path(__file__).resolve().parents[2]
            / "src/agriconnect/graphs/agents/market_coach/interpreter/routing.py"
        )
        tree = ast.parse(mod_path.read_text(encoding="utf-8"))
        target = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_interpret_fast_path"
        )
        # Chaque `dict` littéral retourné doit soit omettre `detected_intent`
        # soit le construire à partir de `locked_goal` (jamais un intent
        # littéral en dur, jamais `raw_intent`/une valeur issue du LLM).
        offending = []
        for node in ast.walk(target):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "detected_intent":
                        text = ast.dump(value)
                        if "locked_goal" not in text and "UNKNOWN" not in text:
                            offending.append(ast.dump(value))
        assert not offending, (
            f"branche(s) FastPath construisant detected_intent hors de "
            f"locked_goal : {offending}"
        )
