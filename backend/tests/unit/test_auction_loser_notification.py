"""`AuctionMixin.select_winning_bid` — notification des producteurs
PERDANTS (F3, 2026-09-04, audit fonctionnel — "producteur perdant une
enchère jamais notifié").

## Le gap réel fermé par ce fichier

Seul le gagnant était notifié (`AUCTION_WON_PRODUCER`) — les producteurs
dont l'offre passe `PENDING`/`WINNING` → `LOST` (le bulk UPDATE déjà
existant, motif "sans cela un producteur non retenu resterait En attente à
vie") n'étaient jamais notifiés de la décision. Corrigé en dérivant la
liste des perdants à notifier DIRECTEMENT de la décision métier
(`UPDATE ... RETURNING Bid.producer_id`) — jamais une requête séparée,
jamais un `for bid: send_whatsapp()` en pleine transaction (Outbox,
même transaction que la commande)."""
from __future__ import annotations

import inspect
import types
import uuid

import pytest

from tests.conftest import run
from tests.unit.certified_bids import certified_bid_columns

from ladini.services.database.auction import AuctionMixin
from ladini.services.database.errors import BusinessRuleException


def _bid(**overrides):
    base = dict(
        id=uuid.uuid4(), auction_id=uuid.uuid4(), producer_id=uuid.uuid4(),
        offered_price=250.0, status="PENDING", is_winner=False,
    )
    base.update(overrides)
    if "offered_price_basis" not in base:
        base.update(certified_bid_columns(base["offered_price"]))
    return types.SimpleNamespace(**base)


def _auction(**overrides):
    base = dict(
        id=uuid.uuid4(), buyer_id=uuid.uuid4(), quantity=10.0, unit="TONNE",
        target_zone_id=None, sub_category_id=uuid.uuid4(), status="OPEN", winner_bid_id=None,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _FakeSelectWinningBidSession:
    """1er `.execute()` -> la requête principale (fetchone) ; puis le bulk
    `UPDATE ... RETURNING` (renvoie EXACTEMENT les `producer_id` passés en
    fixture — représentant ce que la VRAIE clause `WHERE status IN
    (PENDING, WINNING)` aurait réellement affecté, voir
    `TestUpdateWhereClauseExcludesWithdrawn` pour la preuve sur le code
    source) ; puis, s'il y a des perdants, le SELECT téléphone-par-producteur."""

    def __init__(self, row, loser_producer_ids, phone_by_producer):
        from sqlalchemy.sql.dml import Update as _UpdateStmt

        self._row = row
        self._UpdateStmt = _UpdateStmt
        self._loser_producer_ids = list(loser_producer_ids)
        self._phone_by_producer = phone_by_producer
        self._select_served = False
        self.added: list = []

    async def scalar(self, stmt):
        # Résolution du propriétaire acheteur (garde de propriété ajouté à
        # `select_winning_bid`, 2026-09-28) — hors du périmètre testé ici
        # (notification des perdants) : renvoie systématiquement le même
        # téléphone que celui passé en argument par CHAQUE test de ce
        # fichier, pour que le garde de propriété passe sans jamais devenir
        # l'objet du test.
        return "+22670000099"

    async def execute(self, stmt):
        if isinstance(stmt, self._UpdateStmt):
            ids = self._loser_producer_ids
            return types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: ids))
        if not self._select_served:
            self._select_served = True
            row = self._row
            return types.SimpleNamespace(fetchone=lambda: row)
        rows = [(pid, self._phone_by_producer.get(pid)) for pid in self._loser_producer_ids]
        return types.SimpleNamespace(all=lambda: rows)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


def _service(session):
    class _Svc(AuctionMixin):
        @property
        def session(self):
            return session

    return _Svc()


def _capture_outbox(monkeypatch):
    captured: list = []

    async def _fake_enqueue(_session, entries):
        captured.extend(entries)
        return len(entries)

    monkeypatch.setattr(
        "ladini.workers.repositories.outbox_repo.enqueue", _fake_enqueue
    )
    return captured


