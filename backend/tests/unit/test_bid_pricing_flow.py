"""Certification du prix d'un bid conversationnel (Phase B2b) — domaine pur.

Ce que ces tests verrouillent : « 450000 la tonne » a une BASE, « 4,5 millions pour tout » est un lot entier,
un montant nu n'a de base que si la QUESTION POSÉE la fixe, et rien d'ambigu n'est jamais résolu par
devinette (ni par un indice du LLM)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from ladini.domain.bid_pricing_flow import (
    BidPriceContext,
    BidPriceParse,
    BidPriceStatus,
    find_amounts,
    parse_bid_price,
    render_pricing_label,
    resolve_basis_reply,
    resolve_package_reply,
)
from ladini.domain.commercial_offer import PriceBasis, Provenance
from ladini.domain.commercial_pricing_snapshot import PricingSnapshotError

D = Decimal
TONNE = dict(auction_unit="TONNE", auction_quantity=10)
KG = dict(auction_unit="KG", auction_quantity=1000)


def parse(text, context=None, **auction):
    return parse_bid_price(text, context=context, **(auction or TONNE))


class TestAmountExtraction:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("450000", D("450000")),
            ("450 000", D("450000")),
            ("450.000", D("450000")),
            ("4 500 000", D("4500000")),
            ("4,5 millions", D("4500000")),
            ("4,2 millions", D("4200000")),
            ("450 mille", D("450000")),
            ("450k", D("450000")),
            ("12,5", D("12.5")),
        ],
    )
    def test_french_number_notations(self, text, expected):
        assert find_amounts(text)[0].value == expected


class TestExplicitPerUnit:
    @pytest.mark.parametrize("text", ["450000 la tonne", "450 000 fcfa la tonne", "450000 fcfa/tonne", "je propose 450000 par tonne"])
    def test_450000_per_tonne(self, text):
        r = parse(text)
        assert r.status == BidPriceStatus.RESOLVED
        assert (r.amount, r.basis, r.price_unit, r.source) == (D("450000"), PriceBasis.PER_BASE_UNIT, "TONNE", Provenance.USER_EXPLICIT)

    def test_the_quantity_in_the_message_is_not_the_price(self):
        r = parse("pour les 10 tonnes je propose 450000 la tonne")
        assert (r.amount, r.price_unit) == (D("450000"), "TONNE")

    def test_450000_per_tonne_on_a_kg_auction_is_compatible(self):
        r = parse("450000 la tonne", **KG)
        assert r.is_resolved and r.price_unit == "TONNE"
        snap = r.snapshot(1000, "KG")
        assert snap.commercial_price_amount == D("450000") and snap.price_unit == "TONNE"
        assert snap.normalized_unit_price == D("450.0000") and snap.normalized_unit == "KG"
        assert snap.total_for(1000, "KG") == D("450000.00")


class TestTotalLot:
    @pytest.mark.parametrize(
        "text,amount",
        [
            ("4,5 millions pour tout", D("4500000")),
            ("4 500 000 pour l'ensemble", D("4500000")),
            ("4,2 millions au total", D("4200000")),
            ("4200000 en tout", D("4200000")),
        ],
    )
    def test_whole_lot_is_never_converted_to_a_unit_price(self, text, amount):
        r = parse(text)
        assert r.status == BidPriceStatus.RESOLVED
        assert (r.amount, r.basis, r.price_unit, r.source) == (amount, PriceBasis.TOTAL_LOT, None, Provenance.USER_EXPLICIT)
        assert r.snapshot(10, "TONNE").total_for(10, "TONNE") == quantize(amount)

    def test_a_contradiction_is_asked_not_resolved(self):
        assert parse("450000 la tonne pour tout").status == BidPriceStatus.NEEDS_BASIS


def quantize(v):
    return v.quantize(D("0.01"))


class TestBareAmounts:
    def test_after_a_question_that_fixes_the_basis_it_comes_from_the_question(self):
        r = parse("450000", BidPriceContext.per_auction_unit("TONNE"))
        assert (r.basis, r.price_unit, r.source) == (PriceBasis.PER_BASE_UNIT, "TONNE", Provenance.QUESTION_CONTEXT_EXPLICIT)
        assert r.is_resolved

    def test_without_context_it_is_ambiguous_and_names_both_readings(self):
        r = parse("450000")
        assert r.status == BidPriceStatus.NEEDS_BASIS and r.amount == D("450000")
        assert "par tonne" in r.message and "pour l'ensemble" in r.message and "10" in r.message
        with pytest.raises(PricingSnapshotError):
            r.snapshot(10, "TONNE")  # aucune écriture possible avant résolution

    def test_an_llm_basis_hint_never_resolves_an_ambiguity(self):
        r = parse_bid_price("450000", llm_hints={"price_basis": "TOTAL_LOT"}, **TONNE)
        assert r.status == BidPriceStatus.NEEDS_BASIS and r.llm_hint_basis == "TOTAL_LOT"

    def test_a_correction_keeps_the_basis_shown_in_the_recap(self):
        current = parse("4,5 millions pour tout").snapshot(10, "TONNE")
        r = parse("4 millions", BidPriceContext.keep(current))
        assert (r.amount, r.basis, r.source) == (D("4000000"), PriceBasis.TOTAL_LOT, Provenance.QUESTION_CONTEXT_EXPLICIT)

    def test_no_number_is_no_price(self):
        assert parse("je ne sais pas").status == BidPriceStatus.NO_PRICE

    def test_two_unmarked_numbers_are_not_guessed(self):
        r = parse("450000 ou 430000")
        assert r.status == BidPriceStatus.NEEDS_BASIS and r.amount is None


class TestBasisReply:
    def test_per_tonne_reply(self):
        r = resolve_basis_reply("par tonne", amount=D("450000"), **TONNE)
        assert (r.basis, r.price_unit, r.source) == (PriceBasis.PER_BASE_UNIT, "TONNE", Provenance.USER_EXPLICIT)

    def test_whole_lot_reply(self):
        r = resolve_basis_reply("pour l'ensemble", amount=D("450000"), **TONNE)
        assert r.basis == PriceBasis.TOTAL_LOT

    def test_a_reply_that_does_not_decide_asks_again(self):
        assert resolve_basis_reply("oui", amount=D("450000"), **TONNE).status == BidPriceStatus.NEEDS_BASIS

    def test_a_reply_carrying_its_own_price_is_a_new_price(self):
        r = resolve_basis_reply("430000 la tonne", amount=D("450000"), **TONNE)
        assert r.is_resolved and r.amount == D("430000")


class TestIncompatibleUnits:
    def test_500_per_litre_on_a_kg_auction_is_invalid_and_never_written(self):
        r = parse("500 le litre", **KG)
        assert r.status == BidPriceStatus.INVALID and "litre" in r.message
        with pytest.raises(PricingSnapshotError):
            r.snapshot(1000, "KG")


class TestPackageBids:
    def test_12000_the_case_of_25_kg(self):
        r = parse("12000 la caisse de 25 kg", **KG)
        assert r.status == BidPriceStatus.RESOLVED and r.basis == PriceBasis.PER_PACKAGE
        assert (r.package_type, r.package_content_amount, r.package_content_unit) == ("CAISSE", D("25"), "KG")
        snap = r.snapshot(1000, "KG")
        assert snap.total_for(1000, "KG") == D("480000.00")  # 40 caisses × 12 000
        assert render_pricing_label(snap) == "12 000 FCFA par caisse de 25 kg"

    def test_a_package_without_content_asks_for_it_and_is_then_resolved(self):
        r = parse("12000 la caisse", **KG)
        assert r.status == BidPriceStatus.NEEDS_PACKAGE_SIZE and "caisse" in r.message
        done = resolve_package_reply(r, "25 kg", auction_unit="KG")
        assert done.is_resolved and done.package_content_amount == D("25")

    def test_a_non_divisible_package_is_refused_not_rounded(self):
        r = parse("12000 la caisse de 30 kg", auction_unit="KG", auction_quantity=1000)
        with pytest.raises(PricingSnapshotError):
            r.snapshot(1000, "KG")  # 1000 / 30 n'est pas entier

    def test_a_package_of_another_family_is_invalid(self):
        assert parse("12000 la caisse de 10 litres", **KG).status == BidPriceStatus.INVALID


class TestLabels:
    def test_per_unit_and_total_labels_keep_their_own_semantics(self):
        per = parse("450000 la tonne").snapshot(10, "TONNE")
        tot = parse("4,2 millions pour tout").snapshot(10, "TONNE")
        assert render_pricing_label(per) == "450 000 FCFA par tonne"
        assert render_pricing_label(tot) == "4 200 000 FCFA pour l'ensemble"


class TestStateRoundTrip:
    def test_state_is_json_safe_and_restorable(self):
        import json

        r = parse("12000 la caisse de 25 kg", **KG)
        state = json.loads(json.dumps(r.to_state()))
        back = BidPriceParse.from_state(state)
        assert back is not None and back.amount == r.amount and back.package_content_amount == D("25")
        assert back.source == Provenance.USER_EXPLICIT
