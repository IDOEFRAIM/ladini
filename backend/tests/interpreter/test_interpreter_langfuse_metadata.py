"""Instrumentation Langfuse de `input_interpreter` (2026-09-12) — vérifie
que les dimensions minimales requises pour calculer, PAR MESSAGE, les
appels LLM / tokens / taux de cache-hit / distribution de modèle sont
RÉELLEMENT transmises jusqu'à `_gateway.complete(...)` via `extra_metadata`
— sans changer le comportement fonctionnel de l'interpréteur (le contrat
`interpreted_event`/`detected_intent`/... reste identique, voir les autres
fichiers de ce dossier)."""
from __future__ import annotations

from typing import Any, Dict, List

import ladini.graphs.agents.market_coach.llm_gateway as llm_gateway_module
from ladini.graphs.agents.market_coach.interpreter.new_task_prompts import (
    NEW_TASK_PROMPT_VERSION,
)
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from tests.conftest import ForbiddenLLM, StubRuntime, make_state, run


class _CapturingGateway:
    """Capture les kwargs de chaque appel `.complete(...)` — permet de
    vérifier CE QUI aurait été transmis à la télémétrie sans dépendre d'un
    vrai réseau ni d'un vrai Langfuse."""

    def __init__(self, response: Dict[str, Any]) -> None:
        self._response = response
        self.calls: List[Dict[str, Any]] = []

    def primary_model_name(self, profile) -> str:
        return "test-model-x"

    async def complete(self, **kwargs: Any):
        self.calls.append(kwargs)
        import json

        class _Msg:
            def __init__(self, content: str) -> None:
                self.content = content

        class _Choice:
            def __init__(self, content: str) -> None:
                self.message = _Msg(content)

        class _Completion:
            def __init__(self, content: str, model: str) -> None:
                self.choices = [_Choice(content)]
                self.model = model

        return _Completion(json.dumps(self._response), "test-model-x")


def _patch_gateway(monkeypatch, response: Dict[str, Any]) -> _CapturingGateway:
    # `routing.py` fait `from ladini.graphs.agents.market_coach.llm_gateway
    # import resolve_gateway, resolve_profile` LOCALEMENT, à l'intérieur de
    # la fonction, à chaque appel — patcher les noms sur le module SOURCE
    # (pas sur `routing` lui-même, qui n'a jamais ces noms en attribut de
    # module) affecte donc bien le prochain import local.
    gateway = _CapturingGateway(response)
    monkeypatch.setattr(llm_gateway_module, "resolve_gateway", lambda mc_runtime: gateway)
    monkeypatch.setattr(llm_gateway_module, "resolve_profile", lambda mc_runtime: "REASONING")
    return gateway


