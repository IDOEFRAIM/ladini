"""Flux Bid -> Award sur le VRAI graphe compilé (Phase B2b).

Garantie testée : un bid n'est JAMAIS « 450000 » sans savoir si c'est par tonne, par kg, par conditionnement ou au
total ; l'acheteur ne retient jamais un gagnant sans confirmer PRIX + BASE + QUANTITÉ + TOTAL ; ce qui est exécuté est
ce qui a été confirmé. Le prix d'un bid est lu déterministiquement (aucun appel LLM sur les tours de prix)."""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from ladini.domain.commercial_pricing_snapshot import build_bid_pricing_snapshot
from tests.harness import ConversationHarness, new_task
from tests.nodes.award_fixtures import bid_row, bids_response

pytestmark = pytest.mark.integration

AUCTION_ID = "11111111-1111-1111-1111-111111111111"
BROWSE = new_task("MARKET_BROWSE_REQUESTS")


def _producer(unit: str = "TONNE", quantity: float = 10.0):
    c = ConversationHarness(role="PRODUCER")
    c.__enter__()
    c.runtime.responses["get_producer_auctions"] = {
        "status": "success", "count": 1, "scope": "MATCHABLE", "formatted_menu": "1. Maïs",
        "mapping": {"1": AUCTION_ID},
        "data": [{"auction_id": AUCTION_ID, "product": "maïs", "unit": unit, "max_price": 500000, "quantity": quantity}],
    }
    c.runtime.responses["place_bid"] = {"status": "success", "bid_id": "bid-1", "message": "✅ Offre transmise."}
    return c


@pytest.fixture
def producer():
    c = _producer()
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def kg_producer():
    c = _producer("KG", 1000.0)
    yield c
    c.__exit__(None, None, None)


def _writes(c, name: str = "place_bid") -> List[Dict[str, Any]]:
    return [dict(args) for tool, args in c.runtime.calls if tool == name]


def _open_price_question(c):
    c.send("voir les appels d'offres", llm=BROWSE)
    return c.send("1")


# =====================================================================
# Producteur — nouveau bid
# =====================================================================


class TestGoldenA_BarePriceAfterThePerTonneQuestion:
    def test_the_question_fixes_the_basis(self, producer):
        t = _open_price_question(producer)
        assert "par tonne" in t.response and "10 tonnes" in t.response
        assert t.pending_after.field == "price"

    def test_450000_becomes_450000_per_tonne_from_the_question_context(self, producer):
        _open_price_question(producer)
        t = producer.send("450000")
        assert t.llm_calls == 0, "le prix d'un bid ne dépend pas d'un classifieur LLM"
        assert "450 000 FCFA par tonne" in t.response and "4 500 000 FCFA" in t.response
        assert _writes(producer) == [], "rien n'est écrit avant le « oui »"
        state = producer.state()["working_memory"]["pending_bid_pricing"]
        assert (state["basis"], state["price_unit"], state["source"]) == ("PER_BASE_UNIT", "TONNE", "QUESTION_CONTEXT_EXPLICIT")

    def test_place_bid_receives_the_complete_pricing_contract(self, producer):
        _open_price_question(producer)
        producer.send("450000")
        producer.send("oui")
        (call,) = _writes(producer)
        assert (call["offered_price"], call["price_basis"], call["price_unit"]) == (450000.0, "PER_BASE_UNIT", "TONNE")
        assert call["auction_id"] == AUCTION_ID and call["package_type"] is None


class TestGoldenB_TotalLot:
    @pytest.mark.parametrize("text,amount", [("4,2 millions pour tout", 4_200_000.0), ("4 500 000 pour l'ensemble", 4_500_000.0)])
    def test_whole_lot_is_never_multiplied_or_converted(self, producer, text, amount):
        _open_price_question(producer)
        t = producer.send(text)
        assert "pour l'ensemble" in t.response and f"Total : *{int(amount):,}".replace(",", " ") in t.response
        producer.send("oui")
        (call,) = _writes(producer)
        assert (call["offered_price"], call["price_basis"], call["price_unit"]) == (amount, "TOTAL_LOT", None)


class TestGoldenC_AmbiguityIsAskedNeverGuessed:
    def test_a_price_with_two_readings_asks_and_writes_nothing(self, producer):
        _open_price_question(producer)
        t = producer.send("450000 la tonne pour tout")
        assert "par tonne" in t.response and "pour l'ensemble" in t.response
        assert t.pending_after.field == "price_basis"
        assert _writes(producer) == []

    def test_answering_the_basis_question_completes_the_pricing(self, producer):
        _open_price_question(producer)
        producer.send("450000 la tonne pour tout")
        t = producer.send("pour l'ensemble")
        assert t.llm_calls == 0 and "pour l'ensemble" in t.response
        producer.send("oui")
        (call,) = _writes(producer)
        assert (call["offered_price"], call["price_basis"]) == (450000.0, "TOTAL_LOT")

    def test_two_unmarked_numbers_are_not_guessed(self, producer):
        _open_price_question(producer)
        producer.send("450000 ou 430000")
        assert _writes(producer) == []


