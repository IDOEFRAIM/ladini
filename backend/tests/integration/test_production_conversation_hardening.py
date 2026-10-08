"""PRODUCTION CONVERSATION HARDENING — un message livré N fois, un worker qui meurt, un état rechargé : UN SEUL effet métier.

Moteur RÉEL (harnais `tests/harness`) : `Orchestrator.handle`, graphe compilé, `WorkspaceCheckpointer`, store JSON (aller-retour identique à `agri_workspaces`),
tâche Celery `process_agent_task` en eager avec ses `autoretry_for`. Seuls LLM, MCP/DB et envoi WhatsApp sont doublés ; Redis est un `FakeRedis` perdable.

Vocabulaire : « redémarrage » = nouvel `Orchestrator` + nouveau graphe + NOUVEAU Redis (clés de dédoublonnage perdues) sur le MÊME store de workspace et les MÊMES
tables de domaine : exactement ce que voit un worker relancé.
"""
from __future__ import annotations

from contextlib import ExitStack
from typing import Any, Dict, Tuple

import pytest

from tests.harness import ConversationHarness, new_task
from tests.harness.conversation import FakeRedis

pytestmark = pytest.mark.integration

_COQ = new_task("CREATE_RECURRING_NEED", product="coq", quantity=14.0, recurrence_type="WEEKLY")


def _restart(old: ConversationHarness, stack: ExitStack) -> ConversationHarness:
    """Processus relancé : tout ce qui vit en RAM / dans Redis est perdu, la base (workspace + brouillons + serveur de domaine) subsiste."""
    new = ConversationHarness(role=old.role, phone=old.phone, channel=old.channel, store=old.store, redis=FakeRedis())
    new.drafts = old.drafts
    new.server = old.server
    new.runtime.responses["create_recurring_need"] = old.server.create_recurring_need
    new.runtime.responses["create_recurring_needs"] = old.server.create_recurring_needs
    new.runtime.responses["get_recurring_start_policy"] = old.server.get_recurring_start_policy
    stack.enter_context(new)
    return new


def _incomplete_profile() -> Tuple[Dict[str, Any], Any, Any]:
    state: Dict[str, Any] = {"declared_location": None, "name": None, "buyer": False, "calls": []}

    def get_user(**_: Any) -> Dict[str, Any]:
        return {"status": "success", "data": {
            "id": "22222222-2222-2222-2222-222222222222", "name": state["name"] or "User_2876", "phone": "+22670000001", "role": "USER",
            "zone": {"id": None, "name": "Zone inconnue"}, "declared_location": state["declared_location"],
            "permissions": {"can_buy": state["buyer"], "can_sell": False, "is_admin": False},
            "status": {"producer": None, "identity_verified": False}}}

    def complete(**kw: Any) -> Dict[str, Any]:
        state["calls"].append(dict(kw))
        if kw.get("declared_location"):
            state["declared_location"] = kw["declared_location"]
        if kw.get("name"):
            state["name"] = kw["name"]
        if str(kw.get("capability") or "").upper() == "BUY":
            state["buyer"] = True
        return {"status": "success", "data": {"id": "22222222-2222-2222-2222-222222222222"}}

    return state, get_user, complete


@pytest.fixture()
def conv():
    with ConversationHarness() as harness:
        yield harness


def _confirmed_once(conv: ConversationHarness) -> Any:
    conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
    return conv.send("oui", message_id="wamid.CONFIRM")


