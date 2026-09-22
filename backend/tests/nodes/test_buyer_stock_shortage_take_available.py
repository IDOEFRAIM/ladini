"""Rupture de stock partielle côté acheteur — proposer *et reconnaître* la
prise du stock réellement disponible (2026-09-22, incident réel de
production).

## La faille

*poulets* est disponible chez *TEST Producteur* (43 UNITE en stock).
L'acheteur en demande 900 :

    "okay je veux 900"
    -> 📉 Stock insuffisant : 43 UNITE disponible(s) sur 900 UNITE demandé(s)
       🙋 Lancer un appel d'offres ? oui/non
    "nonnn je vais prendre les 43"
    -> Confirmez-vous cette opération pour 900 UNITE UNITE de *poulets* ?

La quantité re-proposée à la confirmation est la quantité PÉRIMÉE (900,
celle qui vient justement d'être refusée), jamais celle que l'acheteur
vient de demander (43). Root cause : seuls deux choix étaient reconnus
pendant ce moment d'attente — CONFIRM (lancer l'appel d'offres) ou REJECT
(abandonner) — un troisième choix, pourtant naturel, n'était géré nulle
part : refuser l'appel d'offres ET prendre ce qui est réellement
disponible. `interpreted_event` sur ce message composé ne matchait ni
CONFIRM ni REJECT, laissant `transaction_payload["quantity"]` (900) survivre
sans y toucher jusqu'à la confirmation finale.

## Le correctif

`services/domain/cart_service.py` mémorise désormais `available_quantity`
en mémoire de travail (vérité déterministe du STOCK réel, jamais à
re-deviner) au moment même où le choix est posé, et le propose
explicitement comme option dans le message. `flows/buyer/procurement.py`
reconnaît alors un nombre nu et non-ambigu dans la réponse comme une
quantité CORRIGÉE, et bascule directement vers `cart_management` avec
cette quantité — sans jamais dupliquer la logique de validation de stock
(déjà correcte et déjà testée en aval)."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.flows.buyer.procurement import (
    buyer_request_resolver,
)
from ladini.graphs.agents.market_coach.services.domain.cart_service import (
    CartDomainService,
)
from tests.conftest import StubRuntime, make_state, run


def rt(responses=None):
    return StubRuntime(responses=responses or {})


def _poulets_vendor():
    return {
        "product_id": "P-POULET",
        "name": "poulets",
        "price": 3500.0,
        "unit": "UNITE",
        "vendor_name": "TEST Producteur",
        "producer_id": "PR-1",
        "source_type": "DIRECT",
        "is_auction": False,
    }


class TestCartServiceStoresTheRealAvailableQuantity:
    def test_insufficient_stock_message_offers_the_available_quantity(self):
        svc = CartDomainService(
            rt(
                responses={
                    "validate_stock_availability_atomic": {
                        "status": "error",
                        "reason": "insufficient_stock",
                        "available_quantity": 43.0,
                        "unit": "UNITE",
                        "message": "Stock insuffisant",
                    }
                }
            )
        )
        result = run(
            svc.add_to_cart_with_ref(
                "+22670000001", "poulets", 900, _poulets_vendor(), [], make_state(),
                buyer_unit="UNITE",
            )
        )
        assert result["status"] == "WAITING_INPUT"
        assert "43" in result["final_response"]
        assert result["working_memory"]["buyer_request_available_quantity"] == 43.0
        assert result["working_memory"]["buyer_request_available_unit"] == "UNITE"
        assert result["working_memory"]["buyer_request_waiting_choice"] is True

    def test_product_not_found_never_stores_an_available_quantity(self):
        """Pas de stock chiffré à proposer quand le produit a simplement
        disparu du catalogue — `available` est `None`, rien à mémoriser."""
        svc = CartDomainService(
            rt(
                responses={
                    "validate_stock_availability_atomic": {
                        "status": "error",
                        "reason": "product_not_found",
                        "message": "Le produit n'existe plus.",
                    }
                }
            )
        )
        result = run(
            svc.add_to_cart_with_ref(
                "+22670000001", "poulets", 900, _poulets_vendor(), [], make_state(),
                buyer_unit="UNITE",
            )
        )
        assert "buyer_request_available_quantity" not in result["working_memory"]


class TestBuyerRequestResolverBridgesToTheAvailableQuantity:
    def _patch_cart_management(self, monkeypatch, result=None):
        import ladini.graphs.agents.market_coach.flows.buyer.procurement as mod

        calls = []

        async def _fake_cart_management(state, mc_runtime):
            calls.append(state)
            return result or {"status": "COMPLETED", "final_response": "ok"}

        monkeypatch.setattr(mod, "cart_management", _fake_cart_management)
        return calls

    def test_the_exact_production_message_bridges_with_the_corrected_quantity(
        self, monkeypatch
    ):
        calls = self._patch_cart_management(monkeypatch)
        state = make_state(
            user_phone="+2260",
            normalized_text="nonnn je vais prendre les 43",
            working_memory={
                "buyer_request_waiting_choice": True,
                "buyer_request_available_quantity": 43.0,
                "buyer_request_available_unit": "UNITE",
            },
            transaction_payload={"product": "poulets", "quantity": 900, "unit": "UNITE"},
        )
        result = run(buyer_request_resolver(state, rt()))

        assert len(calls) == 1, "cart_management aurait dû être appelé exactement une fois"
        bridged_payload = calls[0]["transaction_payload"]
        assert bridged_payload["quantity"] == 43.0, (
            f"la quantité PÉRIMÉE (900) a survécu au lieu de la correction "
            f"(43) : {bridged_payload!r}"
        )
        assert bridged_payload["unit"] == "UNITE"
        assert bridged_payload["product"] == "poulets"
        assert calls[0]["current_goal"] == "BUYER_ADD_TO_CART"
        assert result["final_response"] == "ok"

    def test_a_quantity_lower_than_available_is_also_honored(self):
        """L'acheteur peut vouloir MOINS que le disponible ("je prends 20
        alors") — pas besoin que le nombre corresponde pile à
        `available_quantity`, `cart_management` revalide le stock en aval."""
        import ladini.graphs.agents.market_coach.flows.buyer.procurement as mod

        calls = []

        async def _fake_cart_management(state, mc_runtime):
            calls.append(state)
            return {"status": "COMPLETED"}

        import pytest

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(mod, "cart_management", _fake_cart_management)
        try:
            state = make_state(
                user_phone="+2260",
                normalized_text="je prends 20 alors",
                working_memory={
                    "buyer_request_waiting_choice": True,
                    "buyer_request_available_quantity": 43.0,
                    "buyer_request_available_unit": "UNITE",
                },
                transaction_payload={"product": "poulets", "quantity": 900, "unit": "UNITE"},
            )
            run(buyer_request_resolver(state, rt()))
            assert calls[0]["transaction_payload"]["quantity"] == 20.0
        finally:
            monkeypatch.undo()

    def test_working_memory_waiting_flags_are_cleared_on_bridge(self, monkeypatch):
        calls = self._patch_cart_management(monkeypatch)
        state = make_state(
            user_phone="+2260",
            normalized_text="non je prends les 43",
            working_memory={
                "buyer_request_waiting_choice": True,
                "buyer_request_catalog_checked": True,
                "buyer_request_last_product": "poulets",
                "buyer_request_available_quantity": 43.0,
                "buyer_request_available_unit": "UNITE",
            },
            transaction_payload={"product": "poulets", "quantity": 900, "unit": "UNITE"},
        )
        run(buyer_request_resolver(state, rt()))

        bridged_wm = calls[0]["working_memory"]
        for key in (
            "buyer_request_waiting_choice",
            "buyer_request_catalog_checked",
            "buyer_request_last_product",
            "buyer_request_available_quantity",
            "buyer_request_available_unit",
        ):
            assert bridged_wm[key] is None, key

    def test_confirm_event_still_wins_over_a_bare_number(self, monkeypatch):
        """Priorité inchangée : un "oui" clair pour lancer l'appel d'offres
        ne doit jamais être détourné par cette nouvelle branche, même si le
        message contient par ailleurs un chiffre."""
        calls = self._patch_cart_management(monkeypatch)
        state = make_state(
            user_phone="+2260",
            normalized_text="oui allons-y pour les 43",
            interpreted_event="CONFIRM",
            working_memory={
                "buyer_request_waiting_choice": True,
                "buyer_request_available_quantity": 43.0,
                "buyer_request_available_unit": "UNITE",
            },
            transaction_payload={"product": "poulets", "quantity": 900, "unit": "UNITE"},
        )
        result = run(buyer_request_resolver(state, rt()))
        assert calls == []
        assert result["active_form"] == "AUCTION_CREATE"

    def test_ambiguous_multiple_numbers_falls_back_to_reject(self, monkeypatch):
        """2+ nombres nus dans la réponse = ambigu — jamais un pick arbitraire
        (même discipline que le reste de l'interpréteur) : repli sur le REJECT
        existant plutôt qu'une bascule risquée."""
        calls = self._patch_cart_management(monkeypatch)
        state = make_state(
            user_phone="+2260",
            normalized_text="non, rappelez-moi dans 2 ou 3 jours",
            interpreted_event="REJECT",
            working_memory={
                "buyer_request_waiting_choice": True,
                "buyer_request_available_quantity": 43.0,
                "buyer_request_available_unit": "UNITE",
            },
            transaction_payload={"product": "poulets", "quantity": 900, "unit": "UNITE"},
        )
        result = run(buyer_request_resolver(state, rt()))
        assert calls == []
        assert result["status"] == "COMPLETED"
        assert result["transaction_payload"] == {"__reset__": True}

    def test_no_stored_available_quantity_preserves_the_old_reject_behavior(self):
        """Non-régression : quand `available_quantity` n'a jamais été
        mémorisé (ex: cas `product_not_found`), un chiffre isolé dans une
        réponse REJECT ne doit rien changer au comportement historique."""
        state = make_state(
            user_phone="+2260",
            normalized_text="non, 12 c'est trop tard",
            interpreted_event="REJECT",
            working_memory={"buyer_request_waiting_choice": True},
            transaction_payload={"product": "poulets"},
        )
        result = run(buyer_request_resolver(state, rt()))
        assert result["status"] == "COMPLETED"
        assert result["transaction_payload"] == {"__reset__": True}
