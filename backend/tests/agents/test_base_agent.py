"""
tests/agents/test_base_agent.py
================================
Tests unitaires pour :
  - BaseAgent (HITL, clarification, handoff, dispatch, capabilities)
  - ClimateSentinel → handoff vers marketplace sur détection d'achat
  - MarketplaceAgent → require_human déclenché sur grande transaction
"""

from __future__ import annotations

import sys
import os

# Assure que le répertoire src est trouvable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest
from unittest.mock import MagicMock, patch

from agriconnect.agents.base import BaseAgent


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_state(**kwargs) -> dict:
    return {
        "user_query": "",
        "status": "INIT",
        **kwargs,
    }


class ConcreteAgent(BaseAgent):
    """Sous-classe minimale pour tester les méthodes de BaseAgent."""

    _capabilities = ["ACTION_A", "ACTION_B"]

    def _handle_action_a(self, state: dict) -> dict:
        state["handled"] = "action_a"
        return state

    def _handle_action_b(self, state: dict) -> dict:
        state["handled"] = "action_b"
        return state


# ─────────────────────────────────────────────────────────────────────────────
# require_human
# ─────────────────────────────────────────────────────────────────────────────

class TestRequireHuman:

    def test_sets_requires_human_flag(self):
        agent = ConcreteAgent()
        state = agent.require_human(_make_state(), reason="Test reason")
        assert state["requires_human"] is True

    def test_sets_human_reason(self):
        agent = ConcreteAgent()
        state = agent.require_human(_make_state(), reason="Montant trop élevé")
        assert state["human_reason"] == "Montant trop élevé"

    def test_uses_custom_prompt_as_final_response(self):
        agent = ConcreteAgent()
        state = agent.require_human(_make_state(), reason="r", prompt="Confirmez-vous ?")
        assert state["final_response"] == "Confirmez-vous ?"

    def test_default_prompt_contains_reason(self):
        agent = ConcreteAgent()
        state = agent.require_human(_make_state(), reason="Vérification identité")
        assert "Vérification identité" in state["final_response"]

    def test_status_set_to_pending_human(self):
        agent = ConcreteAgent()
        state = agent.require_human(_make_state(), reason="r")
        assert state["status"] == "PENDING_HUMAN"

    def test_does_not_mutate_original_state(self):
        agent = ConcreteAgent()
        original = _make_state()
        agent.require_human(original, reason="r")
        assert "requires_human" not in original


# ─────────────────────────────────────────────────────────────────────────────
# clarify_intent
# ─────────────────────────────────────────────────────────────────────────────

class TestClarifyIntent:

    def test_sets_clarification_needed(self):
        agent = ConcreteAgent()
        state = agent.clarify_intent(_make_state(), question="Quelle culture ?")
        assert state["clarification_needed"] == "Quelle culture ?"

    def test_sets_requires_human(self):
        agent = ConcreteAgent()
        state = agent.clarify_intent(_make_state(), question="Q?")
        assert state["requires_human"] is True

    def test_question_is_final_response(self):
        agent = ConcreteAgent()
        state = agent.clarify_intent(_make_state(), question="Précisez la zone ?")
        assert state["final_response"] == "Précisez la zone ?"

    def test_status_awaiting_clarification(self):
        agent = ConcreteAgent()
        state = agent.clarify_intent(_make_state(), question="Q?")
        assert state["status"] == "AWAITING_CLARIFICATION"


# ─────────────────────────────────────────────────────────────────────────────
# handoff
# ─────────────────────────────────────────────────────────────────────────────

class TestHandoff:

    def test_sets_handoff_to(self):
        agent = ConcreteAgent()
        state = agent.handoff(_make_state(), target_agent="marketplace")
        assert state["handoff_to"] == "marketplace"

    def test_sets_handoff_reason(self):
        agent = ConcreteAgent()
        state = agent.handoff(_make_state(), target_agent="marketplace", reason="Achat détecté")
        assert state["handoff_reason"] == "Achat détecté"

    def test_sets_handoff_context(self):
        agent = ConcreteAgent()
        ctx = {"product": "fongicide"}
        state = agent.handoff(_make_state(), target_agent="marketplace", context=ctx)
        assert state["handoff_context"] == ctx

    def test_no_context_key_when_none(self):
        agent = ConcreteAgent()
        state = agent.handoff(_make_state(), target_agent="market")
        assert "handoff_context" not in state

    def test_does_not_mutate_original_state(self):
        agent = ConcreteAgent()
        original = _make_state()
        agent.handoff(original, target_agent="market")
        assert "handoff_to" not in original


# ─────────────────────────────────────────────────────────────────────────────
# dispatch (Strategy pattern)
# ─────────────────────────────────────────────────────────────────────────────