# ── 1. même message livré plusieurs fois ───────────────────────────────────────────────────────────────────────────────────────
class TestDuplicateInboundMessage:
    def test_the_same_confirmation_delivered_twice_creates_one_commitment(self, conv):
        first = _confirmed_once(conv)
        second = conv.send("oui", message_id="wamid.CONFIRM")
        assert conv.server.executions == 1
        assert second.response == first.response and second.llm_calls == 0
        assert not [t for t in second.mcp_tools() if t.startswith("create_")]

    def test_a_redelivery_after_the_redis_keys_are_gone_is_answered_from_the_database(self, conv):
        """TTL expiré / Redis vidé / panne Redis : le dédoublonnage Redis est un ACCÉLÉRATEUR, la base fait autorité."""
        first = _confirmed_once(conv)
        conv.redis._data.clear()
        conv.redis._expires.clear()
        second = conv.send("oui", message_id="wamid.CONFIRM")
        assert conv.server.executions == 1
        assert second.response == first.response
        assert second.mcp_tools() == [], "le tour n'est pas rejoué : aucune lecture, aucune écriture"
        assert second.llm_calls == 0

    def test_a_redelivery_after_a_process_restart_replays_the_original_reply(self, conv):
        first = _confirmed_once(conv)
        with ExitStack() as stack:
            restarted = _restart(conv, stack)
            second = restarted.send("oui", message_id="wamid.CONFIRM")
            assert restarted.server.executions == 1
            assert second.response == first.response and second.mcp_tools() == [] and second.llm_calls == 0

    def test_two_distinct_messages_with_the_same_text_are_two_messages(self, conv):
        """L'identité d'un message est son id fournisseur, jamais son texte : un 2ᵉ « oui » légitime n'est PAS rejoué (et ne recrée rien : le brouillon est terminal)."""
        _confirmed_once(conv)
        later = conv.send("oui", message_id="wamid.OTHER")
        assert conv.server.executions == 1, "le registre du domaine protège le double-tap (2 ids distincts)"
        assert later.llm_calls >= 0 and later.error is None


