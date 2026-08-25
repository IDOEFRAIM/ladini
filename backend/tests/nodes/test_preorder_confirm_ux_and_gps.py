"""`flows/buyer/preorder.py::create_preorder` — deux correctifs demandés par
l'utilisateur après un test WhatsApp réel (2026-08-13) :

1. UX redondante : le panier affichait déjà items+total en demandant de
   taper *précommander*, puis le récap de précommande réaffichait la même
   chose en demandant de choisir 1/2/3. Fusionné en un seul écran de
   confirmation simple oui/non (plus de menu numéroté ni de double récap).
2. Aucune géolocalisation n'était demandée avant de confirmer une
   précommande — contrairement au flux gagnant d'enchère. Le même gate GPS
   2 étapes (`_GPS_HABITUAL_PROMPT`/`_GPS_FIRST_TIME_PROMPT`/`_GPS_TEXT_REMINDER`)
   est maintenant inséré juste avant `confirm_preorder_draft`.

Voir [[gps-delivery-burkina-faso-2026-08]]."""
from __future__ import annotations

from typing import Any, Dict

from agriconnect.core.settings import settings
from tests.conftest import make_state, run

_GATE_MODULE = "agriconnect.graphs.agents.market_coach.flows.buyer.gps_delivery_gate"

CART = [{
    "product_id": "p1", "name": "tomates", "quantity": 55,
    "unit": "KG", "price": 225, "producer_id": "prod1", "status": "ACTIVE",
}]


def _mod():
    import agriconnect.graphs.agents.market_coach.flows.buyer.preorder as mod
    return mod


class TestPreorderConfirmScreenIsNotRedundant:
    def test_typing_precommander_creates_the_draft_and_asks_a_simple_yes_no(self, stub_runtime):
        mod = _mod()
        state = make_state(
            current_goal="BUYER_PREORDER_INIT",
            active_cart=CART,
            preorder_workflow={},
        )
        runtime = stub_runtime(responses={
            "create_preorder_draft": {"status": "success", "preorder_id": "abc123"},
        })

        result = run(mod.create_preorder(state, runtime))

        assert result["status"] == "WAITING_INPUT"
        assert result["expected_input"] == "CONFIRMATION"
        assert result["response_strategy"] == "ASK_MISSING_FIELD"
        assert "OUI" in result["final_response"]
        assert "NON" in result["final_response"]
        # Plus de menu numéroté 1/2/3 ni de deuxième récap complet des lignes.
        assert "[1]" not in result["final_response"]
        assert "numéro" not in result["final_response"].lower()
        assert result["preorder_workflow"]["phase"] == "PREORDER_DRAFTED"
        assert not result["preorder_workflow"].get("gps_stage")
        assert "pending_menu" not in result


class TestPreorderConfirmDeviationIsAdaptive:
    def test_a_deviation_at_the_confirm_stage_gets_an_llm_generated_note(self):
        """Bug réel (2026-08-14) : un écart au "confirmez-vous ?" de la
        précommande rejouait le même écran mot pour mot, quoi que dise
        l'utilisateur. Voir [[precommande-architecture-consolidation-2026-08]]."""
        mod = _mod()

        class _Msg:
            content = "Je comprends, laisse-moi t'expliquer avant de continuer."

        class _Choice:
            message = _Msg()

        class _Completion:
            choices = [_Choice()]

        class _StubLLM:
            @property
            def chat(self):
                return self

            @property
            def completions(self):
                return self

            def create(self, **kwargs):
                return _Completion()

        from tests.conftest import StubRuntime
        runtime = StubRuntime(llm=_StubLLM())
        state = make_state(
            # `BUYER_PREORDER_INIT` (pas `_CONFIRM`) : sinon `create_preorder`
            # force resolved_id="PREORDER_CONFIRM" quel que soit le texte
            # (voir le goal-based short-circuit en tête de la fonction) et le
            # test ne testerait plus du tout la branche d'écart.
            current_goal="BUYER_PREORDER_INIT",
            active_cart=CART,
            preorder_workflow={"phase": "PREORDER_DRAFTED", "preorder_id": "abc123"},
            transaction_payload={},
            normalized_text="je vends seulement des intrants patron",
        )

        result = run(mod.create_preorder(state, runtime))

        assert result["final_response"].startswith("Je comprends, laisse-moi t'expliquer")
        assert "OUI" in result["final_response"]