class TestGoldenD_TonnePriceOnAKgAuction:
    def test_commercial_price_stays_per_tonne_and_the_total_is_right(self, kg_producer):
        _open_price_question(kg_producer)
        t = kg_producer.send("450000 la tonne")
        assert "450 000 FCFA par tonne" in t.response
        assert "Total : *450 000 FCFA*" in t.response
        kg_producer.send("oui")
        (call,) = _writes(kg_producer)
        assert (call["offered_price"], call["price_basis"], call["price_unit"]) == (450000.0, "PER_BASE_UNIT", "TONNE")

    def test_a_price_per_litre_on_a_kg_auction_is_refused_and_not_written(self, kg_producer):
        _open_price_question(kg_producer)
        t = kg_producer.send("500 le litre")
        assert "ne correspond pas" in t.response
        assert _writes(kg_producer) == []


class TestCorrectionsAndCancellation:
    def test_a_correction_keeps_the_displayed_basis(self, producer):
        _open_price_question(producer)
        producer.send("4,5 millions pour tout")
        t = producer.send("4 millions")
        assert "4 000 000 FCFA pour l'ensemble" in t.response
        producer.send("oui")
        (call,) = _writes(producer)
        assert (call["offered_price"], call["price_basis"]) == (4_000_000.0, "TOTAL_LOT")

    def test_a_correction_can_change_the_basis(self, producer):
        _open_price_question(producer)
        producer.send("4,5 millions pour tout")
        producer.send("finalement 430000 la tonne")
        producer.send("oui")
        (call,) = _writes(producer)
        assert (call["offered_price"], call["price_basis"], call["price_unit"]) == (430000.0, "PER_BASE_UNIT", "TONNE")

    def test_declining_writes_nothing(self, producer):
        _open_price_question(producer)
        producer.send("450000")
        t = producer.send("non")
        assert "annulée" in t.response
        assert _writes(producer) == []

    def test_a_second_ok_does_not_write_again(self, producer):
        _open_price_question(producer)
        producer.send("450000")
        producer.send("oui")
        try:
            producer.send("oui")
        except Exception:  # noqa: BLE001 — un tour non scripté hors tunnel n'est pas l'objet du test
            pass
        assert len(_writes(producer)) == 1


class TestLlmHintsAreNeverAuthoritative:
    def test_an_llm_suggested_basis_does_not_resolve_an_ambiguous_price(self, producer):
        _open_price_question(producer)
        # le LLM « suggère » TOTAL_LOT ; le texte est ambigu -> on demande, on n'écrit pas
        producer.send(
            "450000 la tonne pour tout",
            llm={"disposition": "ANSWER", "extracted_entities": {"price": 450000.0, "price_basis": "TOTAL_LOT"}, "confidence": 0.9},
        )
        assert _writes(producer) == []


# =====================================================================
# Producteur — modification d'un bid existant
# =====================================================================


def _modify_setup(c, *, legacy: bool):
    pricing = None if legacy else build_bid_pricing_snapshot(
        amount=450000, basis="PER_BASE_UNIT", price_unit="TONNE", auction_quantity=10, auction_unit="TONNE",
    ).to_dict()
    c.runtime.responses["get_my_active_bids"] = {
        "status": "success", "count": 1, "formatted_menu": "1. maïs", "mapping": {"1": "bid-1"},
        "data": [{
            "bid_id": "bid-1", "auction_id": AUCTION_ID, "product": "maïs", "quantity": 10.0, "unit": "TONNE",
            "offered_price": 450000.0, "status": "PENDING",
            "pricing_label": "450 000 FCFA (base de prix inconnue)" if legacy else "450 000 FCFA par tonne",
            "pricing": pricing,
        }],
    }
    c.runtime.responses["update_bid_price"] = {"status": "success"}
    c.send("mes propositions", llm=new_task("MARKET_GET_MY_PROPOSALS"))
    return c.send("1")


