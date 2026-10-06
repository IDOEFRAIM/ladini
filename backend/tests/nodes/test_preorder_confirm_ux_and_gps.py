"""`flows/buyer/preorder.py::create_preorder` — UX confirmation simple
oui/non + gate GPS (voir [[gps-delivery-burkina-faso-2026-08]]), RÉÉCRIT
2026-09-03 pour la migration transactionnelle PREORDER
(`domain/preorder_draft.py` + `flows/buyer/preorder_confirmation.py`) — le
brouillon vit désormais dans une ligne PostgreSQL versionnée (CAS), plus
dans `preorder_workflow` seul. Réutilise le faux moteur SQL de
`test_preorder_draft_persistence.py` (même fidélité, une seule source de
vérité pour la simulation)."""
from __future__ import annotations

from typing import Any, Dict

import pytest

from ladini.core.settings import settings
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.domain.preorder_draft import (
    PreorderDraft,
    PreorderDraftStatus,
)
from ladini.services.database import preorder_draft_store as store_mod
from tests.architecture.test_preorder_draft_persistence import _draft, _install_fake_db
from tests.conftest import make_state, run

_GATE_MODULE = "ladini.graphs.agents.market_coach.flows.buyer.gps_delivery_gate"

CART = [{
    "product_id": "p1", "name": "tomates", "quantity": 55,
    "unit": "KG", "price": 225, "producer_id": "prod1", "status": "ACTIVE",
}]


def _mod():
    import ladini.graphs.agents.market_coach.flows.buyer.preorder as mod
    return mod


class TestPreorderConfirmScreenIsNotRedundant:
    def test_typing_precommander_creates_the_draft_and_asks_a_simple_yes_no(
        self, stub_runtime, monkeypatch
    ):
        _install_fake_db(monkeypatch)
        mod = _mod()
        state = make_state(current_goal="BUYER_PREORDER_INIT", active_cart=CART, preorder_workflow={})
        runtime = stub_runtime(responses={
            "create_preorder_draft": {"status": "success", "preorder_id": "abc123"},
        })

        result = run(mod.create_preorder(state, runtime))

        assert result["status"] == "WAITING_INPUT"
        assert to_tunnel_category(get_pending_interaction(result)) == "CONFIRMATION"
        assert result["response_strategy"] == "CONFIRMATION"
        assert "OUI" in result["final_response"]
        assert "NON" in result["final_response"]
        # Plus de menu numéroté 1/2/3 ni de deuxième récap complet des lignes.
        assert "[1]" not in result["final_response"]
        assert "numéro" not in result["final_response"].lower()
        assert result["preorder_workflow"]["phase"] == "PREORDER_DRAFTED"
        assert not result["preorder_workflow"].get("gps_stage")
        assert result["preorder_draft"]["order_id"] == "abc123"
        assert result["preorder_draft"]["status"] == "DRAFT"
        assert result["preorder_draft"]["version"] == 1
        target = get_pending_interaction(result).target
        assert target == {"draft_id": result["preorder_draft"]["draft_id"], "draft_version": 1}


class TestPreorderConfirmDeviationIsAdaptive:
    def test_a_deviation_at_the_confirm_stage_gets_an_llm_generated_note(self, monkeypatch):
        """Bug réel (2026-08-14) : un écart au "confirmez-vous ?" de la
        précommande rejouait le même écran mot pour mot, quoi que dise
        l'utilisateur. Verrouillé maintenant via `PreorderOutcomeKind.
        DRAFT_UNCHANGED` (même contrat que PROCUREMENT)."""
        _install_fake_db(monkeypatch)
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
        draft = _draft(draft_id="abc123draft", order_id="abc123")
        run(store_mod.insert(draft, conversation_id="+22670000000"))
        target = {"draft_id": draft.draft_id, "draft_version": draft.version}
        state = make_state(
            current_goal="BUYER_PREORDER_INIT",
            user_phone="+22670000000",
            active_cart=CART,
            preorder_draft=draft.to_dict(),
            transaction_payload={},
            interpreted_event="UNKNOWN",
            normalized_text="je vends seulement des intrants patron",
        )
        state["pending_interaction"] = {"kind": "CONFIRM_ACTION", "target": target}

        result = run(mod.create_preorder(state, runtime))

        assert result["final_response"].startswith("Je comprends, laisse-moi t'expliquer")
        assert "OUI" in result["final_response"]


