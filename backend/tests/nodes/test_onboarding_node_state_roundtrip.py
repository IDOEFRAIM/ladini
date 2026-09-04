"""`flows/common/onboarding.py::onboarding_node` — le pont LangGraph state
dict <-> `OnboardingState`, jamais couvert directement jusqu'ici (seule la
fonction pure `run_onboarding_step` avait des tests). Vérification demandée
(2026-08-27) : "reverifie que le systeme d'onboarding fonctionne bien" —
ce fichier verrouille que `onboarding_profile`/`transaction_payload` sont
bien reconstruits à l'identique d'un tour à l'autre (la classe de bug
documentée dans [[market-coach-turn-boundary-state]] : un nœud de fin de
tour qui droppe un champ dont le tour suivant a besoin)."""
from __future__ import annotations

from typing import Any, Dict

from agriconnect.graphs.agents.market_coach.flows.common.onboarding import onboarding_node
from tests.conftest import run


class _FakeRuntime:
    def __init__(self, *, llm_out: Dict[str, Any] | None = None, zone_id: str = "z1"):
        self.llm = object() if llm_out is not None else None
        self._llm_out = llm_out or {}
        self._zone_id = zone_id

    async def call_db(self, tool_name: str, **kwargs: Any):
        if tool_name == "get_zone_by_name":
            return {"data": {"zone_id": self._zone_id, "zone_name": kwargs.get("name")}}
        if tool_name == "get_available_zones":
            return {"data": [{"label": "Bobo-Dioulasso"}]}
        if tool_name == "create_user_profile":
            return {"status": "success", "data": {"id": "u1"}}
        if tool_name == "get_user_by_phone":
            return {"data": {"id": "u1"}}
        return {}


def _patch_llm_extract(monkeypatch, outputs):
    """`outputs` is a list of dicts consumed in order, one per call."""
    calls = {"n": 0}

    async def _fake(mc_runtime, text, context_hint=""):
        i = min(calls["n"], len(outputs) - 1)
        calls["n"] += 1
        base = {"role": None, "name": None, "zone": None, "confirm": None, "is_question": False, "reply": None}
        base.update(outputs[i])
        return base

    import agriconnect.graphs.agents.market_coach.flows.common.onboarding as mod
    monkeypatch.setattr(mod, "_llm_extract_onboarding_all", _fake)


class TestOnboardingNodeStateRoundtrip:
    def test_cold_start_activates_onboarding_and_asks_the_role(self, monkeypatch):
        _patch_llm_extract(monkeypatch, [{}])
        state = {"is_onboarding": True, "user_phone": "+22670000001", "normalized_text": ""}
        result = run(onboarding_node(state, _FakeRuntime()))

        assert result["is_onboarding"] is True
        assert result["response_strategy"] == "ONBOARDING"
        assert "Bienvenue" in result["onboarding_prompt"]
        assert result["onboarding_internal_step"] == "COLLECT_ROLE"

    def test_role_and_name_collected_in_one_turn_survive_to_the_next_turn(self, monkeypatch):
        """Le champ critique : `onboarding_profile` (et sa copie
        `transaction_payload`) doivent porter role+name vers le tour
        suivant — sans ça, le tour suivant repart de zéro."""
        _patch_llm_extract(monkeypatch, [
            {"role": "PRODUCER", "name": "Awa"},
            {"zone": "Bobo"},
        ])
        state = {
            "is_onboarding": True,
            "user_phone": "+22670000001",
            "normalized_text": "Je suis Awa, productrice",
            "onboarding_internal_step": "COLLECT_ROLE",
        }
        result = run(onboarding_node(state, _FakeRuntime()))

        assert result["onboarding_profile"]["role"] == "PRODUCER"
        assert result["onboarding_profile"]["name"] == "Awa"
        assert result["transaction_payload"]["role"] == "PRODUCER"
        assert result["transaction_payload"]["name"] == "Awa"
        assert result["is_onboarding"] is True  # zone still missing

        # Next turn: only the state carried over is fed back in (simulates
        # the checkpointer round-trip) — zone provided now.
        next_state = {
            "is_onboarding": True,
            "user_phone": "+22670000001",
            "normalized_text": "je suis a Bobo",
            "onboarding_internal_step": result["onboarding_internal_step"],
            "onboarding_profile": result["onboarding_profile"],
            "transaction_payload": result["transaction_payload"],
        }
        result2 = run(onboarding_node(next_state, _FakeRuntime()))
        # role/name must still be present — carried via onboarding_profile,
        # NOT re-extracted this turn.
        assert result2["onboarding_profile"]["role"] == "PRODUCER"
        assert result2["onboarding_profile"]["name"] == "Awa"
        assert result2["onboarding_profile"]["zone_id"] == "z1"

    def test_full_flow_reaches_completion_and_clears_onboarding_flags(self, monkeypatch):
        _patch_llm_extract(monkeypatch, [
            {"role": "PRODUCER", "name": "Awa", "zone": "Bobo"},
            {"confirm": "YES"},
        ])
        rt = _FakeRuntime()
        state = {
            "is_onboarding": True,
            "user_phone": "+22670000001",
            "normalized_text": "Je suis Awa, productrice a Bobo",
        }
        r1 = run(onboarding_node(state, rt))
        assert r1["onboarding_step"] == "COMPLETED"  # display-complete (CONFIRM_DETAILS)
        assert r1["is_onboarding"] is True  # not YET actually done

        state2 = {
            "is_onboarding": True,
            "user_phone": "+22670000001",
            "normalized_text": "oui",
            "onboarding_internal_step": r1["onboarding_internal_step"],
            "onboarding_profile": r1["onboarding_profile"],
            "transaction_payload": r1["transaction_payload"],
        }
        r2 = run(onboarding_node(state2, rt))
        # CREATE_PROFILE succeeds -> lands on COLLECT_LOCATION, not yet fully
        # `completed` (GPS step still pending) but already SUCCESS/onboarding.
        assert r2["status"] == "SUCCESS"
        assert "Bienvenue patron Awa" in r2["onboarding_prompt"]

        state3 = {
            "is_onboarding": True,
            "user_phone": "+22670000001",
            "normalized_text": "",
            "location_shared": True,
            "onboarding_internal_step": r2["onboarding_internal_step"],
            "onboarding_profile": r2["onboarding_profile"],
            "transaction_payload": r2["transaction_payload"],
        }
        r3 = run(onboarding_node(state3, rt))
        assert r3["is_onboarding"] is False
        assert r3["status"] == "COMPLETED"
        assert r3["onboarding_internal_step"] is None

    def test_a_non_onboarding_turn_is_a_silent_pass_through(self):
        state = {"is_onboarding": False, "response_strategy": "SUCCESS"}
        result = run(onboarding_node(state, _FakeRuntime()))
        assert result == {}
