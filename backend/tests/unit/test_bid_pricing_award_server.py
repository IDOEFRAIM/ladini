"""Côté SERVEUR du flux Bid -> Award (Phase B2b) : persistance d'un bid typé, modification (changement de base),
listes qui affichent la base propre à chaque offre, attribution sur décision certifiée, commande gelée, legacy.

Doublures de session fidèles à l'ordre réel des requêtes (voir les fichiers jumeaux `test_place_bid_upsert_semantics`,
`test_auction_loser_notification`) — pas un vrai PostgreSQL (les triggers/CHECK sont couverts par tests/schema/*_pg.py)."""
from __future__ import annotations

import types
import uuid
from decimal import Decimal

import pytest

from ladini.domain.bid_award import CertifiedAwardDecision
from ladini.domain.commercial_pricing_snapshot import bid_pricing_view
from ladini.services.database.auction import AuctionMixin
from ladini.services.database.errors import BusinessRuleException
from tests.conftest import run
from tests.unit.certified_bids import certified_bid_columns

D = Decimal


def _auction(**over):
    base = dict(
        id=uuid.uuid4(), status="OPEN", buyer_id=uuid.uuid4(), target_zone_id=None, sub_category_id=uuid.uuid4(),
        quantity=10.0, unit="TONNE", winner_bid_id=None, images=[], max_price_per_unit=500000.0,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def _bid(auction, *, amount=450000, basis="PER_BASE_UNIT", legacy=False, unit=None, **over):
    unit = unit or auction.unit
    base = dict(
        id=uuid.uuid4(), auction_id=auction.id, producer_id=uuid.uuid4(), status="PENDING", is_winner=False,
        message=None, images=[],
    )
    if legacy:
        base.update(offered_price=D(str(amount)), offered_price_basis=None, pricing_snapshot_version=None)
    else:
        base.update(certified_bid_columns(amount, unit=unit, quantity=auction.quantity, basis=basis))
    base.update(over)
    return types.SimpleNamespace(**base)


async def _async_return(value):
    return value


# =====================================================================
# place_bid / update_bid_price
# =====================================================================


class _FakePlaceBidSession:
    def __init__(self, auction, existing_bid=None):
        self._auction, self._existing, self._n = auction, existing_bid, 0
        self.added: list = []

    async def scalar(self, _stmt):
        self._n += 1
        return self._auction if self._n == 1 else self._existing

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


def _svc(session, **attrs):
    class _Svc(AuctionMixin):
        @property
        def session(self):
            return session

    svc = _Svc()
    svc.get_producer_profile = lambda phone: _async_return(
        (types.SimpleNamespace(id=uuid.uuid4()), types.SimpleNamespace(id=uuid.uuid4()))
    )
    for k, v in attrs.items():
        setattr(svc, k, v)
    return svc


class TestPlaceBidRequiresABasis:
    def test_a_new_bid_without_a_basis_is_refused_and_nothing_is_written(self):
        auction = _auction()
        session = _FakePlaceBidSession(auction)
        with pytest.raises(BusinessRuleException) as info:
            run(_svc(session).place_bid(auction_id=str(auction.id), phone="+22670000001", offered_price=450000))
        assert info.value.reason == "price_basis_required"
        assert session.added == []

    def test_450000_per_tonne_persists_with_its_semantics(self):
        auction = _auction()
        session = _FakePlaceBidSession(auction)
        run(_svc(session).place_bid(
            auction_id=str(auction.id), phone="+22670000001", offered_price=450000,
            price_basis="PER_BASE_UNIT", price_unit="TONNE",
        ))
        (bid,) = session.added
        assert (bid.offered_price, bid.offered_price_basis, bid.offered_price_unit) == (D("450000"), "PER_BASE_UNIT", "TONNE")
        assert bid.offered_price_currency == "XOF" and bid.pricing_snapshot_version == 1
        assert bid.normalized_unit_price == D("450.0000") and bid.normalized_unit == "KG"

    def test_a_total_lot_bid_has_no_price_unit(self):
        auction = _auction()
        session = _FakePlaceBidSession(auction)
        run(_svc(session).place_bid(
            auction_id=str(auction.id), phone="+22670000001", offered_price=4_500_000, price_basis="TOTAL_LOT",
        ))
        (bid,) = session.added
        assert bid.offered_price_basis == "TOTAL_LOT" and bid.offered_price_unit is None

    def test_a_basis_incompatible_with_the_auction_unit_is_refused(self):
        auction = _auction(unit="KG", quantity=1000.0)
        session = _FakePlaceBidSession(auction)
        with pytest.raises(BusinessRuleException):
            run(_svc(session).place_bid(
                auction_id=str(auction.id), phone="+22670000001", offered_price=500,
                price_basis="PER_BASE_UNIT", price_unit="LITRE",
            ))
        assert session.added == []

    def test_a_package_bid_needs_its_package(self):
        auction = _auction(unit="KG", quantity=1000.0)
        session = _FakePlaceBidSession(auction)
        with pytest.raises(BusinessRuleException):
            run(_svc(session).place_bid(
                auction_id=str(auction.id), phone="+22670000001", offered_price=12000, price_basis="PER_PACKAGE",
            ))
        session = _FakePlaceBidSession(auction)  # nouvelle session: le compteur d'appels est propre à un place_bid
        run(_svc(session).place_bid(
            auction_id=str(auction.id), phone="+22670000001", offered_price=12000, price_basis="PER_PACKAGE",
            package_type="caisse", package_content_amount=25, package_content_unit="KG",
        ))
        (bid,) = session.added
        assert (bid.package_type, bid.package_content_amount) == ("CAISSE", D("25"))

    def test_a_legacy_bid_can_be_requalified_by_giving_its_basis(self):
        auction = _auction()
        legacy = _bid(auction, legacy=True, amount=450000)
        session = _FakePlaceBidSession(auction, legacy)
        run(_svc(session).place_bid(
            auction_id=str(auction.id), phone="+22670000001", offered_price=450000,
            price_basis="PER_BASE_UNIT", price_unit="TONNE",
        ))
        assert legacy.offered_price_basis == "PER_BASE_UNIT" and legacy.pricing_snapshot_version == 1


class _FakeSingleBidSession:
    def __init__(self, bid, auction):
        self._bid, self._auction = bid, auction

    async def scalar(self, _stmt):
        return self._bid

    async def get(self, _model, _pk):
        return self._auction

    async def flush(self):
        pass


class TestUpdateBidPrice:
    def test_a_correction_keeps_the_basis_and_recomputes_the_snapshot(self):
        auction = _auction()
        bid = _bid(auction, amount=450000)
        run(_svc(_FakeSingleBidSession(bid, auction)).update_bid_price(
            bid_id=str(bid.id), phone="+22670000001", new_price=430000,
        ))
        assert (bid.offered_price, bid.offered_price_basis, bid.offered_price_unit) == (D("430000"), "PER_BASE_UNIT", "TONNE")
        assert bid.normalized_unit_price == D("430.0000")

    def test_the_basis_can_change_from_per_tonne_to_total_lot(self):
        auction = _auction()
        bid = _bid(auction, amount=450000)
        run(_svc(_FakeSingleBidSession(bid, auction)).update_bid_price(
            bid_id=str(bid.id), phone="+22670000001", new_price=4_000_000, price_basis="TOTAL_LOT",
        ))
        assert bid.offered_price_basis == "TOTAL_LOT" and bid.offered_price_unit is None
        assert bid.offered_price == D("4000000")
        assert bid_pricing_view(bid).snapshot.total_for(10, "TONNE") == D("4000000.00")

    def test_a_legacy_bid_modified_without_a_basis_stays_unknown_never_guessed(self):
        auction = _auction()
        legacy = _bid(auction, legacy=True, amount=450000)
        run(_svc(_FakeSingleBidSession(legacy, auction)).update_bid_price(
            bid_id=str(legacy.id), phone="+22670000001", new_price=430000,
        ))
        assert legacy.offered_price == 430000.0
        assert legacy.offered_price_basis is None and legacy.pricing_snapshot_version is None

    def test_a_legacy_bid_modified_with_a_basis_is_requalified(self):
        auction = _auction()
        legacy = _bid(auction, legacy=True, amount=450000)
        run(_svc(_FakeSingleBidSession(legacy, auction)).update_bid_price(
            bid_id=str(legacy.id), phone="+22670000001", new_price=450000,
            price_basis="PER_BASE_UNIT", price_unit="TONNE",
        ))
        assert legacy.offered_price_basis == "PER_BASE_UNIT" and legacy.pricing_snapshot_version == 1


# =====================================================================
# select_winning_bid
# =====================================================================


class _FakeSelectSession:
    def __init__(self, row):
        from sqlalchemy.sql.dml import Update as _UpdateStmt

        self._row, self._Update, self._served = row, _UpdateStmt, False
        self.added: list = []

    async def scalar(self, _stmt):
        return "+22670000099"  # propriétaire de l'enchère = l'appelant

    async def execute(self, stmt):
        if isinstance(stmt, self._Update):
            return types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: []))
        if not self._served:
            self._served = True
            row = self._row
            return types.SimpleNamespace(fetchone=lambda: row)
        return types.SimpleNamespace(all=lambda: [])

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


