"""Parcours complet Auction/Bid — de la mise en concurrence à la sélection
du gagnant, PUIS toutes les tentatives post-gagnant qui doivent être
refusées (2026-09-04, audit Auction/Bid, clôture — mandat §16 "TEST
END-TO-END FINAL").

## Scénario RÉEL (mandat, verbatim)

```
PRODUCER A -> bid 250
PRODUCER B -> bid 275
PRODUCER A -> update to 300
BUYER -> select A
```
puis, sur l'état RÉSULTANT (`auction` CLOSED, `bid_a` WINNING) :
```
select B      -> refusé
update A      -> refusé
withdraw A    -> refusé
place new bid -> refusé
cancel auction -> refusé
```

## Ce que ce fichier prouve, précisément

Chaque étape RÉUTILISE les MÊMES objets Python (`auction`/`bid_a`/`bid_b`)
d'une étape à l'autre — les mutations d'une étape (ex: `bid_a.offered_price
= 300.0`, `auction.status = "CLOSED"`) sont donc RÉELLEMENT visibles par
l'étape suivante, exactement comme des lignes Postgres partagées entre
deux appels séquentiels dans la même "session" logique. Chaque étape garde
son propre faux moteur MINIMAL (même convention que les autres fichiers de
cette suite, aucune simulation de filtrage SQL réel) — la NOUVELLE preuve
apportée ici est la CHAÎNE complète, pas une fonction isolée."""
from __future__ import annotations

import types
import uuid

import pytest

from tests.conftest import run

from agriconnect.services.database.auction import AuctionMixin
from agriconnect.services.database.errors import BusinessRuleException


async def _async_return(value):
    return value


