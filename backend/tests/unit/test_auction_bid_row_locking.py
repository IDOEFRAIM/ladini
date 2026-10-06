"""Verrouillage `SELECT ... FOR UPDATE` — `AuctionMixin.place_bid`/
`update_bid_price`/`withdraw_bid`/`select_winning_bid` (2026-09-04,
hardening concurrence post-publication SALES).

## Portée EXACTE de cette preuve (honnêteté requise, même discipline que
`test_procurement_draft_persistence.py`)

Ce dépôt n'a AUCUNE infrastructure de test Postgres réelle — confirmé pour
`domain/procurement_draft.py`/`preorder_draft.py` (faux moteur SQL texte),
et ENCORE MOINS disponible ici : `auction.py` utilise l'ORM SQLAlchemy
(requêtes composées, jointures), pas du SQL texte brut — un faux moteur
fidèle à la sémantique `SELECT...FOR UPDATE` réelle de Postgres (blocage
d'un lecteur concurrent tant que la transaction verrouillante n'a pas
commité) n'existe pas et ne serait pas raisonnable à construire ici pour un
seul correctif ciblé.

Ce que CE fichier prouve à la place, honnêtement : la requête SQL
RÉELLEMENT envoyée à Postgres (compilée via le dialecte `postgresql`,
jamais exécutée) porte bien la clause `FOR UPDATE` — preuve que le
correctif est RÉELLEMENT dans le chemin de code exécuté (pas dans un
commentaire, pas dans une branche morte), pas une preuve de comportement
sous concurrence réelle (qui resterait à valider en CI contre une vraie
base, chantier séparé — comme documenté ailleurs pour les drafts)."""
from __future__ import annotations

import types
import uuid

import pytest

from ladini.services.database.auction import AuctionMixin
from tests.conftest import run


def _fake_producer_profile():
    user = types.SimpleNamespace(id=uuid.uuid4(), phone="+22670000001", name="Producteur Test")
    producer = types.SimpleNamespace(id=uuid.uuid4())
    return user, producer


class _CapturingSession:
    """Capture chaque `stmt` passé à `.execute()`/`.scalar()` — ne simule
    AUCUNE sémantique de verrouillage réelle (impossible sans Postgres),
    seulement la FORME de la requête envoyée."""

    def __init__(self, *, scalar_result=None, execute_row=None):
        self.captured_statements = []
        self._scalar_result = scalar_result
        self._execute_row = execute_row

    async def scalar(self, stmt):
        self.captured_statements.append(stmt)
        return self._scalar_result

    async def execute(self, stmt):
        self.captured_statements.append(stmt)
        row = self._execute_row

        class _Result:
            def fetchone(_self):
                return row

        return _Result()

    async def flush(self):
        pass

    def get(self, *_a, **_kw):
        return None


def _compiled_sql(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))


def _service(session):
    class _Svc(AuctionMixin):
        @property
        def session(self):
            return session

    return _Svc()


class TestPlaceBidLocksAuctionAndExistingBid:
    def test_the_auction_lookup_is_for_update(self, monkeypatch):
        session = _CapturingSession(scalar_result=None)  # auction introuvable -> lève après la requête verrouillée
        svc = _service(session)
        monkeypatch.setattr(svc, "get_producer_profile", lambda phone: _async_return(_fake_producer_profile()))

        with pytest.raises(Exception):
            run(svc.place_bid(str(uuid.uuid4()), "+22670000001", 100.0))

        assert session.captured_statements, "aucune requête capturée — la fonction n'a pas atteint la requête verrouillée"
        sql = _compiled_sql(session.captured_statements[0])
        assert "FOR UPDATE" in sql.upper()


async def _async_return(value):
    return value


class TestUpdateBidPriceLocksTheBid:
    def test_the_bid_lookup_is_for_update(self):
        session = _CapturingSession(scalar_result=None)
        svc = _service(session)

        with pytest.raises(Exception):
            run(svc.update_bid_price(str(uuid.uuid4()), "+22670000001", 250.0))

        assert session.captured_statements
        sql = _compiled_sql(session.captured_statements[0])
        assert "FOR UPDATE" in sql.upper()


class TestWithdrawBidLocksTheBid:
    def test_the_bid_lookup_is_for_update(self):
        session = _CapturingSession(scalar_result=None)
        svc = _service(session)

        with pytest.raises(Exception):
            run(svc.withdraw_bid(str(uuid.uuid4()), "+22670000001"))

        assert session.captured_statements
        sql = _compiled_sql(session.captured_statements[0])
        assert "FOR UPDATE" in sql.upper()


class TestSelectWinningBidLocksBidAndAuction:
    def test_the_join_lookup_is_for_update_of_bid_and_auction(self):
        session = _CapturingSession(execute_row=None)  # offre introuvable -> lève après la requête verrouillée
        svc = _service(session)

        with pytest.raises(Exception):
            run(svc.select_winning_bid(bid_id=str(uuid.uuid4()), phone="+22670000001"))

        assert session.captured_statements
        sql = _compiled_sql(session.captured_statements[0])
        assert "FOR UPDATE" in sql.upper()
        # `of=[Bid, Auction]` — vérifie que le verrou est bien SCOPÉ (pas
        # les jointures User/SubCategory, en lecture seule) : Postgres rend
        # `FOR UPDATE OF <table1>, <table2>` quand `of=` est fourni.
        assert "OF" in sql.upper()