def _select(bid, auction, **kwargs):
    session = _FakeSelectSession((bid, auction, "Gilbert-prod", "+22670000001", types.SimpleNamespace(name="Maïs")))
    result = None
    exc = None
    try:
        result = run(_svc(session).select_winning_bid(bid_id=str(bid.id), phone="+22670000099", **kwargs))
    except BusinessRuleException as e:
        exc = e
    orders = [o for o in session.added if hasattr(o, "award_pricing_snapshot")]
    return result, exc, orders


@pytest.fixture(autouse=True)
def _no_outbox(monkeypatch):
    async def _fake_enqueue(_session, entries):
        return len(entries)

    monkeypatch.setattr("ladini.workers.repositories.outbox_repo.enqueue", _fake_enqueue)


class TestAwardUsesTheBidBasis:
    def test_per_tonne_award_total_and_frozen_snapshot(self):
        auction = _auction()
        bid = _bid(auction, amount=450000)
        result, exc, orders = _select(bid, auction)
        assert exc is None and result["status"] == "success"
        (order,) = orders
        assert order.total_amount == 4_500_000.0
        frozen = order.award_pricing_snapshot
        assert (frozen["price_basis"], frozen["price_unit"], frozen["commercial_price_amount"]) == ("PER_BASE_UNIT", "TONNE", "450000")
        assert frozen["award"]["auction_quantity"] == "10" and frozen["award"]["total_amount"] == "4500000.00"

    def test_total_lot_award_is_not_multiplied_by_the_quantity(self):
        auction = _auction()
        bid = _bid(auction, amount=4_200_000, basis="TOTAL_LOT")
        _, exc, orders = _select(bid, auction)
        assert exc is None
        assert orders[0].total_amount == 4_200_000.0 and orders[0].award_pricing_snapshot["price_basis"] == "TOTAL_LOT"

    def test_per_tonne_bid_on_a_kg_auction_keeps_450000_per_tonne_and_totals_450000(self):
        auction = _auction(unit="KG", quantity=1000.0)
        from ladini.domain.commercial_pricing_snapshot import build_bid_pricing_snapshot

        bid = _bid(auction, legacy=True)
        bid.__dict__.update(build_bid_pricing_snapshot(  # « 450000 la tonne » sur une enchère de 1000 KG
            amount=450000, basis="PER_BASE_UNIT", price_unit="TONNE", auction_quantity=1000, auction_unit="KG",
        ).to_bid_columns())
        _, exc, orders = _select(bid, auction)
        assert exc is None
        assert orders[0].total_amount == 450000.0
        assert orders[0].award_pricing_snapshot["price_unit"] == "TONNE"
        assert orders[0].award_pricing_snapshot["normalized_unit_price"] == "450"

    def test_an_inconsistent_stored_basis_is_refused_not_awarded(self):
        auction = _auction(unit="LITRE", quantity=100.0)
        bid = _bid(auction, amount=450000, unit="TONNE", quantity=10)  # bid par TONNE sur une enchère en LITRE
        _, exc, orders = _select(bid, auction)
        assert exc is not None and exc.reason == "award_pricing_invalid" and orders == []


