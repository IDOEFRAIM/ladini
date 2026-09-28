"""Décision d'attribution certifiée (Phase B2b) — domaine pur.

Ce que l'acheteur confirme (producteur, prix + BASE, quantité, total) est un objet FIGÉ dont l'empreinte
change dès qu'un terme change ; une offre sans base n'est jamais attribuable ; la comparaison ne mélange
jamais les bases."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from ladini.domain.bid_award import (
    AwardNotPossible,
    BidBasisUnknown,
    CertifiedAwardDecision,
    build_award_decision,
    compare_bid,
    rank_comparisons,
)
from ladini.domain.commercial_pricing_snapshot import PricingReliability, build_bid_pricing_snapshot

D = Decimal


def decision_for(amount, *, basis="PER_BASE_UNIT", unit="TONNE", quantity=10, bid_id="b1") -> CertifiedAwardDecision:
    snap = build_bid_pricing_snapshot(
        amount=amount, basis=basis, price_unit=unit if basis == "PER_BASE_UNIT" else None,
        auction_quantity=quantity, auction_unit=unit,
    )
    return build_award_decision(
        auction_id="a1", bid_id=bid_id, producer_id="p1", buyer_id="buyer-1", producer_name="Gilbert-prod",
        pricing=snap, auction_quantity=quantity, auction_unit=unit,
    )


def bid_row(amount, *, basis="PER_BASE_UNIT", unit="TONNE", quantity=10, bid_id="b1"):
    snap = build_bid_pricing_snapshot(
        amount=amount, basis=basis, price_unit=unit if basis == "PER_BASE_UNIT" else None,
        auction_quantity=quantity, auction_unit=unit,
    )
    return SimpleNamespace(id=bid_id, **snap.to_bid_columns())


class TestAwardTotals:
    def test_per_tonne_bid_total(self):
        d = decision_for(450000)
        assert d.award_total == D("4500000.00")

    def test_total_lot_is_not_multiplied_by_the_quantity(self):
        d = decision_for(4_200_000, basis="TOTAL_LOT")
        assert d.award_total == D("4200000.00")

    def test_per_tonne_bid_on_a_kg_auction_keeps_the_commercial_price(self):
        snap = build_bid_pricing_snapshot(
            amount=450000, basis="PER_BASE_UNIT", price_unit="TONNE", auction_quantity=1000, auction_unit="KG",
        )
        d = build_award_decision(
            auction_id="a1", bid_id="b1", producer_id="p1", buyer_id="u", producer_name="X",
            pricing=snap, auction_quantity=1000, auction_unit="KG",
        )
        assert d.award_total == D("450000.00")
        assert d.pricing.commercial_price_amount == D("450000") and d.pricing.price_unit == "TONNE"
        assert d.pricing.normalized_unit_price == D("450.0000")  # dérivé, jamais affiché À LA PLACE
        assert "450 000 FCFA par tonne" in d.confirmation_text("maïs")


class TestUnknownBasisIsNeverAwarded:
    def test_a_bid_without_a_snapshot_raises_basis_unknown(self):
        with pytest.raises(BidBasisUnknown) as info:
            build_award_decision(
                auction_id="a1", bid_id="b1", producer_id="p1", buyer_id="u", producer_name="X",
                pricing=None, auction_quantity=10, auction_unit="TONNE",
            )
        assert info.value.reason == "bid_basis_unknown"

    def test_an_incompatible_basis_is_refused(self):
        snap = build_bid_pricing_snapshot(
            amount=450, basis="PER_BASE_UNIT", price_unit="KG", auction_quantity=10, auction_unit="TONNE",
        )
        with pytest.raises(AwardNotPossible):
            build_award_decision(
                auction_id="a1", bid_id="b1", producer_id="p1", buyer_id="u", producer_name="X",
                pricing=snap, auction_quantity=10, auction_unit="LITRE",
            )


class TestConfirmationText:
    def test_per_unit_offer(self):
        text = decision_for(450000).confirmation_text("maïs")
        assert "Gilbert-prod" in text and "450 000 FCFA par tonne" in text
        assert "10 tonnes" in text and "4 500 000 FCFA" in text

    def test_total_lot_offer(self):
        text = decision_for(4_200_000, basis="TOTAL_LOT").confirmation_text("maïs")
        assert "4 200 000 FCFA pour l'ensemble" in text
        assert "Total : *4 200 000 FCFA*" in text


class TestFingerprintAndIdempotency:
    def test_same_terms_same_fingerprint_and_key(self):
        a, b = decision_for(450000), decision_for(450000)
        assert a.fingerprint == b.fingerprint and a.idempotency_key == b.idempotency_key

    @pytest.mark.parametrize(
        "other",
        [
            dict(amount=430000),  # prix
            dict(amount=4_500_000, basis="TOTAL_LOT"),  # base
            dict(amount=450000, quantity=12),  # quantité
            dict(amount=450000, bid_id="b2"),  # autre offre
        ],
    )
    def test_any_changed_term_changes_the_fingerprint_and_the_key(self, other):
        base = decision_for(450000)
        changed = decision_for(**other)
        assert changed.fingerprint != base.fingerprint and changed.idempotency_key != base.idempotency_key

    def test_state_round_trip_and_tamper_detection(self):
        d = decision_for(450000)
        back = CertifiedAwardDecision.from_state(d.to_state())
        assert back is not None and back.fingerprint == d.fingerprint and back.award_total == d.award_total
        tampered = d.to_state()
        tampered["award_total"] = "1"
        assert CertifiedAwardDecision.from_state(tampered) is None or CertifiedAwardDecision.from_state(tampered).fingerprint != d.fingerprint
        forged = d.to_state()
        forged["pricing"]["commercial_price_amount"] = "1"
        assert CertifiedAwardDecision.from_state(forged) is None  # l'empreinte stockée ne correspond plus

    def test_frozen_snapshot_carries_exactly_the_certified_terms(self):
        frozen = decision_for(450000).frozen_snapshot()
        assert frozen["price_basis"] == "PER_BASE_UNIT" and frozen["price_unit"] == "TONNE"
        assert frozen["award"]["auction_quantity"] == "10" and frozen["award"]["auction_unit"] == "TONNE"
        assert frozen["award"]["total_amount"] == "4500000.00"
        assert frozen["award"]["fingerprint"]


class TestMixedBidsComparison:
    def test_per_unit_and_total_lot_are_comparable_via_their_totals_and_legacy_is_not(self):
        a = compare_bid(bid_row(450000), 10, "TONNE")  # 4 500 000
        b = compare_bid(bid_row(4_200_000, basis="TOTAL_LOT", bid_id="b2"), 10, "TONNE")  # 4 200 000
        legacy = SimpleNamespace(id="c", offered_price=D("430000"), offered_price_basis=None, pricing_snapshot_version=None)
        c = compare_bid(legacy, 10, "TONNE")

        assert (a.total, b.total) == (D("4500000.00"), D("4200000.00"))
        assert a.label == "450 000 FCFA par tonne" and b.label == "4 200 000 FCFA pour l'ensemble"
        assert c.comparable is False and c.reason == "bid_basis_unknown"
        assert c.reliability == PricingReliability.UNKNOWN_BASIS and "base de prix inconnue" in c.label

        ranked = rank_comparisons([a, c, b])
        assert [r.bid_id for r in ranked] == ["b2", "b1", "c"]  # 4,2 M < 4,5 M ; l'offre sans base vient APRÈS
        assert ranked[0].total == D("4200000.00") and ranked[-1].comparable is False

    def test_the_normalized_price_is_secondary_not_a_replacement(self):
        cmp = compare_bid(bid_row(450000), 10, "TONNE")
        assert cmp.label == "450 000 FCFA par tonne" and cmp.normalized == "450 FCFA/kg"
