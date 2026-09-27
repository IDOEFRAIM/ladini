"""`AuctionMixin.place_bid` — confirmation comportementale (pas seulement la
forme SQL) du modèle métier "un seul bid actif par producteur et par
enchère" (2026-09-04, audit fonctionnel/transactionnel Auction↔Bid, mandat
§5 : "Détermine si le métier autorise one bid per producer ou plusieurs
bids. Ne suppose rien.").

## Ce que confirme ce fichier

`bids_auction_producer_unique` (`domain/orders/models.py`, UNIQUE sur
`(auction_id, producer_id)`) EST la règle métier réelle — `place_bid`
l'implémente via un UPSERT explicite (relit `existing_bid` sous
`FOR UPDATE`, met à jour son prix au lieu d'un second INSERT) plutôt que de
compter sur l'IntegrityError de la contrainte pour refuser un second appel.
Ce fichier verrouille CE comportement au niveau métier, pas seulement la
présence du verrou (déjà prouvée par `test_auction_bid_row_locking.py`)."""
from __future__ import annotations

import types
import uuid

from tests.conftest import run

from ladini.services.database.auction import AuctionMixin


def _auction(**overrides):
    base = dict(
        id=uuid.uuid4(), status="OPEN", buyer_id=uuid.uuid4(), target_zone_id=None,
        sub_category_id=uuid.uuid4(), quantity=10.0, unit="TONNE",
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _existing_bid(**overrides):
    base = dict(
        id=uuid.uuid4(), offered_price=250.0, status="PENDING", message=None,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


async def _async_return(value):
    return value


class _FakePlaceBidSession:
    """1er `.scalar()` -> Auction ; 2e -> le bid existant (ou None) — même
    ordre que `place_bid` (voir son propre docstring)."""

    def __init__(self, auction, existing_bid):
        self._auction = auction
        self._existing_bid = existing_bid
        self._n = 0
        self.added: list = []

    async def scalar(self, _stmt):
        self._n += 1
        if self._n == 1:
            return self._auction
        return self._existing_bid

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


def _producer_profile():
    user = types.SimpleNamespace(id=uuid.uuid4())
    producer = types.SimpleNamespace(id=uuid.uuid4())
    return user, producer


def _service(session):
    class _Svc(AuctionMixin):
        @property
        def session(self):
            return session

    svc = _Svc()
    svc.get_producer_profile = lambda phone: _async_return(_producer_profile())
    return svc


class TestOneBidPerProducerPerAuction:
    def test_a_second_call_from_the_same_producer_updates_the_existing_bid_not_a_new_row(
        self,
    ):
        auction = _auction()
        existing = _existing_bid(offered_price=250.0)
        session = _FakePlaceBidSession(auction, existing)
        svc = _service(session)

        result = run(
            svc.place_bid(auction_id=str(auction.id), phone="+22670000001", offered_price=300.0)
        )

        assert result["status"] == "success"
        assert result.get("updated") is True
        assert result["bid_id"] == str(existing.id)
        assert existing.offered_price == 300.0
        # Aucune nouvelle ligne `Bid` ajoutée — la contrainte unique
        # (auction_id, producer_id) n'est jamais approchée par un second
        # INSERT, elle n'a même pas à intervenir.
        assert not any(type(o).__name__ == "Bid" for o in session.added)

    def test_a_first_call_creates_exactly_one_new_bid_row(self):
        auction = _auction()
        session = _FakePlaceBidSession(auction, existing_bid=None)
        svc = _service(session)

        result = run(
            svc.place_bid(auction_id=str(auction.id), phone="+22670000001", offered_price=250.0)
        )

        assert result["status"] == "success"
        assert not result.get("updated")
        new_bids = [o for o in session.added if type(o).__name__ == "Bid"]
        assert len(new_bids) == 1
        assert new_bids[0].offered_price == 250.0
        assert new_bids[0].status == "PENDING"

    def test_updating_a_bid_already_processed_is_refused(self):
        """Un bid déjà WINNING/LOST/WITHDRAWN ne peut plus être "corrigé"
        par un nouvel appel `place_bid` — même producteur, même enchère,
        mais l'engagement est déjà tranché."""
        from ladini.services.database.errors import BusinessRuleException
        import pytest

        auction = _auction()
        existing = _existing_bid(status="WINNING")
        session = _FakePlaceBidSession(auction, existing)
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc_info:
            run(
                svc.place_bid(
                    auction_id=str(auction.id), phone="+22670000001", offered_price=999.0
                )
            )
        assert exc_info.value.reason == "bid_already_processed"
        assert existing.offered_price == 250.0  # jamais modifié