class TestLegacyBidsAreNeverAwarded:
    def test_a_bid_without_a_basis_is_blocked_with_a_requalification_reason(self):
        auction = _auction()
        legacy = _bid(auction, legacy=True, amount=450000)
        _, exc, orders = _select(legacy, auction)
        assert exc is not None and exc.reason == "bid_basis_unknown"
        assert "préciser" in str(exc) and orders == []
        assert auction.status == "OPEN" and legacy.status == "PENDING"  # rien n'a bougé


class TestConfirmedTermsAreRevalidatedUnderLock:
    def _expected(self, auction, bid):
        from ladini.services.database.pricing_persistence import award_decision_for

        return {"fingerprint": award_decision_for(bid, auction).fingerprint}

    def test_matching_terms_execute(self):
        auction = _auction()
        bid = _bid(auction, amount=450000)
        _, exc, orders = _select(bid, auction, expected_award=self._expected(auction, bid))
        assert exc is None and len(orders) == 1

    def test_a_price_changed_after_confirmation_is_refused(self):
        auction = _auction()
        bid = _bid(auction, amount=450000)
        expected = self._expected(auction, bid)
        bid.offered_price = D("430000")  # le producteur a modifié entre-temps
        _, exc, orders = _select(bid, auction, expected_award=expected)
        assert exc.reason == "award_terms_changed" and orders == []
        assert auction.status == "OPEN"

    def test_a_basis_changed_after_confirmation_is_refused_even_with_the_same_amount(self):
        auction = _auction()
        bid = _bid(auction, amount=450000)
        expected = self._expected(auction, bid)
        bid.__dict__.update(certified_bid_columns(450000, unit="TONNE", quantity=10, basis="TOTAL_LOT"))
        _, exc, orders = _select(bid, auction, expected_award=expected)
        assert exc.reason == "award_terms_changed" and orders == []

    def test_a_second_confirmation_creates_no_second_order(self):
        auction = _auction()
        bid = _bid(auction, amount=450000)
        first = _select(bid, auction)
        assert first[1] is None and len(first[2]) == 1
        second = _select(bid, auction)  # même objet: l'enchère est désormais CLOSED
        assert second[1] is not None and second[1].reason == "auction_already_closed" and second[2] == []


