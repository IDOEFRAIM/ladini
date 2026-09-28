"""Base du prix ↔ unité de la quantité (incident WhatsApp 2026-09-28).

    Producteur : « je veux vendre 50 litres de lait de vache »
    Agent      : « quel prix ? »
    Producteur : « 500f le sachet de lait »
    Agent (AVANT) : « Vente de 50 LITRE de Lait de vache à 500 FCFA/SAC.
                      ⚠️ Le prix sera appliqué par LITRE, pas par SAC — Confirmez-vous ? »

Le récap affichait une base (SAC) et exécutait l'autre (LITRE) : 500 F le sachet devenait
500 F le litre. Ces tests tournent sur le VRAI graphe compilé (harnais conversationnel)."""
from __future__ import annotations

import pytest

from ladini.domain.price_basis import PriceBasisAction, reconcile_price_basis
from tests.harness import ConversationHarness, new_task

pytestmark = pytest.mark.integration


def _answer(**entities):
    return {"disposition": "ANSWER", "extracted_entities": entities, "confidence": 0.9}


@pytest.fixture
def conv():
    with ConversationHarness(role="PRODUCER") as c:
        c.runtime.responses["get_farms"] = {
            "status": "success", "data": [{"id": "farm-1", "name": "Ferme Awa"}],
        }
        c.runtime.responses["get_or_create_farm"] = {
            "status": "success", "data": {"farm_id": "farm-1", "id": "farm-1", "name": "Ferme Awa"},
        }
        yield c


def _start(conv):
    return conv.send(
        "je veux vendre 50 litres de lait de vache",
        llm=new_task("SALES_PUBLISH_PRODUCT", product="lait de vache", quantity=50.0, unit="LITRE"),
    )


class TestSachetPriceForLitreQuantity:
    """Le scénario de la capture, sur le VRAI graphe. Depuis la Phase B1 le vertical slice
    SALES_PUBLISH_PRODUCT est porté par le modèle commercial (`CommercialOffer`) : « 500f le
    sachet » ne peut plus atteindre la confirmation tant que le contenu du sachet est inconnu
    (voir `test_commercial_pricing_vertical_slice.py` pour la suite complète A–F)."""

    def test_the_recap_never_shows_a_price_basis_that_is_not_executed(self, conv):
        t1 = _start(conv)
        assert (t1.pending_after.kind.value, t1.pending_after.field) == ("ENTER_FIELD", "price")

        t2 = conv.send("500f le sachet", llm=_answer(price=500.0, price_unit="SAC"))

        assert "Confirmez" not in t2.response, t2.response
        assert "500 FCFA/SAC" not in t2.response
        assert "appliqué par" not in t2.response
        assert t2.draft("sales_publish_draft") is None
        assert (t2.pending_after.kind.value, t2.pending_after.field) == ("ENTER_FIELD", "package_size")

    def test_the_agent_asks_the_content_of_the_package_without_guessing(self, conv):
        _start(conv)
        t2 = conv.send("500f le sachet", llm=_answer(price=500.0, price_unit="SAC"))
        assert "Quelle quantité contient un *sachet*" in t2.response
        assert "0,5 L" in t2.response

    def test_after_the_package_size_the_recap_is_consistent(self, conv):
        _start(conv)
        conv.send("500f le sachet", llm=_answer(price=500.0, price_unit="SAC"))
        t3 = conv.send("0,5 litre")
        assert "500 FCFA par sachet de 0,5 litre" in t3.response
        assert "appliqué par" not in t3.response
        assert t3.pending_after.kind.value == "CONFIRM_ACTION"


class TestConvertiblePriceBasisIsConvertedExactly:
    def test_price_per_tonne_with_quantity_in_kg(self):
        result = reconcile_price_basis(500000, "TONNE", "KG")
        assert result.action == PriceBasisAction.CONVERTED and result.price == 500.0

    def test_price_per_kg_with_quantity_in_tonnes_is_converted_to_the_tonne_basis(self):
        result = reconcile_price_basis(500, "KG", "TONNE")
        assert result.action == PriceBasisAction.CONVERTED and result.price == 500000.0

    def test_same_unit_is_untouched(self):
        result = reconcile_price_basis(400, "litre", "LITRE")
        assert result.action == PriceBasisAction.CONSISTENT and result.price == 400.0

    def test_no_price_unit_means_nothing_to_reconcile(self):
        assert reconcile_price_basis(400, None, "LITRE").action == PriceBasisAction.CONSISTENT

    @pytest.mark.parametrize(
        "price_unit,unit",
        [("SAC", "LITRE"), ("PANIER", "KG"), ("SAC", "KG"), ("KG", "SAC"), ("LITRE", "KG"), ("TETE", "KG")],
    )
    def test_incommensurable_bases_are_a_conflict_never_converted(self, price_unit, unit):
        result = reconcile_price_basis(500, price_unit, unit)
        assert result.action == PriceBasisAction.CONFLICT
        assert result.price is None

    def test_validator_converts_price_per_tonne_and_the_recap_says_so(self):
        import asyncio

        from ladini.graphs.agents.market_coach.nodes.validation import validator
        from ladini.graphs.agents.market_coach.services.ui.confirmation_summary import (
            build_confirmation_summary,
        )
        from tests.conftest import StubRuntime

        state = {
            "current_goal": "PROCUREMENT_CREATE_REQUEST",
            "transaction_payload": {
                "product": "maïs", "quantity": 200.0, "unit": "TONNE",
                "price": 500000.0, "price_unit": "TONNE", "deadline": "2026-12-01",
            },
            "working_memory": {},
        }
        out = asyncio.new_event_loop().run_until_complete(validator(state, StubRuntime()))
        payload = out["transaction_payload"]
        assert payload["unit"] == "KG" and payload["quantity"] == 200000.0
        assert payload["price"] == 500.0, "500000 F la tonne = 500 F le kg, pas 500000 F/KG"
        summary = build_confirmation_summary("PROCUREMENT_CREATE_REQUEST", payload)
        assert "500 FCFA/KG" in summary and "500000 FCFA/TONNE" in summary
