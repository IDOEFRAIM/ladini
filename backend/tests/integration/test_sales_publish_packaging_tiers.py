"""SALES_PUBLISH_PRODUCT — tarification par conditionnements (`pricing_tiers`) et « le sachet à 500 ».

Incidents de production rejoués sur le VRAI graphe compilé (harnais conversationnel) :

A. « Je veux vendre mon miel » / « J'ai 60 l de miel » /
   « Je vends le bidon de 5 l à 700 FCFA et celui de 9 l à 1000 FCFA »  ->  fallback générique
   (`pricing_tiers` présent : le gate rendait `None`, l'offre incomplète du tour 2 survivait, draft jamais
   complet, `confirmation_gate` rendait `{}`).
B. « Quel est votre prix par litre ? » / « Je vends le sachet à 500 FCFA »  ->  confirmé
   « 1 200 litres de lait à 500 FCFA par litre » (le conditionnement placé AVANT le montant était ignoré et
   le contexte de question imposait LITRE).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List
from unittest import mock

import pytest

from ladini.services.database import sales_publish_draft_store as store_mod
from tests.architecture.test_sales_publish_draft_persistence import (
    _FakeDraftTable,
    _FakeSession,
)
from tests.harness import ConversationHarness, new_task

pytestmark = pytest.mark.integration

INTENT = "SALES_PUBLISH_PRODUCT"
GENERIC_FALLBACK = "Je n'ai pas bien saisi"

B5 = {"quantity": 5.0, "unit": "L", "price": 700.0, "packaging": "bidon"}
B9 = {"quantity": 9.0, "unit": "L", "price": 1000.0, "packaging": "bidon"}
S1 = {"quantity": 1.0, "unit": "L", "price": 500.0, "packaging": "sachet"}


def _ans(**entities):
    return {"disposition": "ANSWER", "extracted_entities": entities, "confidence": 0.9}


@pytest.fixture
def conv():
    table = _FakeDraftTable()
    patcher = mock.patch.object(store_mod, "get_sessionmaker", lambda: (lambda: _FakeSession(table)))
    with patcher, ConversationHarness(role="PRODUCER") as c:
        c.runtime.responses["get_farms"] = {"status": "success", "data": [{"id": "farm-1", "name": "Ferme Awa"}]}
        c.runtime.responses["get_or_create_farm"] = {
            "status": "success", "data": {"farm_id": "farm-1", "id": "farm-1", "name": "Ferme Awa"},
        }
        c.draft_table = table
        yield c


def _creates(conv) -> List[Dict[str, Any]]:
    return [dict(args) for name, args in conv.runtime.calls if name == "create_product"]


def _payload(conv) -> Dict[str, Any]:
    return conv.state().get("transaction_payload") or {}


def _draft(conv) -> Dict[str, Any]:
    return conv.state().get("sales_publish_draft") or {}


def _miel_60(conv):
    conv.send("Je veux vendre mon miel", llm=new_task(INTENT, product="miel"))
    return conv.send("J ai 60 l de miel", llm=_ans(quantity=60.0, unit="L"))


def _lait_1200_asked_price(conv):
    conv.send("Je veux vendre mon lait", llm=new_task(INTENT, product="lait"))
    t = conv.send("J ai 1200 l de lait", llm=_ans(quantity=1200.0, unit="L"))
    assert "prix" in t.response.lower() and "litre" in t.response.lower()
    return t


def _no_fallback(turn):
    assert GENERIC_FALLBACK not in turn.response, turn.response


# =====================================================================
# A — incident MIEL
# =====================================================================


class TestA_MielTwoTiers:
    def test_two_tiers_reach_a_real_confirmation_without_fallback(self, conv):
        _miel_60(conv)
        t3 = conv.send(
            "Je vends le bidon de 5 l a 700 FCFA et celui de 9 l a 1000 FCFA", llm=_ans(pricing_tiers=[B5, B9])
        )
        _no_fallback(t3)
        assert t3.pending_after.kind.value == "CONFIRM_ACTION"
        assert t3.response.startswith("Awa, ") or "Miel : 60 L disponibles" in t3.response
        assert "Miel : 60 L disponibles" in t3.response
        assert "- Bidon de 5 L : 700 FCFA" in t3.response
        assert "- Bidon de 9 L : 1" in t3.response and "000 FCFA" in t3.response
        assert "Confirmez-vous" in t3.response
        # jamais un prix par unité fabriqué à partir d'un palier
        for forbidden in ("FCFA/L", "par litre", "140", "111", "à partir de"):
            assert forbidden not in t3.response, forbidden

    def test_draft_carries_exactly_two_certified_tiers_and_the_60_l_stock(self, conv):
        _miel_60(conv)
        conv.send("Je vends le bidon de 5 l a 700 FCFA et celui de 9 l a 1000 FCFA", llm=_ans(pricing_tiers=[B5, B9]))
        d = _draft(conv)
        assert d["quantity"] == 60.0, "5 L + 9 L ne sont PAS un stock de 14 L"
        assert [(t["quantity"], t["price"], t["packaging"]) for t in d["pricing_tiers"]] == [
            (5.0, 700.0, "bidon"), (9.0, 1000.0, "bidon"),
        ]
        assert d["price"] is None, "aucun prix scalaire commercial inventé"
        assert d["commercial_offer"] is None, "l'offre incomplète du tour précédent ne survit pas"

    def test_nothing_is_created_before_confirmation(self, conv):
        _miel_60(conv)
        conv.send("Je vends le bidon de 5 l a 700 FCFA et celui de 9 l a 1000 FCFA", llm=_ans(pricing_tiers=[B5, B9]))
        assert not _creates(conv)

    def test_confirmation_persists_both_tiers_and_the_60_l_stock(self, conv):
        _miel_60(conv)
        conv.send("Je vends le bidon de 5 l a 700 FCFA et celui de 9 l a 1000 FCFA", llm=_ans(pricing_tiers=[B5, B9]))
        conv.send("oui")
        (call,) = _creates(conv)
        assert call["name"] == "miel"
        assert call["quantity_for_sale"] == 60.0
        tiers = call["pricing_tiers"]
        assert [(t["quantity"], t["price"], t["packaging"]) for t in tiers] == [
            (5.0, 700.0, "bidon"), (9.0, 1000.0, "bidon"),
        ]
        # `Product.price` = legacy shadow (prix BRUT du 1er palier), jamais un prix normalisé par litre
        assert call["price"] == 700.0
        assert "commercial_offer" not in call
        (final,) = conv.draft_table._rows.values()
        assert final["status"] == "PUBLISHED"

    def test_reject_writes_nothing(self, conv):
        _miel_60(conv)
        conv.send("Je vends le bidon de 5 l a 700 FCFA et celui de 9 l a 1000 FCFA", llm=_ans(pricing_tiers=[B5, B9]))
        conv.send("non")
        assert not _creates(conv)

    def test_repeated_confirm_never_creates_a_duplicate(self, conv):
        _miel_60(conv)
        conv.send("Je vends le bidon de 5 l a 700 FCFA et celui de 9 l a 1000 FCFA", llm=_ans(pricing_tiers=[B5, B9]))
        conv.send("oui")
        conv.send("oui")
        assert len(_creates(conv)) == 1


# =====================================================================
# B / D — un seul conditionnement = PER_PACKAGE (B1)
# =====================================================================


class TestB_SinglePackageTier:
    def test_bidon_de_5_l_a_700_is_per_package_not_700_per_litre(self, conv):
        _miel_60(conv)
        t = conv.send("Je vends le bidon de 5 L à 700", llm=_ans(pricing_tiers=[B5]))
        _no_fallback(t)
        assert t.pending_after.kind.value == "CONFIRM_ACTION"
        offer = _draft(conv)["commercial_offer"]
        assert offer["pricing"]["basis"] == "PER_PACKAGE"
        assert offer["pricing"]["amount"] == 700.0
        assert offer["package"]["package_type"] == "BIDON"
        assert offer["package"]["content_amount"] == 5.0
        assert "par bidon de 5 litres" in t.response
        assert "700 FCFA par litre" not in t.response

    def test_single_package_reuses_the_b1_execution(self, conv):
        _miel_60(conv)
        conv.send("Je vends le bidon de 5 L à 700", llm=_ans(pricing_tiers=[B5]))
        conv.send("oui")
        (call,) = _creates(conv)
        assert call["quantity_for_sale"] == 60.0
        assert call["pricing_tiers"] == [{"quantity": 5.0, "unit": "LITRE", "price": 700.0, "packaging": "bidon"}]
        assert call["commercial_offer"]["pricing"]["basis"] == "PER_PACKAGE"


class TestD_SachetDeUnLitre:
    def test_sachet_de_1_l_a_500_is_a_valid_per_package(self, conv):
        _lait_1200_asked_price(conv)
        t = conv.send("Je vends le sachet de 1 L à 500 FCFA", llm=_ans(price=500.0, price_unit="SAC"))
        _no_fallback(t)
        assert t.pending_after.kind.value == "CONFIRM_ACTION"
        offer = _draft(conv)["commercial_offer"]
        assert offer["pricing"]["basis"] == "PER_PACKAGE"
        assert offer["package"]["content_amount"] == 1.0 and offer["package"]["package_type"] == "SACHET"

    def test_same_message_as_a_tier_gives_the_same_representation(self, conv):
        _lait_1200_asked_price(conv)
        conv.send("Je vends le sachet de 1 L à 500 FCFA", llm=_ans(pricing_tiers=[S1]))
        offer = _draft(conv)["commercial_offer"]
        assert offer["pricing"]["basis"] == "PER_PACKAGE" and offer["package"]["content_amount"] == 1.0


# =====================================================================
# C — incident LAIT : « le sachet à 500 » après « prix par litre ? »
# =====================================================================


class TestC_SachetWithoutSize:
    @pytest.mark.parametrize(
        "entities",
        [
            {"price": 500.0, "price_unit": "SAC"},
            {"price": 500.0},
            {},
        ],
    )
    def test_never_500_per_litre_and_asks_the_package_size(self, conv, entities):
        _lait_1200_asked_price(conv)
        t = conv.send("Je vends le sachet a 500 FCFA", llm=_ans(**entities) if entities else _ans(price=500.0))
        _no_fallback(t)
        assert "Quelle quantité contient un *sachet*" in t.response
        assert "500 FCFA par litre" not in t.response
        assert t.pending_after.kind.value == "ENTER_FIELD"
        assert t.pending_after.field == "package_size"
        assert not _draft(conv), "aucun draft tant que le contenu du sachet est inconnu"
        offer = _payload(conv)["commercial_offer"]
        assert offer["pricing"]["basis"] == "PER_PACKAGE"
        assert offer["package"]["content_amount"] is None, "jamais 1 L halluciné"
        assert not _creates(conv)

    def test_the_size_reply_completes_the_offer(self, conv):
        _lait_1200_asked_price(conv)
        conv.send("Je vends le sachet a 500 FCFA", llm=_ans(price=500.0))
        t = conv.send("1 litre")
        assert t.pending_after.kind.value == "CONFIRM_ACTION"
        assert _draft(conv)["commercial_offer"]["package"]["content_amount"] == 1.0

    @pytest.mark.parametrize("text", ["le sachet à 500", "500 le sachet", "le bidon coûte 500"])
    def test_package_before_or_after_the_amount_has_priority_over_the_question(self, conv, text):
        _lait_1200_asked_price(conv)
        t = conv.send(text, llm=_ans(price=500.0))
        assert t.pending_after.field == "package_size", t.response
        assert _payload(conv)["commercial_offer"]["pricing"]["basis"] == "PER_PACKAGE"


# =====================================================================
# E — comportement historique inchangé
# =====================================================================


class TestE_PerBaseUnitUnchanged:
    def test_500_fcfa_par_litre(self, conv):
        _miel_60(conv)
        t = conv.send("500 FCFA par litre", llm=_ans(price=500.0, price_unit="LITRE"))
        assert "60 litres de miel à 500 FCFA par litre" in t.response
        assert _draft(conv)["commercial_offer"]["pricing"]["basis"] == "PER_BASE_UNIT"

    def test_bare_amount_still_takes_the_asked_unit(self, conv):
        _miel_60(conv)
        conv.send("500", llm=_ans(price=500.0))
        offer = _draft(conv)["commercial_offer"]
        assert offer["pricing"]["basis"] == "PER_BASE_UNIT"
        assert offer["pricing"]["basis_source"] == "QUESTION_CONTEXT_EXPLICIT"

    def test_execution_is_the_historical_scalar_payload(self, conv):
        _miel_60(conv)
        conv.send("500 FCFA par litre", llm=_ans(price=500.0, price_unit="LITRE"))
        conv.send("oui")
        (call,) = _creates(conv)
        assert call["price"] == 500.0 and "pricing_tiers" not in call


# =====================================================================
# F / G — formulations équivalentes
# =====================================================================


class TestF_ReversedPhrasing:
    def test_700_le_bidon_de_5_l_et_1000_le_bidon_de_9_l_gives_the_same_two_tiers(self, conv):
        _miel_60(conv)
        t = conv.send("700 le bidon de 5 L et 1000 le bidon de 9 L", llm=_ans(pricing_tiers=[B5, B9]))
        _no_fallback(t)
        assert [(x["quantity"], x["price"], x["packaging"]) for x in _draft(conv)["pricing_tiers"]] == [
            (5.0, 700.0, "bidon"), (9.0, 1000.0, "bidon"),
        ]

    def test_shorthand_bidon_5_l_700_bidon_9_l_1000(self, conv):
        _miel_60(conv)
        t = conv.send("bidon 5 L 700, bidon 9 L 1000", llm=_ans(pricing_tiers=[B5, B9]))
        _no_fallback(t)
        assert len(_draft(conv)["pricing_tiers"]) == 2


class TestG_NoPackagingWordIsNeverInvented:
    def test_5_l_a_700_et_9_l_a_1000(self, conv):
        _miel_60(conv)
        no_pack = [{**B5, "packaging": None}, {**B9, "packaging": None}]
        t = conv.send("5 L à 700 et 9 L à 1000", llm=_ans(pricing_tiers=no_pack))
        _no_fallback(t)
        d = _draft(conv)
        assert [(x["quantity"], x["price"], x["packaging"]) for x in d["pricing_tiers"]] == [
            (5.0, 700.0, None), (9.0, 1000.0, None),
        ]
        assert "bidon" not in t.response.lower()
        assert "- 5 L : 700 FCFA" in t.response and "- 9 L : 1" in t.response


# =====================================================================
# H — quantité disponible absente
# =====================================================================


class TestH_MissingAvailableQuantity:
    def test_tiers_without_stock_ask_the_stock_and_never_sum_the_contents(self, conv, caplog):
        conv.send("Je veux vendre mon miel", llm=new_task(INTENT, product="miel"))
        with caplog.at_level(logging.INFO):
            t = conv.send(
                "Je vends le bidon de 5 l a 700 FCFA et celui de 9 l a 1000 FCFA",
                llm=_ans(pricing_tiers=[B5, B9]),
            )
        _no_fallback(t)
        assert t.pending_after.kind.value == "ENTER_FIELD" and t.pending_after.field == "quantity"
        assert not _draft(conv)
        assert _payload(conv).get("quantity") in (None, ""), "jamais 14 L"
        assert "MISSING_AVAILABLE_QUANTITY" in caplog.text or "quantit" in t.response.lower()

    def test_the_stock_answer_then_completes_the_tiers(self, conv):
        conv.send("Je veux vendre mon miel", llm=new_task(INTENT, product="miel"))
        conv.send("Je vends le bidon de 5 l a 700 FCFA et celui de 9 l a 1000 FCFA", llm=_ans(pricing_tiers=[B5, B9]))
        t = conv.send("60 litres", llm=_ans(quantity=60.0, unit="L"))
        assert t.pending_after.kind.value == "CONFIRM_ACTION"
        assert _draft(conv)["quantity"] == 60.0 and len(_draft(conv)["pricing_tiers"]) == 2


# =====================================================================
# Clarifications tarifaires structurées
# =====================================================================


class TestClarifications:
    def test_invalid_tier_unit_is_a_targeted_clarification_in_the_tunnel(self, conv, caplog):
        _miel_60(conv)
        bad = [{"quantity": 5.0, "unit": "KG", "price": 700.0, "packaging": "bidon"}, B9]
        with caplog.at_level(logging.INFO):
            t = conv.send("bidon de 5 kg à 700 et bidon de 9 L à 1000", llm=_ans(pricing_tiers=bad))
        _no_fallback(t)
        assert "INVALID_PRICING_TIER" in caplog.text
        assert t.pending_after.kind.value == "ENTER_FIELD"
        assert not _draft(conv) and not _creates(conv)
        assert conv.state().get("current_goal") == INTENT or _payload(conv).get("intent") == INTENT

    def test_missing_package_size_is_logged_with_its_reason_and_pricing_mode(self, conv, caplog):
        _lait_1200_asked_price(conv)
        with caplog.at_level(logging.INFO):
            conv.send("Je vends le sachet a 500 FCFA", llm=_ans(price=500.0))
        assert "reason=MISSING_PACKAGE_SIZE" in caplog.text
        assert "pricing_mode=PER_PACKAGE" in caplog.text

    def test_pricing_modes_are_logged(self, conv, caplog):
        _miel_60(conv)
        with caplog.at_level(logging.INFO):
            conv.send("Je vends le bidon de 5 l a 700 FCFA et celui de 9 l a 1000 FCFA", llm=_ans(pricing_tiers=[B5, B9]))
        assert "pricing_mode=PACKAGING_TIERS" in caplog.text

    def test_a_restated_scalar_price_leaves_the_tier_mode(self, conv):
        _miel_60(conv)
        conv.send("Je vends le bidon de 5 l a 700 FCFA et celui de 9 l a 1000 FCFA", llm=_ans(pricing_tiers=[B5, B9]))
        t = conv.send("finalement 500 FCFA par litre", llm=new_task(INTENT, price=500.0, price_unit="LITRE"))
        _no_fallback(t)
        d = _draft(conv)
        assert not d.get("pricing_tiers")
        assert d["commercial_offer"]["pricing"]["basis"] == "PER_BASE_UNIT"


# =====================================================================
# K — de la publication à l'achat : 2 x 9 L
# =====================================================================


class TestK_PublishedTiersAreWhatTheBuyerPays:
    def _publish(self, conv):
        _miel_60(conv)
        conv.send("Je vends le bidon de 5 l a 700 FCFA et celui de 9 l a 1000 FCFA", llm=_ans(pricing_tiers=[B5, B9]))
        conv.send("oui")
        (call,) = _creates(conv)
        return call

    def _persisted_product(self, call):
        """L'objet `products` tel que `create_product` l'écrit : mêmes validations, mêmes colonnes."""
        import types
        import uuid

        from ladini.domain.pricing_tiers import tiers_to_dicts, validate_pricing_tiers

        tiers = tiers_to_dicts(validate_pricing_tiers(call["pricing_tiers"], call["unit"]))
        return types.SimpleNamespace(
            id=uuid.uuid4(), name=call["name"], price=call["price"], unit=call["unit"].upper(),
            quantity_for_sale=call["quantity_for_sale"], producer_id=uuid.uuid4(), pricing_tiers=tiers,
            commercial_pricing=None,
        )

    def test_the_buyer_sees_both_packagings_and_pays_2_x_1000_and_debits_18_l(self, conv):
        from ladini.domain.pricing_tiers import resolve_stock_debit
        from tests.conftest import run
        from tests.unit.test_create_preorder_draft_pricing_tiers import _FakeSession as _BuyerSession
        from tests.unit.test_create_preorder_draft_pricing_tiers import _service

        product = self._persisted_product(self._publish(conv))
        assert product.quantity_for_sale == 60.0
        assert [(t["quantity"], t["price"]) for t in product.pricing_tiers] == [(5.0, 700.0), (9.0, 1000.0)]
        nine = next(t for t in product.pricing_tiers if t["quantity"] == 9.0)
        session = _BuyerSession(product)
        result = run(
            _service(session).create_preorder_draft(
                buyer_phone="+22670000001",
                cart_items=[{"product_id": str(product.id), "quantity": 2, "tier_id": nine["tier_id"]}],
            )
        )
        assert result["status"] == "success"
        assert result["total_amount"] == 2000.0, "2 x 1000, jamais 2 x le legacy Product.price (700)"
        (item,) = [o for o in session.added if type(o).__name__ == "OrderItem"]
        assert item.quantity == 2 and item.price_at_sale == 1000.0
        assert item.base_unit_quantity == 18.0
        assert resolve_stock_debit(item) == 18.0
        assert product.quantity_for_sale - resolve_stock_debit(item) == 42.0


# =====================================================================
# L — catalogue producteur
# =====================================================================


class TestL_ProducerCatalogNeverShowsTheLegacyShadowAsAPrice:
    def test_tiered_product_shows_its_packagings_and_no_per_litre_price(self):
        from ladini.graphs.agents.market_coach.nodes.rendering.success import _render_catalog_section

        text, _ = _render_catalog_section(
            [{"name": "Miel", "price": 700.0, "unit": "LITRE", "quantity_for_sale": 60.0,
              "pricing_tiers": [B5, B9]}]
        )
        assert "60 LITRE dispo" in text and "Conditionnements" in text
        assert "5 L (bidon)" in text and "700 FCFA" in text and "9 L (bidon)" in text
        assert "FCFA/LITRE" not in text and "💰" not in text

    def test_historical_per_unit_product_keeps_its_display(self):
        from ladini.graphs.agents.market_coach.nodes.rendering.success import _render_catalog_section

        text, _ = _render_catalog_section(
            [{"name": "Maïs", "price": 300.0, "unit": "KG", "quantity_for_sale": 100.0}]
        )
        assert "💰 300 FCFA/KG" in text


# =====================================================================
# Consommateurs du legacy shadow `Product.price`
# =====================================================================


class TestLegacyShadowIsNeverACommercialPrice:
    def test_buyer_search_label_of_a_tiered_product_is_its_packagings(self):
        from ladini.domain.commercial_pricing_snapshot import product_pricing_view

        view = product_pricing_view({"price": 700.0, "pricing_tiers": [B5, B9], "commercial_pricing": None})
        assert view.pricing_label == "Bidon de 5 L : 700 FCFA · Bidon de 9 L : 1 000 FCFA"
        assert view.amount is None and not view.is_comparable

    def test_untiered_legacy_product_label_is_unchanged(self):
        from ladini.domain.commercial_pricing_snapshot import product_pricing_view

        view = product_pricing_view({"price": 300.0, "pricing_tiers": None, "commercial_pricing": None})
        assert view.pricing_label == "300 FCFA — base historique non certifiée"

    def test_dto_shadow_is_the_raw_first_tier_price_and_never_rebased(self):
        from ladini.graphs.agents.market_coach.actions.sales_dto import SalesPublishProductPayload

        dto = SalesPublishProductPayload.from_payload(
            {"product": "miel", "quantity": 60.0, "unit": "LITRE", "pricing_tiers": [B5, B9]}
        )
        assert dto.price == 700.0 and len(dto.pricing_tiers) == 2

    def test_no_price_and_no_tiers_is_still_refused(self):
        from ladini.graphs.agents.market_coach.actions.sales_dto import SalesPublishProductPayload

        with pytest.raises(ValueError):
            SalesPublishProductPayload.from_payload({"product": "miel", "quantity": 60.0, "unit": "LITRE"})