class TestPreorderGpsGate:
    def _drafted_state(self, **extra_flow: Any) -> Dict[str, Any]:
        flow = {"phase": "PREORDER_DRAFTED", "preorder_id": "abc123", **extra_flow}
        return make_state(
            current_goal="BUYER_PREORDER_CONFIRM",
            active_cart=CART,
            preorder_workflow=flow,
            transaction_payload={"resolved_id": "PREORDER_CONFIRM"},
        )

    def test_confirming_the_preorder_with_a_stored_location_offers_reuse(self, stub_runtime, monkeypatch):
        mod = _mod()

        async def _fake_get_stored_location(mc_runtime, phone):
            return 12.35, -1.5

        monkeypatch.setattr(f"{_GATE_MODULE}._get_stored_location", _fake_get_stored_location)
        runtime = stub_runtime()

        result = run(mod.create_preorder(self._drafted_state(), runtime))

        assert result["status"] == "WAITING_INPUT"
        assert "habituel" in result["final_response"]
        assert result["preorder_workflow"]["gps_stage"] is True
        assert result["preorder_workflow"]["gps_default"] == {"lat": 12.35, "lon": -1.5}
        assert "confirm_preorder_draft" not in runtime.calls

    def test_confirming_the_preorder_without_a_stored_location_asks_to_share_one(self, stub_runtime, monkeypatch):
        mod = _mod()

        async def _fake_get_stored_location(mc_runtime, phone):
            return None, None

        monkeypatch.setattr(f"{_GATE_MODULE}._get_stored_location", _fake_get_stored_location)
        runtime = stub_runtime()

        result = run(mod.create_preorder(self._drafted_state(), runtime))

        assert result["status"] == "WAITING_INPUT"
        assert "📎" in result["final_response"]
        assert result["preorder_workflow"]["gps_stage"] is True
        assert result["preorder_workflow"]["gps_default"] is None

    def test_confirming_the_habitual_point_at_the_gps_stage_finalizes_with_those_coordinates(self, monkeypatch):
        """Ce test verrouille spécifiquement le chemin escrow (Paydunya) : le
        point GPS résolu par le gate doit être threadé jusqu'à
        `initiate_escrow_payment`, pas seulement jusqu'à l'ancien
        `confirm_draft` non-escrow. `ESCROW_PAYMENT_ENABLED` est désactivé par
        défaut depuis 2026-08-24 (voir .env) — on l'épingle donc à True ici
        pour tester ce chemin précis, indépendamment du défaut ambiant. Voir
        [[gps-delivery-burkina-faso-2026-08]]."""
        monkeypatch.setattr(settings, "ESCROW_PAYMENT_ENABLED", True)
        mod = _mod()
        seen: Dict[str, Any] = {}

        class _CapturingEscrowGateway:
            def __init__(self, rt):
                pass

            async def initiate_escrow_payment(self, **kwargs):
                seen.update(kwargs)
                return {"status": "success", "order_id": "order1", "order_number": "ORD1"}

        monkeypatch.setattr(
            "agriconnect.graphs.agents.market_coach.services.mcp.gateway.EscrowGateway",
            _CapturingEscrowGateway,
        )

        state = self._drafted_state(gps_stage=True, gps_default={"lat": 12.35, "lon": -1.5})
        result = run(mod.create_preorder(state, None))

        assert result["status"] == "COMPLETED"
        assert seen["delivery_lat"] == 12.35
        assert seen["delivery_lon"] == -1.5

    def test_sharing_a_new_location_at_the_gps_stage_rereads_the_profile_and_finalizes(self, monkeypatch):
        """Chemin escrow — voir docstring du test précédent."""
        monkeypatch.setattr(settings, "ESCROW_PAYMENT_ENABLED", True)
        mod = _mod()
        seen: Dict[str, Any] = {}

        class _CapturingEscrowGateway:
            def __init__(self, rt):
                pass

            async def initiate_escrow_payment(self, **kwargs):
                seen.update(kwargs)
                return {"status": "success", "order_id": "order1", "order_number": "ORD1"}

        async def _fake_get_stored_location(mc_runtime, phone):
            return 13.0, -1.0

        monkeypatch.setattr(
            "agriconnect.graphs.agents.market_coach.services.mcp.gateway.EscrowGateway",
            _CapturingEscrowGateway,
        )
        monkeypatch.setattr(f"{_GATE_MODULE}._get_stored_location", _fake_get_stored_location)

        state = self._drafted_state(gps_stage=True, gps_default=None)
        state["location_shared"] = True
        state["transaction_payload"] = {}

        result = run(mod.create_preorder(state, None))

        assert result["status"] == "COMPLETED"
        assert seen["delivery_lat"] == 13.0
        assert seen["delivery_lon"] == -1.0

    def test_non_escrow_path_still_threads_gps_when_payment_disabled(self, monkeypatch):
        """Non-régression : si `ESCROW_PAYMENT_ENABLED` repasse à False un
        jour (Paydunya rebloqué), l'ancien chemin `confirm_draft` reçoit
        toujours le point GPS — comportement historique préservé, pas
        seulement le nouveau chemin escrow."""
        from agriconnect.core.settings import settings

        monkeypatch.setattr(settings, "ESCROW_PAYMENT_ENABLED", False)
        mod = _mod()
        seen: Dict[str, Any] = {}

        class _CapturingGateway:
            def __init__(self, rt):
                pass

            async def confirm_draft(self, **kwargs):
                seen.update(kwargs)
                return {"status": "success", "order_id": "order1", "order_number": "ORD1"}

        monkeypatch.setattr(mod, "PreorderGateway", _CapturingGateway)

        state = self._drafted_state(gps_stage=True, gps_default={"lat": 12.35, "lon": -1.5})
        result = run(mod.create_preorder(state, None))

        assert result["status"] == "COMPLETED"
        assert seen["delivery_lat"] == 12.35
        assert seen["delivery_lon"] == -1.5

    def test_free_text_at_the_gps_stage_reminds_to_use_the_gps_button(self, stub_runtime):
        mod = _mod()
        state = self._drafted_state(gps_stage=True, gps_default=None)
        state["transaction_payload"] = {}
        runtime = stub_runtime()

        result = run(mod.create_preorder(state, runtime))

        assert result["status"] == "WAITING_INPUT"
        assert "📎" in result["final_response"]
        assert "confirm_preorder_draft" not in runtime.calls

    def test_cancelling_at_the_gps_stage_still_cancels_the_whole_preorder(self, stub_runtime):
        mod = _mod()
        state = self._drafted_state(gps_stage=True, gps_default={"lat": 12.35, "lon": -1.5})
        state["transaction_payload"] = {"resolved_id": "PREORDER_CANCEL"}
        runtime = stub_runtime()

        result = run(mod.create_preorder(state, runtime))

        assert result["status"] == "COMPLETED"
        assert result["preorder_workflow"]["phase"] == "CART"
        assert "confirm_preorder_draft" not in runtime.calls
