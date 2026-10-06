"""Cache d'interprétation LLM keyed par `message_sid` (2026-09-12).

Preuve requise : `process_agent_task` (api/tasks.py) a
`autoretry_for=(Exception,), max_retries=3` — une erreur survenant APRÈS
un appel LLM d'interprétation déjà réussi (échec DB/MCP/WhatsApp en aval,
par exemple) relance TOUT le tour, y compris `input_interpreter`. Sans
cache, ce second passage rappelle le LLM pour un résultat déjà connu — un
coût payé deux fois pour zéro valeur.

Ces tests monkeypatchent le cache Redis (`_get_cached_value`/
`_set_cached_value`, importés dans `interpreter/routing.py` depuis
`core/idempotency.py`) par un dict en mémoire : le comportement testé est
la LOGIQUE de cache (clé dérivée de `message_sid`, lecture avant appel,
écriture après), pas la disponibilité réelle de Redis (hors périmètre
d'un test unitaire, déjà fail-open par construction — voir
`core/idempotency.py::get_cached`/`set_cached`, qui dégradent silencieusement
vers "pas de cache" si Redis est indisponible)."""
from __future__ import annotations

from typing import Any, Dict

from ladini.graphs.agents.market_coach.interpreter import routing as routing_module
from ladini.graphs.agents.market_coach.interpreter import (
    new_task_micro as new_task_micro_module,
)
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from tests.conftest import ScriptedLLM, StubRuntime, make_state, run


class _FakeRedisValueStore:
    """Dict en mémoire simulant `core.idempotency.get_cached`/`set_cached`
    pour la durée d'un test — remplace la dépendance à un vrai Redis."""

    def __init__(self) -> None:
        self._data: Dict[str, str] = {}

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value, *, ttl_seconds: int = 3600) -> None:
        if key:
            self._data[key] = value


def _patch_cache(monkeypatch) -> _FakeRedisValueStore:
    """(2026-09-13, Incrément F) : patche AUSSI `new_task_micro.get_cached`/
    `set_cached` — la route NEW_TASK (`expected_input=NONE`, aucun tunnel
    actif, le cas exercé par tous les tests de ce fichier) passe désormais
    par ce module, avec son PROPRE alias importé de `core/idempotency.py`,
    distinct de celui de `routing.py` (patcher seulement `routing_module`
    laisserait `new_task_micro` frapper le VRAI Redis)."""
    store = _FakeRedisValueStore()
    monkeypatch.setattr(routing_module, "_get_cached_value", store.get)
    monkeypatch.setattr(routing_module, "_set_cached_value", store.set)
    monkeypatch.setattr(new_task_micro_module, "get_cached", store.get)
    monkeypatch.setattr(new_task_micro_module, "set_cached", store.set)
    return store


class TestCeleryRetryDoesNotRepayTheLlm:
    def test_same_message_sid_calls_the_llm_only_once_across_two_invocations(
        self, monkeypatch
    ):
        """Simule un retry Celery : `input_interpreter` est invoqué DEUX fois
        pour le MÊME `message_sid` (même appel LLM en amont déjà réussi, une
        erreur aval quelconque a fait tout relancer). Le 2e appel doit relire
        le cache, pas rappeler le LLM."""
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
            message_sid="SM_RETRY_TEST_0001",
        )

        first = run(interp(dict(state), rt))
        assert first["detected_intent"] == "SALES_PUBLISH_PRODUCT"
        assert llm.calls == 1

        # "Retry Celery" = un DEUXIÈME appel du même nœud, même état/message_sid.
        second = run(interp(dict(state), rt))
        assert second["detected_intent"] == "SALES_PUBLISH_PRODUCT"
        assert second["interpreted_event"] == first["interpreted_event"]
        assert second["extracted_entities"] == first["extracted_entities"]
        # L'assertion qui compte : PAS un second appel LLM.
        assert llm.calls == 1

    def test_different_message_sid_calls_the_llm_again(self, monkeypatch):
        """Contre-preuve : deux messages DIFFÉRENTS (message_sid distinct)
        ne doivent JAMAIS partager un résultat de cache — un vrai nouveau
        message coûte bien un vrai nouvel appel."""
        _patch_cache(monkeypatch)
        interp = make_input_interpreter("PRODUCER")
        llm = ScriptedLLM({
            "disposition": "NEW_TASK",
            "intent": "SALES_PUBLISH_PRODUCT",
            "confidence": 0.95,
            "entities": {"product": "mais"},
        })
        rt = StubRuntime(llm=llm)

        state_a = make_state(
            normalized_text="je voudrais mettre en vente du mais",
            expected_input="NONE",
            user_role="PRODUCER",
            message_sid="SM_RETRY_TEST_A",
        )
        state_b = make_state(
            normalized_text="je voudrais mettre en vente du mais",
            expected_input="NONE",
            user_role="PRODUCER",
            message_sid="SM_RETRY_TEST_B",
        )

        run(interp(state_a, rt))
        assert llm.calls == 1
        run(interp(state_b, rt))
        assert llm.calls == 2

    def test_missing_message_sid_never_caches(self, monkeypatch):
        """Sans `message_sid` (appel hors webhook/test direct), le cache est
        structurellement désactivé (clé `None`) — chaque appel reste un vrai
        appel LLM, jamais une resucée silencieuse d'un tour sans rapport."""
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
            normalized_text="je voudrais mettre en vente du mais",
            expected_input="NONE",
            user_role="PRODUCER",
            message_sid=None,
        )

        run(interp(dict(state), rt))
        run(interp(dict(state), rt))
        assert llm.calls == 2
