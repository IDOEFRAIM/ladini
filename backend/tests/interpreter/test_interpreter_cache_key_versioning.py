"""Sensibilité de la clé de cache LLM (`interpreter/routing.py::_llm_cache_key`)
à la version du prompt, au modèle, et au contenu du system prompt
(2026-09-12) — sans cette sensibilité, un déploiement qui change le prompt
système (nouvelle règle, catalogue modifié) ou bascule de modèle
pourrait silencieusement réutiliser une completion produite par
l'ANCIEN prompt/modèle pour un `message_sid` retenté après coup — une
incohérence invisible, jamais un crash.

Deux niveaux de test :
  1. Unitaire, direct sur `_llm_cache_key` — précis, rapide, ne dépend
     d'aucune infrastructure LLM.
  2. Bout en bout, via l'interpréteur complet — preuve que
     `INTERPRETER_PROMPT_VERSION` influence RÉELLEMENT le comportement de
     cache observable (pas seulement la fonction isolée)."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.interpreter import (
    new_task_micro as new_task_micro_module,
)
from ladini.graphs.agents.market_coach.interpreter import routing as routing_module
from ladini.graphs.agents.market_coach.interpreter.routing import (
    _llm_cache_key,
    make_input_interpreter,
)
from tests.conftest import ScriptedLLM, StubRuntime, make_state, run

from .test_interpreter_retry_safety import _patch_cache


class TestCacheKeySensitivity:
    def test_identical_inputs_produce_the_identical_key(self):
        k1 = _llm_cache_key("SM1", "system A", "user A", "model-x")
        k2 = _llm_cache_key("SM1", "system A", "user A", "model-x")
        assert k1 == k2
        assert k1 is not None

    def test_different_prompt_version_produces_a_different_key(self, monkeypatch):
        k1 = _llm_cache_key("SM1", "system A", "user A", "model-x")
        monkeypatch.setattr(routing_module, "INTERPRETER_PROMPT_VERSION", "interpreter_vX")
        # `_llm_cache_key` lit la constante du MODULE au moment de l'appel via
        # une f-string sur le nom importé au niveau module — on doit donc
        # appeler la fonction APRÈS le monkeypatch pour que le nouveau nom
        # soit effectivement utilisé (le corps de la fonction référence
        # `INTERPRETER_PROMPT_VERSION` comme un global du module, pas une
        # valeur figée à l'import).
        k2 = _llm_cache_key("SM1", "system A", "user A", "model-x")
        assert k1 != k2

    def test_different_model_produces_a_different_key(self):
        k1 = _llm_cache_key("SM1", "system A", "user A", "model-x")
        k2 = _llm_cache_key("SM1", "system A", "user A", "model-y")
        assert k1 != k2

    def test_different_system_prompt_produces_a_different_key(self):
        k1 = _llm_cache_key("SM1", "system A", "user A", "model-x")
        k2 = _llm_cache_key("SM1", "system B (nouvelle règle)", "user A", "model-x")
        assert k1 != k2

    def test_different_user_prompt_produces_a_different_key(self):
        k1 = _llm_cache_key("SM1", "system A", "user A", "model-x")
        k2 = _llm_cache_key("SM1", "system A", "user B", "model-x")
        assert k1 != k2

    def test_missing_message_sid_always_returns_none(self):
        assert _llm_cache_key(None, "system A", "user A", "model-x") is None


class TestCacheVersioningEndToEnd:
    """(2026-09-13, Incrément F) : la route NEW_TASK (`expected_input=NONE`,
    aucun tunnel actif) passe désormais par `new_task_micro.py`, avec sa
    PROPRE version de cache (`NEW_TASK_PROMPT_VERSION`), indépendante
    d'`INTERPRETER_PROMPT_VERSION` (qui ne concerne plus que l'interpréteur
    unifié legacy, seulement atteint sur repli infrastructurel). Ces tests
    patchent donc la constante réellement lue par le chemin exercé —
    `new_task_micro.NEW_TASK_PROMPT_VERSION` — même principe de précaution
    que documenté ci-dessous pour `_llm_cache_key` (lire où la valeur est
    CONSOMMÉE, pas seulement où elle est définie)."""

    def test_bumping_prompt_version_forces_a_fresh_llm_call_for_the_same_sid(
        self, monkeypatch
    ):
        """Même `message_sid`, mais `NEW_TASK_PROMPT_VERSION` change ENTRE
        les deux appels (= un déploiement a modifié le prompt système) — le
        2e appel ne doit PAS réutiliser la completion du 1er."""
        _patch_cache(monkeypatch)
        interp = make_input_interpreter("PRODUCER")
        llm = ScriptedLLM({
            "disposition": "NEW_TASK",
            "intent": "SALES_PUBLISH_PRODUCT",
            "confidence": 0.95,
            "entities": {"product": "mais"},
        })
        rt = StubRuntime(llm=llm)
        state = make_state(
            normalized_text="je voudrais mettre en vente du mais que j'ai recolte",
            expected_input="NONE",
            user_role="PRODUCER",
            message_sid="SM_VERSION_BUMP",
        )

        run(interp(dict(state), rt))
        assert llm.calls == 1

        monkeypatch.setattr(
            new_task_micro_module, "NEW_TASK_PROMPT_VERSION", "new_task_v2_test"
        )
        run(interp(dict(state), rt))
        assert llm.calls == 2  # PAS de cache hit malgré le même message_sid

    def test_same_prompt_version_still_hits_cache_for_the_same_sid(self, monkeypatch):
        """Contre-preuve de non-régression : SANS changement de version, le
        cache continue de fonctionner normalement (voir aussi
        `test_interpreter_retry_safety.py`, dupliqué ici pour bien encadrer
        la comparaison avec le test ci-dessus)."""
        _patch_cache(monkeypatch)
        interp = make_input_interpreter("PRODUCER")
        llm = ScriptedLLM({
            "disposition": "NEW_TASK",
            "intent": "SALES_PUBLISH_PRODUCT",
            "confidence": 0.95,
            "entities": {"product": "mais"},
        })
        rt = StubRuntime(llm=llm)
        state = make_state(
            normalized_text="je voudrais mettre en vente du mais que j'ai recolte",
            expected_input="NONE",
            user_role="PRODUCER",
            message_sid="SM_VERSION_STABLE",
        )

        run(interp(dict(state), rt))
        run(interp(dict(state), rt))
        assert llm.calls == 1