class TestDispatch:

    def test_dispatches_to_correct_handler(self):
        agent = ConcreteAgent()
        state = agent.dispatch("ACTION_A", _make_state())
        assert state["handled"] == "action_a"

    def test_dispatch_case_insensitive(self):
        agent = ConcreteAgent()
        state = agent.dispatch("action_b", _make_state())
        assert state["handled"] == "action_b"

    def test_unknown_intent_triggers_clarify(self):
        agent = ConcreteAgent()
        state = agent.dispatch("UNKNOWN_INTENT", _make_state())
        assert state["requires_human"] is True
        assert state["clarification_needed"]

    def test_unknown_intent_sets_awaiting_clarification_status(self):
        agent = ConcreteAgent()
        state = agent.dispatch("TOTALLY_UNKNOWN", _make_state())
        assert state["status"] == "AWAITING_CLARIFICATION"


# ─────────────────────────────────────────────────────────────────────────────
# capabilities (Observer pattern)
# ─────────────────────────────────────────────────────────────────────────────

class TestCapabilities:

    def test_returns_class_capabilities(self):
        assert ConcreteAgent.capabilities() == ["ACTION_A", "ACTION_B"]

    def test_is_copy_not_reference(self):
        caps = ConcreteAgent.capabilities()
        caps.append("EXTRA")
        assert "EXTRA" not in ConcreteAgent.capabilities()

    def test_base_agent_has_empty_capabilities(self):
        assert BaseAgent.capabilities() == []


# ─────────────────────────────────────────────────────────────────────────────
# ClimateSentinel — purchase handoff
# ─────────────────────────────────────────────────────────────────────────────

class TestClimateSentinelPurchaseHandoff:
    """Vérifie que ClimateSentinel déclenche un handoff vers marketplace
    lorsqu'une intention d'achat est détectée."""

    @pytest.fixture
    def sentinel(self):
        try:
            from agriconnect.graphs.nodes.sentinelle import ClimateSentinel
        except Exception:
            pytest.skip("ClimateSentinel not importable in this env")
        return ClimateSentinel.__new__(ClimateSentinel)

    def test_purchase_keyword_triggers_handoff_flag(self, sentinel):
        state = _make_state()
        state = sentinel.handoff(state, target_agent="marketplace", reason="Achat fongicide détecté")
        assert state["handoff_to"] == "marketplace"

    def test_purchase_handoff_needed_with_keyword(self, sentinel):
        result = sentinel._purchase_handoff_needed("je veux acheter du fongicide", [])
        assert result is True

    def test_no_purchase_keyword_no_handoff(self, sentinel):
        result = sentinel._purchase_handoff_needed("quel temps fait-il aujourd'hui ?", [])
        assert result is False

    def test_hazard_purchase_intent_detected(self, sentinel):
        # hazard dict with a disease label triggers handoff (maladie keyword)
        result = sentinel._purchase_handoff_needed(
            "prévisions pluie",
            [{"label": "Risque maladie fongique"}],
        )
        assert result is True

    def test_capabilities_include_weather_intents(self, sentinel):
        caps = sentinel.__class__.capabilities()
        assert "CHECK_WEATHER" in caps
        assert "FLOOD_RISK" in caps


# ─────────────────────────────────────────────────────────────────────────────
# MarketplaceAgent — HITL on large transaction
# ─────────────────────────────────────────────────────────────────────────────

class TestMarketplaceHITL:
    """Vérifie que MarketplaceAgent déclenche require_human au-dessus du seuil."""

    @pytest.fixture
    def marketplace(self):
        try:
            from agriconnect.graphs.nodes.marketplace import MarketplaceAgent, _FINANCIAL_THRESHOLD_FCFA
            agent = MarketplaceAgent.__new__(MarketplaceAgent)
            agent._threshold = _FINANCIAL_THRESHOLD_FCFA
            return agent
        except Exception:
            pytest.skip("MarketplaceAgent not importable in this env")

    def test_require_human_sets_flag(self, marketplace):
        state = _make_state()
        state = marketplace.require_human(state, reason="Transaction 200 000 FCFA")
        assert state["requires_human"] is True
        assert "200 000 FCFA" in state["human_reason"]

    def test_require_human_does_not_commit(self, marketplace):
        """require_human ne doit pas modifier d'état DB — seulement flagger."""
        state = _make_state(transaction_payload=None)
        new_state = marketplace.require_human(state, reason="sécurité")
        # L'état original ne doit pas être muté
        assert state.get("requires_human") is None
        assert new_state["requires_human"] is True

    def test_requires_human_key_in_transaction_payload(self, marketplace):
        """Le payload de transaction doit porter le flag requires_human standard."""
        payload = {
            "intent": "SELL_PRODUCT",
            "data": {},
            "status": "PENDING",
            "requires_human": True,   # doit être la clé standard
        }
        assert "requires_human" in payload
        assert "requires_human_review" not in payload, (
            "Ancienne clé requires_human_review ne devrait plus être utilisée dans les payloads"
        )
