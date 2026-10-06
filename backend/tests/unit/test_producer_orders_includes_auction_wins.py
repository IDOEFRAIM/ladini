"""`get_producer_orders` — les commandes nées d'un `select_winning_bid`
doivent apparaître dans "mes commandes" côté producteur (2026-09-04, audit
Auction/Bid, volet post-winner read side).

## Le gap réel fermé par ce fichier

`select_winning_bid` (`services/database/auction.py`) crée un `Order` avec
`auction_id`/`winning_bid_id` posés — AUCUN `OrderItem` (pas de catalogue
`Product` pour une ligne d'appel d'offres), AUCUN `market_offer_id` (réservé
aux productions futures, `reserve_future_offer`). `get_producer_orders`
n'avait que DEUX sources de candidats (`product_order_ids` via `OrderItem`,
`cycle_order_ids` via `market_offer_id`) — NI L'UNE NI L'AUTRE n'atteint
une commande d'appel d'offres. Un producteur qui remporte une enchère ne la
voyait donc JAMAIS dans "mes commandes" — seule la notification Outbox
ponctuelle (`AUCTION_WON_PRODUCER`) l'en informait, une seule fois, au
moment du gain.

## Portée honnête

Même technique que `test_producer_order_reads_exclude_draft_status.py` —
preuve par SQL réellement compilé (aucune infrastructure Postgres de
test dans ce dépôt)."""
from __future__ import annotations

import types
import uuid

from ladini.services.database.producer import ProducerMgmtMixin
from tests.conftest import run


def _fake_producer_profile():
    user = types.SimpleNamespace(id=uuid.uuid4(), phone="+22670000001", name="Producteur Test")
    producer = types.SimpleNamespace(id=uuid.uuid4())
    return user, producer


async def _async_return(value):
    return value


def _compiled_sql_literal(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


class _EmptyResult:
    def scalars(self):
        return self

    def unique(self):
        return self

    def all(self):
        return []


class _CapturingSession:
    def __init__(self) -> None:
        self.captured_statements: list = []

    async def execute(self, stmt):
        self.captured_statements.append(stmt)
        return _EmptyResult()


def _service(session):
    class _Svc(ProducerMgmtMixin):
        @property
        def session(self):
            return session

    svc = _Svc()
    svc._resolve_producer_phone = lambda phone=None, producer_id=None: _async_return(
        phone or "+22670000001"
    )
    svc.get_producer_profile = lambda phone: _async_return(_fake_producer_profile())
    return svc


class TestGetProducerOrdersReachesAuctionDerivedOrders:
    def test_the_query_includes_orders_reached_via_winning_bid(self):
        session = _CapturingSession()
        svc = _service(session)

        result = run(svc.get_producer_orders(phone="+22670000001"))

        assert result["status"] == "success"
        sql = _compiled_sql_literal(session.captured_statements[0]).upper()
        # La nouvelle source de candidats rejoint `bids` sur
        # `winning_bid_id` — preuve que le chemin auction-order existe
        # désormais dans la requête compilée.
        assert "WINNING_BID_ID" in sql
        assert "BIDS" in sql

    def test_still_excludes_draft_and_superseded_by_default(self):
        """Non-régression du correctif précédent (lecture DRAFT/SUPERSEDED) —
        les deux corrections doivent coexister sans se marcher dessus."""
        session = _CapturingSession()
        svc = _service(session)

        result = run(svc.get_producer_orders(phone="+22670000001"))

        assert result["status"] == "success"
        sql = _compiled_sql_literal(session.captured_statements[0]).upper()
        assert "NOT IN" in sql
        assert "DRAFT" in sql and "SUPERSEDED" in sql