# =====================================================================
# Listes & comparaison
# =====================================================================


class _FakeBidsListSession:
    def __init__(self, auction, bids):
        self._auction, self._bids = auction, bids

    async def execute(self, _stmt):
        auction, bids = self._auction, self._bids

        class _R:
            def fetchone(self_inner):
                return (auction, "Maïs")

            def __iter__(self_inner):
                return iter([(b, f"Producteur {i}") for i, b in enumerate(bids, 1)])

        return _R()


class TestBidLists:
    def test_each_bid_shows_its_own_semantics_and_is_ranked_by_comparable_total(self):
        auction = _auction()
        a = _bid(auction, amount=450000)  # 4 500 000
        b = _bid(auction, amount=4_200_000, basis="TOTAL_LOT")  # 4 200 000
        c = _bid(auction, legacy=True, amount=430000)  # base inconnue
        out = run(_svc(_FakeBidsListSession(auction, [c, a, b])).get_auction_bids(str(auction.id), phone="+22670000099"))

        labels = [x["pricing_label"] for x in out["bids"]]
        assert labels[0] == "4 200 000 FCFA pour l'ensemble"
        assert labels[1] == "450 000 FCFA par tonne"
        assert "base de prix inconnue" in labels[2]
        assert [x["requires_requalification"] for x in out["bids"]] == [False, False, True]
        assert [x["comparable_total"] for x in out["bids"]] == ["4200000.00", "4500000.00", None]
        # la décision certifiée n'existe que pour les offres attribuables
        assert out["bids"][0]["award_decision"] and out["bids"][2]["award_decision"] is None
        assert CertifiedAwardDecision.from_state(out["bids"][1]["award_decision"]).award_total == D("4500000.00")

    def test_a_kg_auction_shows_the_tonne_price_not_a_flattened_kg_price(self):
        auction = _auction(unit="KG", quantity=1000.0)
        from ladini.domain.commercial_pricing_snapshot import build_bid_pricing_snapshot

        bid = _bid(auction, legacy=True)
        bid.__dict__.update(build_bid_pricing_snapshot(
            amount=450000, basis="PER_BASE_UNIT", price_unit="TONNE", auction_quantity=1000, auction_unit="KG",
        ).to_bid_columns())
        out = run(_svc(_FakeBidsListSession(auction, [bid])).get_auction_bids(str(auction.id), phone="+22670000099"))
        row = out["bids"][0]
        assert row["pricing_label"] == "450 000 FCFA par tonne" and row["comparable_total"] == "450000.00"
        assert row["normalized_label"] == "450 FCFA/kg"
