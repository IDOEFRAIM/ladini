"""`domain/preorder_draft.py::build_response_plan` — le message explicatif
d'un échec de confirmation était INATTEIGNABLE (2026-09-13, incident WhatsApp
#4, découvert en testant le correctif producteur/double-rôle).

## Le bug fermé

`PREORDER_FAILED`/`PREORDER_PAYMENT_FAILED`/`PREORDER_PAYMENT_EXPIRED`
posaient chacun un `final_response` explicatif ("Impossible de confirmer
votre précommande — aucun stock n'a été débité...") MAIS avec
`response_strategy="ERROR"`. `nodes/response_handlers.py::_select_handler`
route TOUTE stratégie "ERROR" vers `render_error`
(nodes/rendering/feedback.py), qui reconstruit TOUJOURS son propre texte
depuis `state["validation_errors"]` (jamais posé par ce flux) — écrasant
systématiquement le message explicatif ci-dessus par le générique "❌ Une
erreur technique est survenue. Réessayez dans un instant.", peu importe la
qualité du message d'origine. Un buyer relançant la confirmation d'une
précommande déjà traitée (ou dont le stock a changé) recevait donc un
message inutile au lieu d'une explication actionnable.

Fix : `response_strategy="SUCCESS"` (avec `graph_status="COMPLETED"`,
inchangé) — exactement le même mécanisme déjà utilisé, dans cette même
fonction, par `PREORDER_EXECUTION_UNKNOWN` (une autre issue terminale mais
pas pleinement réussie) : `render_success` RÉUTILISE le `final_response`
déjà posé quand aucun `execution_result` n'est présent (le cas de ce nœud
buyer, hors `mcp_tool_executor`)."""
from __future__ import annotations

import pytest

from tests.conftest import run

from ladini.graphs.agents.market_coach.domain.preorder_draft import (
    PreorderDraft,
    PreorderDraftStatus,
    PreorderOutcome,
    PreorderOutcomeKind,
    build_response_plan,
)
from ladini.graphs.agents.market_coach.nodes.response_handlers import (
    final_response as final_response_node,
)


def _draft(status: PreorderDraftStatus) -> PreorderDraft:
    return PreorderDraft(
        draft_id="draft1",
        version=2,
        status=status,
        order_id="order1",
        buyer_phone="+22670000001",
        total_amount=1000.0,
        currency="XOF",
    )


class TestBuildResponsePlanUsesSuccessStrategyForTerminalFailures:
    @pytest.mark.parametrize(
        "kind,expected_status,expected_snippet",
        [
            (
                PreorderOutcomeKind.PREORDER_FAILED,
                PreorderDraftStatus.FAILED,
                "Impossible de confirmer votre précommande",
            ),
            (
                PreorderOutcomeKind.PREORDER_PAYMENT_FAILED,
                PreorderDraftStatus.PAYMENT_FAILED,
                "n'a pas abouti",
            ),
            (
                PreorderOutcomeKind.PREORDER_PAYMENT_EXPIRED,
                PreorderDraftStatus.PAYMENT_EXPIRED,
                "délai de paiement",
            ),
        ],
    )
    def test_the_explanatory_message_uses_success_strategy_not_error(
        self, kind, expected_status, expected_snippet
    ):
        outcome = PreorderOutcome(kind=kind, draft=_draft(expected_status))
        plan = build_response_plan(outcome)

        assert plan.response_strategy == "SUCCESS"
        assert plan.graph_status == "COMPLETED"
        assert expected_snippet in plan.final_response


class TestTheExplanatoryMessageSurvivesTheRenderDispatch:
    """Reproduit le bug de bout en bout : sans le correctif, ce test échoue
    (le texte reçu par l'utilisateur devient le générique de `render_error`,
    pas le message explicatif du domaine)."""

    def test_a_preorder_failed_message_reaches_the_user_verbatim(self):
        outcome = PreorderOutcome(
            kind=PreorderOutcomeKind.PREORDER_FAILED,
            draft=_draft(PreorderDraftStatus.FAILED),
        )
        plan = build_response_plan(outcome)

        state = {
            "response_strategy": plan.response_strategy,
            "status": plan.graph_status,
            "final_response": plan.final_response,
            "ag_ui_component": None,
            "execution_result": None,
        }

        result = run(final_response_node(state, mc_runtime=None))

        assert result["final_response"] == plan.final_response
        assert "erreur technique" not in result["final_response"].lower()