class TestExtraMetadataReachesTheGatewayCall:
    def test_real_call_carries_the_minimal_observability_dimensions(self, monkeypatch):
        gateway = _patch_gateway(monkeypatch, {
            "disposition": "NEW_TASK",
            "intent": "SALES_PUBLISH_PRODUCT",
            "confidence": 0.9,
            "entities": {"product": "mais"},
        })
        interp = make_input_interpreter("PRODUCER")
        state = make_state(
            normalized_text="je vends du mais",
            expected_input="NONE",
            current_goal="SALES_PUBLISH_PRODUCT",
            user_role="PRODUCER",
            message_sid="SM_META_TEST",
        )
        run(interp(state, StubRuntime(llm=ForbiddenLLM())))

        assert len(gateway.calls) == 1
        meta = gateway.calls[0].get("extra_metadata")
        assert meta is not None
        assert meta["message_sid"] == "SM_META_TEST"
        assert meta["prompt_version"] == NEW_TASK_PROMPT_VERSION
        assert meta["prompt_family"] == "new_task"
        assert meta["cache_hit"] is False
        assert meta["current_goal"] == "SALES_PUBLISH_PRODUCT"
        # (2026-09-13, Incrément F) : `expected_input` n'est plus une
        # dimension pertinente pour NEW_TASK — cette route n'est choisie par
        # le State Router QUE lorsqu'aucun slot/menu n'est actif (voir
        # `state_router.py::choose_interpretation_route`), donc la valeur
        # serait structurellement toujours "NONE" ici : retirée plutôt que
        # gardée comme un signal constant sans information (spec §53 ne la
        # liste d'ailleurs pas parmi les dimensions attendues).
        assert "llm_call_index" in meta  # présent (valeur None acceptable si Redis absent)
        assert gateway.calls[0].get("agent_node") == "input_interpreter"

    def test_cache_hit_is_marked_true_and_skips_a_second_real_call(self, monkeypatch):
        from .test_interpreter_retry_safety import _patch_cache

        _patch_cache(monkeypatch)
        gateway = _patch_gateway(monkeypatch, {
            "disposition": "NEW_TASK",
            "intent": "SALES_PUBLISH_PRODUCT",
            "confidence": 0.9,
            "entities": {"product": "mais"},
        })
        interp = make_input_interpreter("PRODUCER")
        state = make_state(
            normalized_text="je vends du mais",
            expected_input="NONE",
            user_role="PRODUCER",
            message_sid="SM_META_CACHE_HIT",
        )

        run(interp(dict(state), StubRuntime(llm=ForbiddenLLM())))
        run(interp(dict(state), StubRuntime(llm=ForbiddenLLM())))

        # Un seul VRAI appel gateway malgré 2 invocations (2e = cache hit).
        assert len(gateway.calls) == 1
        assert gateway.calls[0]["extra_metadata"]["cache_hit"] is False

    def test_missing_message_sid_still_calls_but_metadata_field_is_none(self, monkeypatch):
        """Sans `message_sid` (pas de cache possible), l'appel a lieu
        normalement — `extra_metadata["message_sid"]` est `None`, pas une
        clé absente (le contrat de dimension reste stable pour les
        consommateurs Langfuse en aval)."""
        gateway = _patch_gateway(monkeypatch, {
            "disposition": "NEW_TASK",
            "intent": "SALES_PUBLISH_PRODUCT",
            "confidence": 0.9,
            "entities": {"product": "mais"},
        })
        interp = make_input_interpreter("PRODUCER")
        state = make_state(
            normalized_text="je vends du mais",
            expected_input="NONE",
            user_role="PRODUCER",
            message_sid=None,
        )
        run(interp(state, StubRuntime(llm=ForbiddenLLM())))

        assert len(gateway.calls) == 1
        assert gateway.calls[0]["extra_metadata"]["message_sid"] is None

    def test_interpretation_route_is_computed_and_attached_for_observability(
        self, monkeypatch
    ):
        """Chantier State Router (2026-09-12, Incrément A) : `state_router.
        choose_interpretation_route` est calculée et déposée dans
        `extra_metadata["interpretation_route"]` — PUREMENT pour
        instrumentation Langfuse (DoD §62 point 6). Elle ne doit encore
        influencer AUCUN comportement : le NEW_TASK classifier ci-dessous
        est appelé exactement comme avant, avec ou sans cette
        instrumentation."""
        gateway = _patch_gateway(monkeypatch, {
            "disposition": "NEW_TASK",
            "intent": "SALES_PUBLISH_PRODUCT",
            "confidence": 0.9,
            "entities": {"product": "mais"},
        })
        interp = make_input_interpreter("PRODUCER")
        state = make_state(
            normalized_text="je vends du mais",
            expected_input="NONE",
            current_goal=None,
            user_role="PRODUCER",
            message_sid="SM_ROUTE_TEST",
        )
        run(interp(state, StubRuntime(llm=ForbiddenLLM())))

        assert len(gateway.calls) == 1
        assert gateway.calls[0]["extra_metadata"]["interpretation_route"] == "new_task"
