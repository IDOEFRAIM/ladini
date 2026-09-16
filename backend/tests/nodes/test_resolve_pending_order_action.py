"""`_resolve_pending_order_action` / `_finalize_pending_order_action`
(2026-09-15) — tunnel auto-suffisant fusionnant PRODUCER_CONFIRM_ORDER/
PRODUCER_CANCEL_ORDER.

Contexte : un premier correctif (label enrichi côté intent.py) puis un
second (signal d'état donné en CONTEXTE au prompt LLM) se sont tous deux
avérés insuffisants pour qu'un "confirmer"/"annuler" tapé NU (sans numéro,
sans phrase) juste après la liste des ventes soit fiablement classifié.
Ce résolveur remplace la dépendance à une classification LLM libre par un
verrou déterministe : `flows/buyer/order_tracking.py::list_orders` pose
`working_memory["pending_order_action_id"/"pending_order_action_phase"]`
PROACTIVEMENT, et ce résolveur lit ensuite `event` (CONFIRM/REJECT) —
jamais le nom du goal — pour choisir entre confirmer et annuler, deux
actions RÉELLEMENT différentes (`confirm_order_by_producer` vs
`cancel_confirmed_order`)."""
from __future__ import annotations

from tests.conftest import StubRuntime, run

from ladini.graphs.agents.market_coach.flows.producer.flow import (
    _resolve_pending_order_action,
)


def rt(responses=None):
    return StubRuntime(responses=responses or {})


class TestAlreadyLockedPhaseReadsEventNotGoal:
    """Une fois verrouillé (phase CONFIRM + order_id déjà connu), l'action
    réellement exécutée dépend de `event` — jamais de `goal` — c'est le
    point central de la fusion des deux résolveurs."""

    def test_confirm_event_calls_confirm_order_by_producer(self):
        runtime = rt(
            {
                "confirm_order_by_producer": {
                    "status": "success",
                    "message": "✅ Commande confirmée.",
                }
            }
        )
        working = {
            "pending_order_action_id": "order-1",
            "pending_order_action_phase": "CONFIRM",
        }
        result = run(
            _resolve_pending_order_action(
                runtime,
                "+22670000001",
                {},
                working,
                "CONFIRM",
                "PRODUCER_CONFIRM_ORDER",
            )
        )
        assert result["status"] == "COMPLETED"
        assert result["response_strategy"] == "SUCCESS"
        assert "confirmée" in result["final_response"]
        assert result["working_memory"]["pending_order_action_id"] is None
        assert result["working_memory"]["pending_order_action_phase"] is None

    def test_reject_event_calls_cancel_confirmed_order_even_though_goal_says_confirm(self):
        """Le goal verrouillé peut rester "PRODUCER_CONFIRM_ORDER" (peu
        importe lequel des deux a été initialement classé) — c'est `event`
        qui décide, jamais le nom du goal."""
        runtime = rt(
            {
                "cancel_confirmed_order": {
                    "status": "success",
                    "message": "❌ Commande annulée.",
                }
            }
        )
        working = {
            "pending_order_action_id": "order-1",
            "pending_order_action_phase": "CONFIRM",
        }
        result = run(
            _resolve_pending_order_action(
                runtime,
                "+22670000001",
                {},
                working,
                "REJECT",
                "PRODUCER_CONFIRM_ORDER",
            )
        )
        assert result["status"] == "COMPLETED"
        assert result["response_strategy"] == "SUCCESS"
        assert "annulée" in result["final_response"]
        assert result["working_memory"]["pending_order_action_id"] is None

    def test_ambiguous_text_re_shows_the_reminder_without_losing_the_lock(self):
        runtime = rt()
        working = {
            "pending_order_action_id": "order-1",
            "pending_order_action_phase": "CONFIRM",
        }
        result = run(
            _resolve_pending_order_action(
                runtime,
                "+22670000001",
                {},
                working,
                "UNKNOWN",
                "PRODUCER_CONFIRM_ORDER",
            )
        )
        assert result["status"] == "WAITING_INPUT"
        assert result["working_memory"]["pending_order_action_id"] == "order-1"
        assert result["working_memory"]["pending_order_action_phase"] == "CONFIRM"

    def test_backend_error_still_clears_the_lock(self):
        runtime = rt(
            {
                "confirm_order_by_producer": {
                    "status": "error",
                    "message": "Commande déjà traitée.",
                }
            }
        )
        working = {
            "pending_order_action_id": "order-1",
            "pending_order_action_phase": "CONFIRM",
        }
        result = run(
            _resolve_pending_order_action(
                runtime,
                "+22670000001",
                {},
                working,
                "CONFIRM",
                "PRODUCER_CONFIRM_ORDER",
            )
        )
        assert result["status"] == "COMPLETED"
        assert result["response_strategy"] == "ERROR"
        assert result["working_memory"]["pending_order_action_id"] is None


class TestNotYetLockedResolvesWhichOrderFirst:
    def test_no_candidate_returns_the_existing_clean_error(self):
        runtime = rt({"get_producer_orders": {"status": "success", "data": []}})
        result = run(
            _resolve_pending_order_action(
                runtime, "+22670000001", {}, {}, "NEW_TASK", "PRODUCER_CONFIRM_ORDER"
            )
        )
        assert result["status"] == "ERROR"

    def test_single_candidate_locks_and_shows_the_recap(self):
        runtime = rt(
            {
                "get_producer_orders": {
                    "status": "success",
                    "data": [
                        {
                            "order_id": "order-1",
                            "reference": "ORDER-1",
                            "status": "PENDING_PRODUCER_CONFIRMATION",
                            "total_amount": 5000.0,
                            "currency": "XOF",
                            "buyer_name": "Acheteur",
                        }
                    ],
                }
            }
        )
        result = run(
            _resolve_pending_order_action(
                runtime, "+22670000001", {}, {}, "NEW_TASK", "PRODUCER_CONFIRM_ORDER"
            )
        )
        assert result["status"] == "WAITING_INPUT"
        assert result["current_goal"] == "PRODUCER_CONFIRM_ORDER"
        assert result["working_memory"]["pending_order_action_id"] == "order-1"
        assert result["working_memory"]["pending_order_action_phase"] == "CONFIRM"

    def test_no_phone_is_a_clean_error(self):
        result = run(
            _resolve_pending_order_action(
                rt(), "", {}, {}, "NEW_TASK", "PRODUCER_CONFIRM_ORDER"
            )
        )
        assert result["status"] == "ERROR"
