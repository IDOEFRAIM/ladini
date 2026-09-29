"""Verrous d'architecture + goldens — certification du prix de négociation directe (Phase B2c.6).

Le bug fermé : `initiate_negotiation_session`/`update_negotiation_offer` écrivaient
`Auction.max_price_per_unit` depuis un float brut extrait du texte, sans base ni provenance —
« 4 millions » pour un lot de 10 TONNE pouvait devenir 4 000 000 FCFA/TONNE (une erreur ×10) au lieu
d'un budget de 4 000 000 FCFA pour tout le lot. Voir `domain/negotiation_offer.py` et l'en-tête de
`flows/buyer/negotiation.py`."""
from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path

from tests.conftest import run

BACKEND = Path(__file__).resolve().parents[2]
SRC = BACKEND / "src" / "ladini"
NEGOTIATION = SRC / "graphs" / "agents" / "market_coach" / "flows" / "buyer" / "negotiation.py"
NEGOTIATION_OFFER = SRC / "domain" / "negotiation_offer.py"


def _calls(path: Path, attr: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr:
            yield node


# =====================================================================
# ARCHITECTURE LOCKS
# =====================================================================


class TestNoRawFloatPricingWriter:
    def test_initiate_session_and_update_offer_only_send_the_certified_ceiling(self):
        for attr, kw in (("initiate_session", "offered_price"), ("update_offer", "new_price")):
            calls = list(_calls(NEGOTIATION, attr))
            assert calls, f"{attr} n'est plus appelé — négociation cassée ?"
            for call in calls:
                kwargs = {c.arg: c for c in call.keywords}
                assert kw in kwargs, f"{attr} sans {kw}"
                value_src = ast.dump(kwargs[kw].value)
                assert "ceiling_per_unit" in value_src, f"{attr}({kw}=...) ne vient pas de l'offre certifiée"

    def test_payload_or_raw_price_never_reaches_the_gateway_call(self):
        src = NEGOTIATION.read_text(encoding="utf-8")
        for pattern in (
            r'offered_price=payload\.get\("price"\)',
            r'new_price=payload\.get\("price"\)',
            r"offered_price=price\b",
            r"new_price=price\b",
            r"offered_price=float\(payload",
        ):
            assert not re.search(pattern, src), pattern


class TestConfirmationAndExecutionUseTheFrozenOffer:
    def test_confirmation_is_a_pure_projection_of_the_certified_offer(self):
        src = NEGOTIATION.read_text(encoding="utf-8")
        assert "offer.confirmation_text(" in src

    def test_execution_rereads_the_frozen_offer_not_the_live_text(self):
        src = NEGOTIATION.read_text(encoding="utf-8")
        assert 'CertifiedNegotiationOffer.from_state(nctx.get("pending_offer"))' in src


class TestNormalizationIsDeterministicDecimalMath:
    def test_ceiling_derivation_never_divides_as_float(self):
        src = NEGOTIATION_OFFER.read_text(encoding="utf-8")
        body = src.split("def build_negotiation_offer")[1].split("return CertifiedNegotiationOffer")[0]
        assert "quantize_normalized(total / qty)" in body
        assert "float(" not in body


# =====================================================================
# GOLDENS A-F (mandat de certification)
# =====================================================================

import ladini.graphs.agents.market_coach.flows.buyer.negotiation as neg  # noqa: E402
from ladini.domain.bid_pricing_flow import (  # noqa: E402
    BidPriceContext,
    parse_bid_price,
)
from ladini.domain.commercial_offer import PriceBasis, Provenance  # noqa: E402

_REF = {"product_id": "p1", "name": "Maïs", "unit": "TONNE"}


class TestGoldenA_PerTonneExplicit:
    def test_450000_la_tonne_is_per_base_unit_unchanged(self):
        parsed = parse_bid_price("450000 la tonne", auction_unit="TONNE", auction_quantity=10)
        assert parsed.is_resolved and parsed.basis == PriceBasis.PER_BASE_UNIT
        offer, err = neg._certify_negotiation_price(
            parsed, ref=_REF, quantity=10, phone="+22670000001", auction_id=None
        )
        assert err is None
        assert offer.ceiling_per_unit == Decimal("450000.0000")
        assert offer.total == Decimal("4500000.00")


class TestGoldenB_TotalLotNoTenXMistake:
    def test_4_millions_pour_tout_normalizes_without_x10_error(self):
        parsed = parse_bid_price("4 millions pour tout", auction_unit="TONNE", auction_quantity=10)
        assert parsed.is_resolved and parsed.basis == PriceBasis.TOTAL_LOT
        offer, err = neg._certify_negotiation_price(
            parsed, ref=_REF, quantity=10, phone="+22670000001", auction_id=None
        )
        assert err is None
        assert offer.total == Decimal("4000000.00")
        # Jamais 4 000 000 FCFA/TONNE (la ×10 dénoncée par la mission) : le plafond dérivé divise
        # le budget total par la quantité, il ne le RÉPÈTE pas par unité.
        assert offer.ceiling_per_unit == Decimal("400000.0000")


class TestGoldenC_BareAmountIsAmbiguousNeverWrites:
    def test_4_millions_alone_asks_and_never_calls_initiate_session(self, monkeypatch):
        class _StubProduct:
            def __init__(self, rt):
                pass

            async def search_products(self, **kwargs):
                return {
                    "status": "success",
                    "results": [
                        {"id": "p1", "name": "Maïs", "unit": "TONNE", "price": 100, "available_quantity": 10}
                    ],
                }

        class _AssertNeverCalledNegotiation:
            def __init__(self, rt):
                pass

            async def initiate_session(self, **kwargs):
                raise AssertionError("aucune écriture ne doit avoir lieu tant que la base du prix est ambiguë")

        monkeypatch.setattr(neg, "ProductGateway", _StubProduct)
        monkeypatch.setattr(neg, "NegotiationGateway", _AssertNeverCalledNegotiation)
        state = {"normalized_text": "4 millions", "user_query": "4 millions"}
        result = run(
            neg._initiate_negotiation(
                mc_runtime=None, phone="+22670000001", product_name="mais", quantity=10, payload={}, state=state,
            )
        )
        assert result["status"] == "WAITING_INPUT"
        assert result["negotiation_context"]["phase"] == "AWAIT_NEGOTIATION_PRICE_BASIS"


class TestGoldenD_QuestionContextFixesTheBasis:
    def test_bare_number_after_the_per_tonne_question_is_per_tonne_not_ambiguous(self):
        parsed = parse_bid_price(
            "450000",
            auction_unit="TONNE",
            auction_quantity=10,
            context=BidPriceContext.per_auction_unit("TONNE"),
        )
        assert parsed.is_resolved
        assert parsed.basis == PriceBasis.PER_BASE_UNIT
        assert parsed.source == Provenance.QUESTION_CONTEXT_EXPLICIT

    def test_the_counter_offer_question_names_the_auction_unit(self):
        src = NEGOTIATION.read_text(encoding="utf-8")
        assert "price_per_unit_question(unit)" in src
        assert "BidPriceContext.per_auction_unit(unit)" in src


class TestGoldenE_CorrectionSwitchesBasisNotJustTheAmount:
    def test_finalement_pour_tout_switches_basis_on_the_confirmation_screen(self):
        parsed = parse_bid_price("450000 la tonne", auction_unit="TONNE", auction_quantity=10)
        offer, _ = neg._certify_negotiation_price(
            parsed, ref=_REF, quantity=10, phone="+22670000001", auction_id=None
        )
        assert offer.pricing.price_basis == PriceBasis.PER_BASE_UNIT
        nctx = {"pending_product": _REF, "pending_quantity": 10, "pending_offer": offer.to_state()}
        state = {"interpreted_event": "NEW_TASK", "normalized_text": "finalement 4 millions pour tout"}
        result = run(
            neg._handle_confirm_negotiation_offer(
                mc_runtime=None, state=state, nctx=nctx, phone="+22670000001", auction_id=None,
            )
        )
        assert result["negotiation_context"]["phase"] == "CONFIRM_NEGOTIATION_INIT"
        new_offer = neg.CertifiedNegotiationOffer.from_state(result["negotiation_context"]["pending_offer"])
        assert new_offer.pricing.price_basis == PriceBasis.TOTAL_LOT
        assert new_offer.ceiling_per_unit == Decimal("400000.0000")


class TestGoldenF_CorruptedRawStateNeverLeaksIntoExecution:
    def test_tampered_transaction_payload_does_not_change_what_gets_persisted(self, monkeypatch):
        parsed = parse_bid_price("4 millions pour tout", auction_unit="TONNE", auction_quantity=10)
        offer, _ = neg._certify_negotiation_price(
            parsed, ref=_REF, quantity=10, phone="+22670000001", auction_id=None
        )
        nctx = {"pending_product": _REF, "pending_quantity": 10, "pending_offer": offer.to_state()}
        captured: dict = {}

        class _SpyNegotiation:
            def __init__(self, rt):
                pass

            async def initiate_session(self, **kwargs):
                captured.update(kwargs)
                return {"status": "success", "negotiation_id": "a1", "auction_id": "a1", "product_id": "p1"}

        monkeypatch.setattr(neg, "claim_once", lambda *a, **k: True)
        monkeypatch.setattr(neg, "NegotiationGateway", _SpyNegotiation)
        # `transaction_payload`/état brut corrompu ENTRE la confirmation affichée et le "oui" — ne
        # doit RIEN changer : `_handle_confirm_negotiation_offer` ne lit même pas `transaction_payload`.
        state = {
            "interpreted_event": "CONFIRM",
            "normalized_text": "oui",
            "transaction_payload": {"price": 1, "resolved_id": "HACKED"},
        }
        result = run(
            neg._handle_confirm_negotiation_offer(
                mc_runtime=None, state=state, nctx=nctx, phone="+22670000001", auction_id=None,
            )
        )
        assert captured["offered_price"] == float(offer.ceiling_per_unit) == 400000.0
        assert result["negotiation_context"]["pricing"]["price_basis"] == "TOTAL_LOT"
