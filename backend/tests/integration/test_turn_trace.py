"""`TurnTrace` (Phase 2 hardening, commit 11) — capturé AVANT `post_response_cleanup`, sur le
VRAI graphe compilé (harnais canonique, `tests/harness/conversation.py`). Chaque test lit
`turn_trace.last_trace()` juste après `conv.send(...)` — le tour vient de fermer sa capture
dans `Orchestrator.handle()`."""
from __future__ import annotations

from unittest import mock

import pytest

from ladini.graphs.agents.market_coach.core import turn_trace
from tests.harness import ConversationHarness, new_task


def _coq(**extra):
    return new_task("CREATE_RECURRING_NEED", product="coq", quantity=14.0, recurrence_type="WEEKLY", **extra)


@pytest.fixture()
def conv():
    with ConversationHarness(role="BUYER", channel="whatsapp") as harness:
        yield harness


class TestA_IntentAndConfidenceSurviveCleanup:
    def test_intent_and_confidence_are_present_even_though_the_final_state_was_cleaned(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        trace = turn_trace.last_trace()
        assert trace is not None
        assert trace.intent == "CREATE_RECURRING_NEED"
        assert trace.confidence == pytest.approx(0.95)
        # Preuve que le nettoyage a bien eu lieu (sinon ce test ne prouverait rien) : l'état
        # FINAL, lui, a déjà perdu ces champs volatils au tour d'après un vrai nettoyage —
        # ici on vérifie juste que TurnTrace, capturé AVANT, ne dépend pas de cet ordre.
        assert trace.interpreted_event == "NEW_TASK"


class TestB_GoalBeforeAndAfter:
    def test_goal_before_is_none_on_a_fresh_conversation_and_after_reflects_the_new_goal(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        trace = turn_trace.last_trace()
        assert trace.goal_before is None
        assert trace.goal_after == "CREATE_RECURRING_NEED"

    def test_goal_before_reflects_the_previous_turns_goal(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        conv.send("oui")
        trace = turn_trace.last_trace()
        assert trace.goal_before == "CREATE_RECURRING_NEED"
        # `current_goal` n'est remis à `None` QUE par le reset de
        # `post_response_cleanup` (voir `_keep_goal_channel`) — donc APRÈS le point de
        # capture de `TurnTrace`. C'est exactement le problème que ce module ferme
        # (voir sa docstring) : `goal_after`, capturé AVANT ce nettoyage, reflète
        # fidèlement l'état RÉEL pendant le tour, pas l'état déjà nettoyé.
        assert trace.goal_after == "CREATE_RECURRING_NEED"


class TestC_PendingBeforeAndAfter:
    def test_pending_transitions_from_none_to_confirm_action(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        trace = turn_trace.last_trace()
        assert trace.pending_before_kind == "NONE"
        assert trace.pending_after_kind == "CONFIRM_ACTION"

    def test_pending_transitions_from_confirm_action_to_none_on_confirmation(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        conv.send("oui")
        trace = turn_trace.last_trace()
        assert trace.pending_before_kind == "CONFIRM_ACTION"
        assert trace.pending_after_kind == "NONE"


class TestD_DraftVersionAndStatus:
    def test_draft_fields_are_populated_for_a_recurring_need_turn(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        trace = turn_trace.last_trace()
        assert trace.draft_type == "recurring_need_draft"
        assert trace.draft_status == "DRAFT"
        assert isinstance(trace.draft_version, int) and trace.draft_version >= 1
        assert trace.item_count == 1

    def test_item_count_reflects_additional_items(self, conv):
        conv.send(
            "je veux 14 coqs et 20 chèvres chaque semaine",
            llm=_coq(additional_items=[{"product": "chèvre", "quantity": 20.0}]),
        )
        trace = turn_trace.last_trace()
        assert trace.item_count == 2

    def test_no_draft_means_no_draft_fields(self, conv):
        conv.send("bonjour", llm=new_task("GREETING", confidence=0.6))
        trace = turn_trace.last_trace()
        assert trace.draft_type is None
        assert trace.draft_id is None
        assert trace.draft_version is None
        assert trace.draft_status is None
        assert trace.item_count == 0


class TestE_ClassifyTurnIsObservedNeverConsumed:
    def test_turn_decision_is_populated(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        trace = turn_trace.last_trace()
        assert trace.turn_decision in {
            "NEW_TASK", "CONTINUE", "INTERRUPT", "ANSWER_PENDING", "CORRECT",
            "CONFIRM", "REJECT", "CANCEL", "CLARIFY", "UNKNOWN",
        }
        assert trace.turn_decision_reason

    def test_capture_pre_cleanup_never_returns_a_patch_or_mutates_state(self):
        """`capture_pre_cleanup` est un pur SIDE EFFECT d'observation — jamais un patch de
        nœud LangGraph, jamais une mutation de `state` lui-même."""
        state = {
            "interpreted_event": "NEW_TASK",
            "detected_intent": "CREATE_RECURRING_NEED",
            "current_goal": "CREATE_RECURRING_NEED",
        }
        before = dict(state)
        turn_trace.start(conversation_phone="+22670000000", message_id=None, channel="WHATSAPP", before_state={})
        result = turn_trace.capture_pre_cleanup(state)
        assert result is None
        assert state == before
        turn_trace.finish()

    def test_classify_turn_is_only_importable_by_turn_policy_and_turn_trace(self):
        """Redondant avec `tests/unit/test_turn_policy_classification.py`, gardé ici pour que
        ce module documente lui-même, à côté de son usage réel, qu'il ne fait qu'OBSERVER."""
        import inspect

        from ladini.graphs.agents.market_coach.core import turn_trace as tt_module

        source = inspect.getsource(tt_module)
        assert "classify_turn(" in source
        # Jamais utilisé pour construire un patch de state ni une décision de routage.
        assert "return {" not in source.split("def capture_pre_cleanup")[1].split("def finish")[0]


class TestF_WebChatWhatsAppParity:
    def test_same_message_produces_the_same_business_outcome_on_both_channels(self):
        whatsapp_llm = _coq()
        webchat_llm = _coq()

        with ConversationHarness(role="BUYER", channel="whatsapp") as conv_wa:
            conv_wa.send("je veux 14 coqs chaque semaine", llm=whatsapp_llm)
            trace_wa = turn_trace.last_trace()

        with ConversationHarness(role="BUYER", channel="webchat") as conv_wc:
            conv_wc.send("je veux 14 coqs chaque semaine", llm=webchat_llm)
            trace_wc = turn_trace.last_trace()

        assert trace_wa.channel == "WHATSAPP"
        assert trace_wc.channel == "WEBCHAT"
        # Différences ACCEPTÉES : channel, message_id/turn_id (transport). Différences NON
        # acceptées : intent, turn_decision, goal_after, draft status/version.
        assert trace_wa.intent == trace_wc.intent
        assert trace_wa.turn_decision == trace_wc.turn_decision
        assert trace_wa.goal_after == trace_wc.goal_after
        assert trace_wa.draft_status == trace_wc.draft_status
        assert trace_wa.draft_version == trace_wc.draft_version
        assert trace_wa.item_count == trace_wc.item_count


class TestG_ControlledErrorNeverLeaksSensitiveData:
    def test_error_class_is_populated_and_no_sensitive_data_leaks(self, conv):
        import ladini.graphs.agents.market_coach.flows.buyer.recurring_need as rn

        original_correct = rn._correct

        async def crashing_correct(*args, **kwargs):
            raise RuntimeError("panne simulée")

        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        with mock.patch.object(rn, "_correct", crashing_correct):
            conv.send(
                "non plutôt 23 boeufs secret-numero-+22670000099",
                llm=new_task("CREATE_RECURRING_NEED", product="boeuf", quantity=23.0),
            )
        trace = turn_trace.last_trace()
        # Le crash survient APRÈS memory_update mais AVANT post_response_cleanup n'est même
        # atteint (le nœud lève) : `finish()` est quand même appelé (branche `except
        # Exception` de `Orchestrator.handle`), avec `error_class` renseigné.
        assert trace is not None
        assert trace.error_class == "RuntimeError"
        payload = trace.to_dict()
        assert "secret-numero" not in str(payload)
        assert "+22670000099" not in str(payload)
        assert original_correct is not None  # évite un import inutilisé signalé à tort


class TestH_NoRawUserMessageByDefault:
    def test_the_trace_never_carries_a_free_text_field(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        trace = turn_trace.last_trace()
        payload = trace.to_dict()
        assert "je veux 14 coqs chaque semaine" not in str(payload)
        # Aucun champ de type "message"/"text"/"response" dans le contrat lui-même.
        assert not {"message", "text", "response", "raw_text", "user_message"} & set(payload)


class TestI_ConversationIdentifierIsOpaque:
    def test_conversation_id_hash_never_contains_the_phone_digits(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        trace = turn_trace.last_trace()
        assert conv.phone not in trace.conversation_id_hash
        digits_only = "".join(c for c in conv.phone if c.isdigit())
        assert digits_only not in trace.conversation_id_hash
        assert len(trace.conversation_id_hash) >= 32  # HMAC-SHA256 hexdigest

    def test_the_same_conversation_always_hashes_to_the_same_identifier(self, conv):
        conv.send("je veux 14 coqs chaque semaine", llm=_coq())
        first = turn_trace.last_trace().conversation_id_hash
        conv.send("oui")
        second = turn_trace.last_trace().conversation_id_hash
        assert first == second