class TestWinnerAndLoserAreBothNotifiedExactlyOnce:
    def test_a_and_b_bid_select_a_notifies_winner_and_loser(self, monkeypatch):
        captured = _capture_outbox(monkeypatch)

        producer_a_id, producer_b_id = uuid.uuid4(), uuid.uuid4()
        bid_a = _bid(producer_id=producer_a_id, offered_price=300.0)
        auction = _auction()
        sub_cat = types.SimpleNamespace(name="Riz")
        row = (bid_a, auction, "Producteur A", "+22670000001", sub_cat)

        session = _FakeSelectWinningBidSession(
            row,
            loser_producer_ids=[producer_b_id],
            phone_by_producer={producer_b_id: "+22670000002"},
        )
        svc = _service(session)

        result = run(svc.select_winning_bid(bid_id=str(bid_a.id), phone="+22670000099"))

        assert result["status"] == "success"
        assert len(captured) == 2
        winner_entries = [e for e in captured if e["template_key"] == "AUCTION_WON_PRODUCER"]
        loser_entries = [e for e in captured if e["template_key"] == "AUCTION_LOST_PRODUCER"]
        assert len(winner_entries) == 1
        assert winner_entries[0]["recipient_phone"] == "+22670000001"
        assert len(loser_entries) == 1
        assert loser_entries[0]["recipient_phone"] == "+22670000002"
        assert loser_entries[0]["dedupe_key"] == f"AUCTION_LOST:{auction.id}:{producer_b_id}"

    def test_no_losers_means_no_loser_notification_no_crash(self, monkeypatch):
        captured = _capture_outbox(monkeypatch)
        producer_a_id = uuid.uuid4()
        bid_a = _bid(producer_id=producer_a_id)
        auction = _auction()
        sub_cat = types.SimpleNamespace(name="Riz")
        row = (bid_a, auction, "Producteur A", "+22670000001", sub_cat)

        session = _FakeSelectWinningBidSession(row, loser_producer_ids=[], phone_by_producer={})
        svc = _service(session)

        result = run(svc.select_winning_bid(bid_id=str(bid_a.id), phone="+22670000099"))
        assert result["status"] == "success"
        assert len(captured) == 1  # uniquement le gagnant
        assert captured[0]["template_key"] == "AUCTION_WON_PRODUCER"

    def test_multiple_losers_each_get_exactly_one_distinct_notification(self, monkeypatch):
        captured = _capture_outbox(monkeypatch)
        producer_a_id = uuid.uuid4()
        producer_b_id, producer_c_id = uuid.uuid4(), uuid.uuid4()
        bid_a = _bid(producer_id=producer_a_id)
        auction = _auction()
        sub_cat = types.SimpleNamespace(name="Riz")
        row = (bid_a, auction, "Producteur A", "+22670000001", sub_cat)

        session = _FakeSelectWinningBidSession(
            row,
            loser_producer_ids=[producer_b_id, producer_c_id],
            phone_by_producer={producer_b_id: "+22670000002", producer_c_id: "+22670000003"},
        )
        svc = _service(session)

        run(svc.select_winning_bid(bid_id=str(bid_a.id), phone="+22670000099"))

        loser_phones = {
            e["recipient_phone"] for e in captured if e["template_key"] == "AUCTION_LOST_PRODUCER"
        }
        assert loser_phones == {"+22670000002", "+22670000003"}
        dedupe_keys = [
            e["dedupe_key"] for e in captured if e["template_key"] == "AUCTION_LOST_PRODUCER"
        ]
        assert len(dedupe_keys) == len(set(dedupe_keys))  # jamais de doublon


class TestRetryNeverRepublishesNotifications:
    def test_selecting_an_already_closed_auction_sends_nothing(self, monkeypatch):
        captured = _capture_outbox(monkeypatch)
        producer_a_id = uuid.uuid4()
        bid_a = _bid(producer_id=producer_a_id)
        auction = _auction(status="CLOSED")  # déjà clôturée (rejeu)
        sub_cat = types.SimpleNamespace(name="Riz")
        row = (bid_a, auction, "Producteur A", "+22670000001", sub_cat)

        session = _FakeSelectWinningBidSession(row, loser_producer_ids=[], phone_by_producer={})
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc:
            run(svc.select_winning_bid(bid_id=str(bid_a.id), phone="+22670000099"))
        assert exc.value.reason == "auction_already_closed"
        assert not captured


class TestUpdateWhereClauseExcludesWithdrawn:
    def test_the_bulk_loser_update_never_touches_withdrawn_or_already_lost_bids(self):
        """Preuve sur le CODE SOURCE réel (pas une reconstruction manuelle) :
        la clause `WHERE` du bulk UPDATE limite explicitement aux statuts
        `PENDING`/`WINNING` — un bid `WITHDRAWN` (retrait volontaire, déjà
        su du producteur) ne peut structurellement JAMAIS être retourné par
        `RETURNING`, donc jamais notifié "vous avez perdu"."""
        source = inspect.getsource(AuctionMixin.select_winning_bid)
        assert 'Bid.status.in_(["PENDING", "WINNING"])' in source
        assert ".returning(Bid.producer_id)" in source