class TestModifyingABid:
    def test_a_bare_amount_keeps_the_existing_basis(self, producer):
        _modify_setup(producer, legacy=False)
        producer.send("430000")
        producer.send("oui")
        (call,) = _writes(producer, "update_bid_price")
        assert (call["new_price"], call["price_basis"], call["price_unit"]) == (430000.0, "PER_BASE_UNIT", "TONNE")

    def test_finalement_4_millions_pour_tout_changes_the_basis_to_total_lot(self, producer):
        _modify_setup(producer, legacy=False)
        t = producer.send("finalement 4 millions pour tout")
        assert t.llm_calls == 0 and "4 000 000 FCFA pour l'ensemble" in t.response
        producer.send("oui")
        (call,) = _writes(producer, "update_bid_price")
        assert (call["new_price"], call["price_basis"], call["price_unit"]) == (4_000_000.0, "TOTAL_LOT", None)

    def test_a_legacy_bid_is_requalified_by_asking_the_basis(self, producer):
        t0 = _modify_setup(producer, legacy=True)
        assert "base de prix" in t0.response
        t = producer.send("450000")
        assert "par tonne" in t.response and "pour l'ensemble" in t.response
        assert _writes(producer, "update_bid_price") == [], "aucune écriture avant résolution de la base"
        producer.send("par tonne")
        producer.send("oui")
        (call,) = _writes(producer, "update_bid_price")
        assert (call["new_price"], call["price_basis"], call["price_unit"]) == (450000.0, "PER_BASE_UNIT", "TONNE")


# =====================================================================
# Acheteur — attribution sur décision certifiée
# =====================================================================


@pytest.fixture
def buyer():
    c = ConversationHarness(role="BUYER")
    c.__enter__()
    c.runtime.responses["get_auctions"] = {
        "status": "success",
        "data": [{"auction_id": "a1", "product": "maïs", "quantity": 10, "unit": "TONNE", "status": "OPEN", "bid_count": 3}],
    }
    c.runtime.responses["get_auction_bids"] = bids_response(
        bid_row(450000, bid_id="b1", producer="Gilbert-prod"),
        bid_row(4_200_000, basis="TOTAL_LOT", bid_id="b2", producer="Awa"),
        bid_row(430000, bid_id="b3", producer="Ancien", legacy=True),
        product="maïs",
    )
    c.runtime.responses["select_winning_bid"] = {"status": "success", "summary_buyer": "🤝 Commande créée."}
    c.runtime.responses["get_user_by_phone"] = {"status": "success", "data": {"latitude": 12.35, "longitude": -1.5}}
    c.send("mes appels d'offres", llm=new_task("BUYER_LIST_AUCTIONS"))
    yield c
    c.__exit__(None, None, None)


class TestBuyerSeesEachOffersOwnSemantics:
    def test_bids_are_listed_with_their_own_basis_and_total_never_flattened(self, buyer):
        t = buyer.send("1")
        assert "450 000 FCFA par tonne → total 4 500 000 FCFA" in t.response
        assert "4 200 000 FCFA pour l'ensemble → total 4 200 000 FCFA" in t.response
        assert "base de prix inconnue" in t.response and "à préciser" in t.response


class TestGoldenA_ConfirmingAPerUnitOffer:
    def test_the_confirmation_shows_producer_price_basis_quantity_and_total(self, buyer):
        buyer.send("1")
        t = buyer.send("1")
        for expected in ("Gilbert-prod", "450 000 FCFA par tonne", "10 tonnes", "4 500 000 FCFA"):
            assert expected in t.response
        assert _writes(buyer, "select_winning_bid") == [], "choisir une ligne ne désigne PAS le gagnant"
        assert t.after["working_memory"]["pending_award"]["fingerprint"]

    def test_nothing_executes_at_the_gps_question_either(self, buyer):
        buyer.send("1")
        buyer.send("1")
        t = buyer.send("oui")
        assert t.pending_after.kind.value == "PROVIDE_LOCATION"
        assert _writes(buyer, "select_winning_bid") == []


class TestGoldenB_TotalLotOffer:
    def test_the_total_is_the_amount_not_amount_times_quantity(self, buyer):
        buyer.send("1")
        t = buyer.send("2")
        assert "4 200 000 FCFA pour l'ensemble" in t.response
        assert "Total : *4 200 000 FCFA*" in t.response and "42 000 000" not in t.response


class TestGoldenE_LegacyBidIsNotAwarded:
    def test_a_bid_without_a_basis_is_explained_not_confirmed(self, buyer):
        buyer.send("1")
        t = buyer.send("3")
        assert "n'indique pas" in t.response and "Confirmez" not in t.response
        assert t.after["working_memory"].get("pending_award") in (None, {})
        assert _writes(buyer, "select_winning_bid") == []


class TestGoldenF_FrozenDecisionSurvivesRawStateMutation:
    def test_the_frozen_decision_is_what_the_state_carries_after_a_raw_price_mutation(self, buyer):
        buyer.send("1")
        buyer.send("1")
        frozen_before = buyer.state()["working_memory"]["pending_award"]["fingerprint"]
        buyer.seed({"transaction_payload": {"price": 1.0, "offered_price": 1.0}})
        assert buyer.state()["working_memory"]["pending_award"]["fingerprint"] == frozen_before
