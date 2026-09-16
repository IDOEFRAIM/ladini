"""`_bare_confirmation_for_pending_producer_order`
(interpreter/routing.py, 2026-09-15).

Contexte : un producteur reçoit une notification PROACTIVE hors-
conversation ("Nouvelle commande — confirmation requise... Tapez
*confirmer*... ou *annuler*...", `workers/outbox/templates.py::
_render_preorder_confirmed_producer`) — aucun tour de conversation n'a eu
lieu pour verrouiller un `PendingInteraction`, contrairement à
`flows/buyer/order_tracking.py::list_orders`. Deux correctifs précédents
(label enrichi, puis signal d'état donné en CONTEXTE au prompt LLM) se sont
avérés insuffisants pour ce cas — remplacés ici par une vérification
déterministe et BORNÉE : jamais un résultat deviné, seulement une
résolution quand la situation est sans ambiguïté (exactement une vente en
attente)."""
from __future__ import annotations

from tests.conftest import StubRuntime, run

from ladini.graphs.agents.market_coach.interpreter.routing import (
    _bare_confirmation_for_pending_producer_order,
)


def rt(responses=None):
    return StubRuntime(responses=responses or {})


class TestBareConfirmationResolvesOnlyWhenUnambiguous:
    def test_confirmer_with_exactly_one_pending_order_resolves_to_confirm_goal(self):
        runtime = rt(
            {
                "get_producer_orders": {
                    "status": "success",
                    "data": [{"order_id": "order-1"}],
                }
            }
        )
        result = run(
            _bare_confirmation_for_pending_producer_order(
                runtime, "+22670000001", "confirmer"
            )
        )
        assert result is not None
        assert result["interpreted_event"] == "NEW_TASK"
        assert result["detected_intent"] == "PRODUCER_CONFIRM_ORDER"
        assert result["extracted_entities"] == {}

    def test_annuler_with_exactly_one_pending_order_resolves_to_cancel_goal(self):
        runtime = rt(
            {
                "get_producer_orders": {
                    "status": "success",
                    "data": [{"order_id": "order-1"}],
                }
            }
        )
        result = run(
            _bare_confirmation_for_pending_producer_order(
                runtime, "+22670000001", "annuler"
            )
        )
        assert result is not None
        assert result["detected_intent"] == "PRODUCER_CANCEL_ORDER"

    def test_no_pending_order_defers_to_normal_classification(self):
        runtime = rt(
            {"get_producer_orders": {"status": "success", "data": []}}
        )
        result = run(
            _bare_confirmation_for_pending_producer_order(
                runtime, "+22670000001", "confirmer"
            )
        )
        assert result is None

    def test_multiple_pending_orders_never_guesses_which_one(self):
        runtime = rt(
            {
                "get_producer_orders": {
                    "status": "success",
                    "data": [{"order_id": "order-1"}, {"order_id": "order-2"}],
                }
            }
        )
        result = run(
            _bare_confirmation_for_pending_producer_order(
                runtime, "+22670000001", "confirmer"
            )
        )
        assert result is None

    def test_a_non_confirmation_word_never_triggers_a_db_lookup(self):
        # Aucune réponse stubée pour get_producer_orders : si le code
        # l'appelait quand même, StubRuntime lèverait — la réussite du test
        # prouve que l'appel n'a jamais eu lieu.
        runtime = rt()
        result = run(
            _bare_confirmation_for_pending_producer_order(
                runtime, "+22670000001", "bonjour"
            )
        )
        assert result is None

    def test_no_phone_defers_to_normal_classification(self):
        runtime = rt(
            {
                "get_producer_orders": {
                    "status": "success",
                    "data": [{"order_id": "order-1"}],
                }
            }
        )
        result = run(
            _bare_confirmation_for_pending_producer_order(runtime, "", "confirmer")
        )
        assert result is None

    def test_a_gateway_error_defers_to_normal_classification_rather_than_raising(self):
        class RaisingRuntime:
            llm = None

            def __getattr__(self, name):
                raise RuntimeError("boom")

        result = run(
            _bare_confirmation_for_pending_producer_order(
                RaisingRuntime(), "+22670000001", "confirmer"
            )
        )
        assert result is None