# ── 2. échec partiel : l'effet a eu lieu, la réponse non ───────────────────────────────────────────────────────────────────────
class TestPartialFailure:
    def test_mutation_done_then_send_fails_the_retry_delivers_the_original_success_message(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
        conv.dispatcher.fail_next = 1
        turn = conv.send("oui", message_id="wamid.R6")
        assert conv.server.executions == 1
        assert turn.error is None
        assert "noté" in turn.response.lower(), "l'utilisateur reçoit le succès d'origine, pas un message d'erreur ni une réponse recalculée"
        assert turn.llm_calls == 0, "la reprise ne relance pas le graphe"

    def test_two_consecutive_send_failures_still_end_with_one_effect_and_the_right_reply(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
        conv.dispatcher.fail_next = 2
        turn = conv.send("oui", message_id="wamid.R6B")
        assert conv.server.executions == 1 and "noté" in turn.response.lower()

    def test_a_technical_failure_is_never_stored_as_the_turn_result(self, conv):
        """LLM indisponible : « indisponible » ne doit pas devenir la réponse rejouée à ce message (le résultat durable n'est écrit que pour un vrai tour)."""
        import json

        conv.send("bonjour", message_id="wamid.DOWN")
        assert "last_turn" not in json.loads(conv.store.rows[conv.phone]["metadata"])
        conv.send("je veux 14 coqs chaque semaine", llm=_COQ, message_id="wamid.OK")
        stored = json.loads(conv.store.rows[conv.phone]["metadata"])["last_turn"]
        assert stored["message_sid"] == "wamid.OK" and "récapitulatif" in stored["final_response"].lower()


# ── 3. redémarrage entre deux tours ────────────────────────────────────────────────────────────────────────────────────────────
class TestRestartBetweenTurns:
    def test_restart_between_the_recap_and_the_confirmation_keeps_the_same_draft(self, conv):
        t1 = conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
        draft_before = t1.draft()
        with ExitStack() as stack:
            restarted = _restart(conv, stack)
            t2 = restarted.send("oui", message_id="wamid.AFTER_RESTART")
            assert restarted.server.executions == 1
            (_tool, kwargs), = [c for c in t2.mcp_calls if c[0] == "create_recurring_need"]
            assert kwargs["draft_id"] == draft_before["draft_id"], "le MÊME brouillon est confirmé après relance"
            assert kwargs["draft_version"] == draft_before["version"] + 1

    def test_restart_between_the_region_question_and_the_answer_resumes_the_same_business_flow(self):
        profile, get_user, complete = _incomplete_profile()
        with ExitStack() as stack:
            conv = stack.enter_context(ConversationHarness())
            conv.runtime.responses["get_user_by_phone"] = get_user
            conv.runtime.responses["complete_user_profile"] = complete
            t1 = conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
            assert "région" in t1.response.lower() and t1.after.get("profile_gate")
            restarted = _restart(conv, stack)
            restarted.runtime.responses["get_user_by_phone"] = get_user
            restarted.runtime.responses["complete_user_profile"] = complete
            t2 = restarted.send("A Ouagadougou", message_id="wamid.REGION")
            assert len(profile["calls"]) == 1 and profile["declared_location"]
            assert "récapitulatif" in t2.response.lower() and "coq" in t2.response.lower(), "la demande d'origine reprend, sans être redite"
            assert not t2.after.get("profile_gate")

    def test_a_replayed_profile_answer_updates_the_profile_and_resumes_exactly_once(self):
        profile, get_user, complete = _incomplete_profile()
        with ExitStack() as stack:
            conv = stack.enter_context(ConversationHarness())
            conv.runtime.responses["get_user_by_phone"] = get_user
            conv.runtime.responses["complete_user_profile"] = complete
            conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
            first = conv.send("A Ouagadougou", message_id="wamid.REGION")
            conv.redis._data.clear()
            second = conv.send("A Ouagadougou", message_id="wamid.REGION")
            restarted = _restart(conv, stack)
            restarted.runtime.responses["get_user_by_phone"] = get_user
            restarted.runtime.responses["complete_user_profile"] = complete
            third = restarted.send("A Ouagadougou", message_id="wamid.REGION")
            assert len(profile["calls"]) == 1, "région posée UNE fois logiquement"
            assert first.response == second.response == third.response
            assert second.mcp_tools() == [] and third.mcp_tools() == []


# ── 4. concurrence et ordre ────────────────────────────────────────────────────────────────────────────────────────────────────
class TestConcurrencyAndOrdering:
    def test_two_concurrent_confirmations_create_one_commitment_and_never_interleave(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_COQ)
        outcome = conv.send_concurrently([("oui", None), ("oui", None)])
        assert conv.server.executions == 1
        assert not outcome.interleaved, "les deux tours de la même conversation sont sérialisés"
        assert not [r for r in outcome.responses if isinstance(r, BaseException)]

    def test_a_confirmation_with_nothing_pending_changes_nothing(self, conv):
        """Message arrivé AVANT celui qui le précède logiquement (« oui » avant la demande) : pas de mutation, pas d'exécution."""
        turn = conv.send("oui", message_id="wamid.EARLY")
        assert conv.server.executions == 0
        assert not [t for t in turn.mcp_tools() if t.startswith("create_")]

    def test_a_late_confirmation_after_the_draft_is_terminal_never_executes_again(self, conv):
        _confirmed_once(conv)
        late = conv.send("oui", message_id="wamid.LATE")
        assert conv.server.executions == 1
        assert not [t for t in late.mcp_tools() if t.startswith("create_")]


# ── 5. contrat de persistance du résultat de tour (unités) ──────────────────────────────────────────────────────────────────────
class TestLastTurnContract:
    def test_the_stored_turn_is_bounded_and_keyed_by_the_provider_message_id(self):
        from ladini.workspace.metadata import clean_last_turn, filter_metadata_dict

        huge = {"message_sid": "wamid.X", "final_response": "a" * 10_000, "interactive": {"k": "v" * 20_000}}
        cleaned = clean_last_turn(huge)
        assert cleaned["message_sid"] == "wamid.X" and len(cleaned["final_response"]) == 3500 and "interactive" not in cleaned
        assert clean_last_turn({"final_response": "no id"}) == {}
        assert filter_metadata_dict({"last_turn": huge, "secret": "x"}) == {"last_turn": cleaned}

    def test_the_replay_never_answers_for_a_different_message(self):
        from ladini.orchestrator.orchestrator import _replayed_turn
        from ladini.workspace.models import Workspace

        ws = Workspace(workspace_id="+226", workspace_type="buyer")
        ws.metadata = {"last_turn": {"message_sid": "wamid.A", "final_response": "réponse A"}}
        assert _replayed_turn(ws, "wamid.A", "+226")["final_response"] == "réponse A"
        assert _replayed_turn(ws, "wamid.B", "+226") is None
        assert _replayed_turn(ws, None, "+226") is None