class TestPreorderGpsGate:
    def _confirmed_no_location_state(self, monkeypatch, **overrides: Any) -> Dict[str, Any]:
        """Draft DÉJÀ confirmé (CONFIRM reçu une 1ère fois) mais sans point
        de livraison — l'état RÉEL au moment d'entrer dans le gate GPS. Le
        draft reste `DRAFT` (mandat §20 : aucune mutation métier tant que
        la localisation n'est pas connue)."""
        _install_fake_db(monkeypatch)
        # `claim_once` réel tape Redis — la même clé
        # (`preorder_confirm:abc123draft:1`) est réutilisée par PLUSIEURS
        # tests de cette classe : neutralisé ici pour éviter une collision
        # entre exécutions (même principe que les tests PROCUREMENT).
        import ladini.graphs.agents.market_coach.domain.preorder_draft as pd_mod
        monkeypatch.setattr(pd_mod, "claim_once", lambda key: True)
        draft = _draft(draft_id="abc123draft", order_id="abc123")
        run(store_mod.insert(draft, conversation_id="+22670000000"))
        target = {"draft_id": draft.draft_id, "draft_version": draft.version}
        state = make_state(
            current_goal="BUYER_PREORDER_CONFIRM",
            user_phone="+22670000000",
            active_cart=CART,
            preorder_draft=draft.to_dict(),
            transaction_payload={"resolved_id": "PREORDER_CONFIRM"},
            interpreted_event="CONFIRM",
        )
        state["pending_interaction"] = {"kind": "CONFIRM_ACTION", "target": target}
        state.update(overrides)
        return state

    def test_confirming_the_preorder_with_a_stored_location_offers_reuse(
        self, stub_runtime, monkeypatch
    ):
        mod = _mod()

        async def _fake_get_stored_location(mc_runtime, phone):
            return 12.35, -1.5

        monkeypatch.setattr(f"{_GATE_MODULE}._get_stored_location", _fake_get_stored_location)
        runtime = stub_runtime()

        result = run(mod.create_preorder(self._confirmed_no_location_state(monkeypatch), runtime))

        assert result["status"] == "WAITING_INPUT"
        assert "habituel" in result["final_response"]
        assert result["preorder_workflow"]["gps_stage"] is True
        assert result["preorder_workflow"]["gps_default"] == {"lat": 12.35, "lon": -1.5}
        assert "confirm_preorder_draft" not in runtime.calls
        # Le draft reste DRAFT — aucune mutation tant que le point n'est
        # pas connu (mandat §20).
        assert result["preorder_draft"]["status"] == "DRAFT"

    def test_confirming_the_preorder_without_a_stored_location_asks_to_share_one(
        self, stub_runtime, monkeypatch
    ):
        mod = _mod()

        async def _fake_get_stored_location(mc_runtime, phone):
            return None, None

        monkeypatch.setattr(f"{_GATE_MODULE}._get_stored_location", _fake_get_stored_location)
        runtime = stub_runtime()

        result = run(mod.create_preorder(self._confirmed_no_location_state(monkeypatch), runtime))

        assert result["status"] == "WAITING_INPUT"
        assert "📎" in result["final_response"]
        assert result["preorder_workflow"]["gps_stage"] is True
        assert result["preorder_workflow"]["gps_default"] is None

    def test_confirming_the_habitual_point_at_the_gps_stage_finalizes_with_those_coordinates(
        self, monkeypatch
    ):
        """Chemin escrow (Paydunya) : le point GPS résolu par le gate doit
        être threadé jusqu'à `initiate_escrow_payment`."""
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
            "ladini.graphs.agents.market_coach.services.mcp.gateway.EscrowGateway",
            _CapturingEscrowGateway,
        )
        import ladini.graphs.agents.market_coach.flows.buyer.preorder_confirmation as pc_mod
        monkeypatch.setattr(pc_mod, "EscrowGateway", _CapturingEscrowGateway)

        state = self._confirmed_no_location_state(
            monkeypatch,
            interpreted_event="CONFIRM",
        )
        state["pending_interaction"] = {
            "kind": "PROVIDE_LOCATION",
            "target": state["pending_interaction"]["target"],
        }
        state["preorder_workflow"] = {"gps_default": {"lat": 12.35, "lon": -1.5}}

        result = run(mod.create_preorder(state, None))

        assert result["status"] == "COMPLETED"
        assert seen["delivery_lat"] == 12.35
        assert seen["delivery_lon"] == -1.5
        assert result["preorder_draft"]["status"] == "AWAITING_PAYMENT"

    def test_sharing_a_new_location_at_the_gps_stage_finalizes_with_it(self, monkeypatch):
        """Chemin escrow — voir docstring du test précédent.

        (2026-09-02, refonte GPS) : plus de relecture DB (`_get_stored_
        location`) — `location_outcome`/`location_lat`/`location_lon` sont
        déjà résolus SYNCHRONE côté webhook, avant l'enqueue Celery."""
        monkeypatch.setattr(settings, "ESCROW_PAYMENT_ENABLED", True)
        mod = _mod()
        seen: Dict[str, Any] = {}

        class _CapturingEscrowGateway:
            def __init__(self, rt):
                pass

            async def initiate_escrow_payment(self, **kwargs):
                seen.update(kwargs)
                return {"status": "success", "order_id": "order1", "order_number": "ORD1"}

        import ladini.graphs.agents.market_coach.flows.buyer.preorder_confirmation as pc_mod
        monkeypatch.setattr(pc_mod, "EscrowGateway", _CapturingEscrowGateway)

        state = self._confirmed_no_location_state(monkeypatch)
        state["pending_interaction"] = {
            "kind": "PROVIDE_LOCATION",
            "target": state["pending_interaction"]["target"],
        }
        state["preorder_workflow"] = {"gps_default": None}
        state["location_shared"] = True
        state["location_outcome"] = "NEW_LOCATION_ACCEPTED"
        state["location_lat"] = 13.0
        state["location_lon"] = -1.0
        state["transaction_payload"] = {}

        result = run(mod.create_preorder(state, None))

        assert result["status"] == "COMPLETED"
        assert seen["delivery_lat"] == 13.0
        assert seen["delivery_lon"] == -1.0

    def test_non_escrow_path_still_threads_gps_when_payment_disabled(self, monkeypatch):
        """Non-régression : si `ESCROW_PAYMENT_ENABLED` repasse à False, le
        chemin `confirm_draft` reçoit toujours le point GPS."""
        monkeypatch.setattr(settings, "ESCROW_PAYMENT_ENABLED", False)
        mod = _mod()
        seen: Dict[str, Any] = {}

        class _CapturingGateway:
            def __init__(self, rt):
                pass

            async def confirm_draft(self, **kwargs):
                seen.update(kwargs)
                return {"status": "success", "order_id": "order1", "order_number": "ORD1"}

        import ladini.graphs.agents.market_coach.flows.buyer.preorder_confirmation as pc_mod
        monkeypatch.setattr(pc_mod, "PreorderGateway", _CapturingGateway)

        state = self._confirmed_no_location_state(monkeypatch)
        state["pending_interaction"] = {
            "kind": "PROVIDE_LOCATION",
            "target": state["pending_interaction"]["target"],
        }
        state["preorder_workflow"] = {"gps_default": {"lat": 12.35, "lon": -1.5}}

        result = run(mod.create_preorder(state, None))

        assert result["status"] == "COMPLETED"
        assert seen["delivery_lat"] == 12.35
        assert seen["delivery_lon"] == -1.5
        assert result["preorder_draft"]["status"] == "EXECUTED"

    def test_a_successful_confirmation_sets_the_producer_role_hint(self, monkeypatch):
        """(2026-09-13, incident WhatsApp #3) : `confirm_preorder_draft`
        tourne dans le service MCP à privilège minimal (pas d'accès Redis)
        et ne peut donc que RENVOYER les numéros producteurs notifiés
        (`producer_phones_notified`) — c'est ce nœud, côté worker (qui a
        accès à Redis), qui doit poser l'indice `pending_role_hint:{phone}`
        pour chacun d'eux juste après l'appel MCP réussi."""
        monkeypatch.setattr(settings, "ESCROW_PAYMENT_ENABLED", False)
        mod = _mod()

        class _CapturingGateway:
            def __init__(self, rt):
                pass

            async def confirm_draft(self, **kwargs):
                return {
                    "status": "success",
                    "order_id": "order1",
                    "order_number": "ORD1",
                    "producer_phones_notified": ["+22670000001", "+22670000002"],
                }

        import ladini.graphs.agents.market_coach.flows.buyer.preorder_confirmation as pc_mod
        monkeypatch.setattr(pc_mod, "PreorderGateway", _CapturingGateway)

        hints_set: Dict[str, tuple] = {}
        monkeypatch.setattr(
            pc_mod,
            "_set_role_hint",
            lambda key, value, ttl_seconds: hints_set.__setitem__(
                key, (value, ttl_seconds)
            ),
        )

        state = self._confirmed_no_location_state(monkeypatch)
        state["pending_interaction"] = {
            "kind": "PROVIDE_LOCATION",
            "target": state["pending_interaction"]["target"],
        }
        state["preorder_workflow"] = {"gps_default": {"lat": 12.35, "lon": -1.5}}

        result = run(mod.create_preorder(state, None))

        assert result["status"] == "COMPLETED"
        assert hints_set["pending_role_hint:+22670000001"][0] == "PRODUCER"
        assert hints_set["pending_role_hint:+22670000002"][0] == "PRODUCER"

    def test_free_text_at_the_gps_stage_reminds_to_use_the_gps_button(self, stub_runtime, monkeypatch):
        mod = _mod()
        state = self._confirmed_no_location_state(monkeypatch)
        state["pending_interaction"] = {
            "kind": "PROVIDE_LOCATION",
            "target": state["pending_interaction"]["target"],
        }
        state["preorder_workflow"] = {"gps_default": None}
        state["transaction_payload"] = {}
        runtime = stub_runtime()

        result = run(mod.create_preorder(state, runtime))

        assert result["status"] == "WAITING_INPUT"
        assert "📎" in result["final_response"]
        assert "confirm_preorder_draft" not in runtime.calls

    def test_cancelling_at_the_gps_stage_still_cancels_the_whole_preorder(
        self, stub_runtime, monkeypatch
    ):
        mod = _mod()
        state = self._confirmed_no_location_state(monkeypatch)
        state["pending_interaction"] = {
            "kind": "PROVIDE_LOCATION",
            "target": state["pending_interaction"]["target"],
        }
        state["preorder_workflow"] = {"gps_default": {"lat": 12.35, "lon": -1.5}}
        state["transaction_payload"] = {"resolved_id": "PREORDER_CANCEL"}
        runtime = stub_runtime()

        result = run(mod.create_preorder(state, runtime))

        assert result["status"] == "COMPLETED"
        assert result["preorder_workflow"]["phase"] == "CART"
        assert "confirm_preorder_draft" not in runtime.calls
        assert result["preorder_draft"]["status"] == "CANCELLED"
        # Le panier reste disponible (mandat §... préservation, pas de stock
        # débité) — pas vidé par l'annulation.
        assert "annulée" in result["final_response"].lower()