def _auction(**overrides):
    base = dict(
        id=uuid.uuid4(), buyer_id=uuid.uuid4(), quantity=10.0, unit="TONNE",
        target_zone_id=None, status="OPEN", winner_bid_id=None,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _FakePlaceBidSession:
    """1er `.scalar()` -> Auction, 2e -> bid existant du producteur (ou
    None) — même ordre que `place_bid`."""

    def __init__(self, auction, existing_bid):
        self._auction = auction
        self._existing_bid = existing_bid
        self._n = 0
        self.added: list = []

    async def scalar(self, _stmt):
        self._n += 1
        return self._auction if self._n == 1 else self._existing_bid

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


class _FakeSingleBidSession:
    """`update_bid_price`/`withdraw_bid` — une seule requête `.scalar()`."""

    def __init__(self, bid):
        self._bid = bid

    async def scalar(self, _stmt):
        return self._bid

    async def flush(self):
        pass


class _FakeSelectWinningBidSession:
    """`select_winning_bid` — 1 `SELECT ... FOR UPDATE` (fetchone), puis un
    bulk UPDATE et un INSERT outbox distingués par type (voir les fichiers
    jumeaux de cette suite)."""

    def __init__(self, row):
        from sqlalchemy.sql.dml import Update as _UpdateStmt

        self._row = row
        self._UpdateStmt = _UpdateStmt
        self.added: list = []
        self._select_served = False

    async def execute(self, stmt):
        if isinstance(stmt, self._UpdateStmt):
            return types.SimpleNamespace(rowcount=0, scalars=lambda: types.SimpleNamespace(all=lambda: []))
        if not self._select_served:
            self._select_served = True
            row = self._row
            return types.SimpleNamespace(fetchone=lambda: row)
        return types.SimpleNamespace(all=lambda: [])

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


class _FakeCancelAuctionSession:
    def __init__(self, auction):
        self._auction = auction

    async def scalar(self, _stmt):
        return self._auction


def _service(session, *, producer=None, phone=None):
    class _Svc(AuctionMixin):
        @property
        def session(self):
            return session

    svc = _Svc()
    if producer is not None:
        user = types.SimpleNamespace(id=uuid.uuid4())
        svc.get_producer_profile = lambda phone=None: _async_return((user, producer))
    return svc


class TestFullAuctionLifecycleThenAllPostWinnerAttemptsAreRejected:
    def test_full_scenario(self):
        buyer_phone = "+22670000099"
        phone_a, phone_b = "+22670000001", "+22670000002"
        producer_a = types.SimpleNamespace(id=uuid.uuid4())
        producer_b = types.SimpleNamespace(id=uuid.uuid4())
        auction = _auction()

        # ── PRODUCER A -> bid 250 ────────────────────────────────────────
        svc_a = _service(_FakePlaceBidSession(auction, None), producer=producer_a)
        session_a1 = svc_a.session
        result = run(svc_a.place_bid(auction_id=str(auction.id), phone=phone_a, offered_price=250.0))
        assert result["status"] == "success"
        bid_a = session_a1.added[0]
        assert bid_a.offered_price == 250.0

        # ── PRODUCER B -> bid 275 ────────────────────────────────────────
        svc_b = _service(_FakePlaceBidSession(auction, None), producer=producer_b)
        session_b1 = svc_b.session
        run(svc_b.place_bid(auction_id=str(auction.id), phone=phone_b, offered_price=275.0))
        bid_b = session_b1.added[0]
        assert bid_b.offered_price == 275.0

        # ── PRODUCER A -> update to 300 ──────────────────────────────────
        svc_a2 = _service(_FakeSingleBidSession(bid_a))
        run(svc_a2.update_bid_price(bid_id=str(bid_a.id), phone=phone_a, new_price=300.0))
        assert bid_a.offered_price == 300.0

        # ── BUYER -> select A ────────────────────────────────────────────
        sub_cat = types.SimpleNamespace(name="Riz")
        row = (bid_a, auction, "Producteur A", phone_a, sub_cat)
        svc_buyer = _service(_FakeSelectWinningBidSession(row))
        select_session = svc_buyer.session
        win = run(svc_buyer.select_winning_bid(bid_id=str(bid_a.id), phone=buyer_phone))

        assert win["status"] == "success"
        assert bid_a.is_winner is True
        assert bid_a.status == "WINNING"
        assert auction.status == "CLOSED"
        assert auction.winner_bid_id == bid_a.id
        orders = [o for o in select_session.added if type(o).__name__ == "Order"]
        assert len(orders) == 1
        order = orders[0]
        # Order.bid_id/auction_id == exactement la cible sélectionnée —
        # jamais une autre ligne, jamais un ancien payload.
        assert order.winning_bid_id == bid_a.id
        assert order.auction_id == auction.id
        assert order.buyer_id == auction.buyer_id
        assert order.total_amount == 300.0 * 10.0  # prix FINAL (300), pas 250 ni 275
        assert "3000" in win["summary_buyer"]

        # Double représentation de la décision gagnante (mandat §13) —
        # `Auction.winner_bid_id`/`Order.winning_bid_id` sont écrits DANS LA
        # MÊME transaction à partir du MÊME `bid.id` : redondance acceptable
        # (jamais deux sources concurrentes), invariant vérifié ici.
        assert auction.winner_bid_id == order.winning_bid_id == bid_a.id

        # ══════════════════════════════════════════════════════════════
        # Toutes les tentatives suivantes doivent être refusées — AUCUNE
        # ne doit produire un second Order, un second gagnant, ou muter
        # silencieusement bid_a/auction.
        # ══════════════════════════════════════════════════════════════

        # -- select B : l'enchère est déjà CLOSED --
        row_b = (bid_b, auction, "Producteur B", phone_b, sub_cat)
        svc_select_b = _service(_FakeSelectWinningBidSession(row_b))
        with pytest.raises(BusinessRuleException) as exc:
            run(svc_select_b.select_winning_bid(bid_id=str(bid_b.id), phone=buyer_phone))
        assert exc.value.reason == "auction_already_closed"
        assert bid_b.is_winner is False
        assert auction.winner_bid_id == bid_a.id  # jamais réassigné

        # -- update A : le bid gagnant n'est plus PENDING --
        svc_update_a2 = _service(_FakeSingleBidSession(bid_a))
        with pytest.raises(BusinessRuleException) as exc:
            run(svc_update_a2.update_bid_price(bid_id=str(bid_a.id), phone=phone_a, new_price=999.0))
        assert "déjà traitée" in str(exc.value)
        assert bid_a.offered_price == 300.0  # jamais modifié après la sélection

        # -- withdraw A : idem, plus PENDING --
        svc_withdraw_a = _service(_FakeSingleBidSession(bid_a))
        with pytest.raises(BusinessRuleException) as exc:
            run(svc_withdraw_a.withdraw_bid(bid_id=str(bid_a.id), phone=phone_a))
        assert "déjà traitée" in str(exc.value)
        assert bid_a.status == "WINNING"  # jamais rétrogradé

        # -- place new bid (producteur C) : l'enchère n'est plus OPEN --
        producer_c = types.SimpleNamespace(id=uuid.uuid4())
        svc_place_c = _service(_FakePlaceBidSession(auction, None), producer=producer_c)
        with pytest.raises(BusinessRuleException):
            run(svc_place_c.place_bid(auction_id=str(auction.id), phone="+22670000003", offered_price=280.0))

        # -- cancel_auction (acheteur) : l'enchère n'est plus OPEN --
        svc_cancel = _service(_FakeCancelAuctionSession(auction))
        with pytest.raises(BusinessRuleException):
            run(svc_cancel.cancel_auction(str(auction.id), buyer_phone))
        assert auction.status == "CLOSED"  # jamais écrasé par CANCELLED
