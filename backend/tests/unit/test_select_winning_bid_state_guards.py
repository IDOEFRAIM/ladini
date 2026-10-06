"""`AuctionMixin.select_winning_bid` — gardes d'état RÉELLEMENT manquants
avant ce chantier (2026-09-04, audit fonctionnel/transactionnel Auction↔Bid).

## Les deux gaps réels fermés par ce fichier

Le verrou `FOR UPDATE OF Bid, Auction` (mandat de hardening précédent)
protège contre une COURSE entre deux sélections concurrentes — il ne
protège PAS contre une sélection SOLITAIRE sur une cible qui n'aurait
jamais dû être sélectionnable du tout :

1. **`auction.status`** — seul `"CLOSED"` était rejeté. Une enchère
   `EXPIRED` (cron `check_and_expire_auctions`) ou `CANCELLED`
   (`cancel_auction`) n'est ni `"CLOSED"` ni `"OPEN"` — un gagnant pouvait
   quand même y être désigné, créant un `Order` sur une enchère hors de son
   cycle de vie actif.
2. **`bid.status`** — AUCUN garde n'existait. Un producteur retire son
   offre (`withdraw_bid` → `WITHDRAWN`) SANS fermer l'enchère (elle reste
   `OPEN`, d'autres offres peuvent arriver) : rien n'empêchait de désigner
   CETTE offre retirée comme gagnante — état impossible explicitement cité
   par l'audit (« withdrawn + winning order »).

## Portée honnête

Faux moteur minimal, capturant le tuple `(Bid, Auction, producer_name,
producer_phone, SubCategory)` que `select_winning_bid` attend de sa requête
principale, et distinguant les 3 formes de statements exécutés ensuite
(bulk UPDATE des perdants, INSERT outbox, aucun autre) — même limite
honnête que le reste de cette suite (pas de Postgres réel, voir
`test_auction_bid_row_locking.py`)."""
from __future__ import annotations

import types
import uuid

import pytest
from sqlalchemy.sql.dml import Update as _UpdateStmt

from tests.conftest import run

from ladini.services.database.auction import AuctionMixin
from tests.unit.certified_bids import certified_bid_columns
from ladini.services.database.errors import BusinessRuleException


def _bid(**overrides):
    base = dict(
        id=uuid.uuid4(),
        auction_id=uuid.uuid4(),
        producer_id=uuid.uuid4(),
        offered_price=250.0,
        status="PENDING",
        is_winner=False,
    )
    base.update(overrides)
    if "offered_price_basis" not in base:
        base.update(certified_bid_columns(base["offered_price"]))
    return types.SimpleNamespace(**base)


def _auction(**overrides):
    base = dict(
        id=uuid.uuid4(),
        buyer_id=uuid.uuid4(),
        quantity=10.0,
        unit="TONNE",
        target_zone_id=None,
        sub_category_id=uuid.uuid4(),
        status="OPEN",
        winner_bid_id=None,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _FakeSelectWinningBidSession:
    """1 seule requête `SELECT ... FOR UPDATE` (celle capturée par
    `select_winning_bid`) ; les statements suivants (bulk UPDATE des
    perdants, INSERT outbox) sont distingués par TYPE, jamais exécutés
    réellement.

    `owner_phone` (2026-09-28, ajout pour le garde de propriété acheteur
    fermé dans `select_winning_bid`) : `.scalar()` sert UNIQUEMENT cette
    requête de résolution de propriétaire — doublure minimale, jamais
    exécutée réellement contre une base. `None` par défaut : les tests de
    CE fichier qui n'atteignent jamais ce garde (rejetés plus tôt par les
    contrôles de statut auction/bid qu'ils testent) n'en ont pas besoin."""

    def __init__(self, row, owner_phone=None):
        self._row = row
        self._owner_phone = owner_phone
        self.added: list = []
        self.update_statements: list = []
        self.other_statements: list = []

    async def scalar(self, stmt):
        return self._owner_phone

    async def execute(self, stmt):
        if isinstance(stmt, _UpdateStmt):
            self.update_statements.append(stmt)
            return types.SimpleNamespace(rowcount=0, scalars=lambda: types.SimpleNamespace(all=lambda: []))
        if self._row is None:
            return types.SimpleNamespace(fetchone=lambda: None)
        # 1er appel non-UPDATE == la requête SELECT principale ; tout appel
        # non-UPDATE ultérieur (INSERT outbox) reçoit une réponse neutre.
        if not hasattr(self, "_select_served"):
            self._select_served = True
            row = self._row
            return types.SimpleNamespace(fetchone=lambda: row)
        self.other_statements.append(stmt)
        return types.SimpleNamespace(all=lambda: [])

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


class TestAuctionMustBeOpenNotJustNonClosed:
    def test_expired_auction_rejects_winner_selection(self):
        bid = _bid()
        auction = _auction(status="EXPIRED")
        row = (bid, auction, "Awa", "+22670000001", types.SimpleNamespace(name="Riz"))
        session = _FakeSelectWinningBidSession(row)
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc_info:
            run(svc.select_winning_bid(bid_id=str(bid.id)))
        assert exc_info.value.reason == "auction_not_open"
        assert not session.added, "aucune Order ne doit être créée"

    def test_cancelled_auction_rejects_winner_selection(self):
        bid = _bid()
        auction = _auction(status="CANCELLED")
        row = (bid, auction, "Awa", "+22670000001", types.SimpleNamespace(name="Riz"))
        session = _FakeSelectWinningBidSession(row)
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc_info:
            run(svc.select_winning_bid(bid_id=str(bid.id)))
        assert exc_info.value.reason == "auction_not_open"
        assert not session.added

    def test_closed_auction_keeps_its_own_specific_message(self):
        """Non-régression : le message dédié ("déjà clôturée avec un autre
        partenaire") reste distinct pour le cas CLOSED — pas absorbé dans
        le message générique."""
        bid = _bid()
        auction = _auction(status="CLOSED")
        row = (bid, auction, "Awa", "+22670000001", types.SimpleNamespace(name="Riz"))
        session = _FakeSelectWinningBidSession(row)
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc_info:
            run(svc.select_winning_bid(bid_id=str(bid.id)))
        assert exc_info.value.reason == "auction_already_closed"
        assert "autre partenaire" in str(exc_info.value)


class TestWithdrawnOrLostBidCanNeverWin:
    def test_a_withdrawn_bid_can_never_be_selected_as_winner(self):
        """Le scénario exact de l'audit : l'enchère reste OPEN (d'autres
        offres possibles), mais CE bid a été retiré — jamais sélectionnable."""
        bid = _bid(status="WITHDRAWN")
        auction = _auction(status="OPEN")
        row = (bid, auction, "Awa", "+22670000001", types.SimpleNamespace(name="Riz"))
        session = _FakeSelectWinningBidSession(row)
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc_info:
            run(svc.select_winning_bid(bid_id=str(bid.id)))
        assert exc_info.value.reason == "bid_not_selectable"
        assert bid.is_winner is False
        assert not session.added

    def test_an_already_lost_bid_can_never_be_reselected(self):
        bid = _bid(status="LOST")
        auction = _auction(status="OPEN")
        row = (bid, auction, "Awa", "+22670000001", types.SimpleNamespace(name="Riz"))
        session = _FakeSelectWinningBidSession(row)
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc_info:
            run(svc.select_winning_bid(bid_id=str(bid.id)))
        assert exc_info.value.reason == "bid_not_selectable"


class TestPriceUsedIsAlwaysTheCurrentOne:
    def test_bid_updated_twice_then_selected_uses_the_final_price_everywhere(self):
        """Reproduction directe du scénario du mandat : 250 -> 275 -> 300,
        PUIS sélection — le prix affiché, le bid sélectionné et l'Order
        doivent tous porter 300, jamais une valeur intermédiaire."""
        bid = _bid(offered_price=250.0)
        bid.offered_price = 275.0
        bid.offered_price = 300.0  # dernière valeur RÉELLEMENT en base au moment du lock
        auction = _auction(quantity=10.0, status="OPEN")
        row = (bid, auction, "Awa", "+22670000001", types.SimpleNamespace(name="Riz"))
        session = _FakeSelectWinningBidSession(row, owner_phone="+22670000099")
        svc = _service(session)

        result = run(svc.select_winning_bid(bid_id=str(bid.id), phone="+22670000099"))

        assert result["status"] == "success"
        assert bid.offered_price == 300.0
        assert bid.is_winner is True
        assert bid.status == "WINNING"
        orders = [o for o in session.added if type(o).__name__ == "Order"]
        assert len(orders) == 1
        assert orders[0].total_amount == 300.0 * 10.0
        assert "3000" in result["summary_buyer"]


class TestSelectWinningBidRequiresOwnership:
    """(2026-09-28, audit fiabilité agent) : gap réel confirmé — cette
    fonction clôturait une enchère ET instanciait une VRAIE `Order` sans
    jamais vérifier que l'appelant est bien l'ACHETEUR propriétaire de
    l'enchère, contrairement à son voisin dans ce même fichier,
    `cancel_auction` (voir `TestCancelAuctionLocksTheRow` ci-dessous, et
    surtout `services/database/auction.py::cancel_auction`, qui filtre
    `User.phone == clean_phone`). Un attaquant en possession d'un `bid_id`
    d'une enchère qui ne lui appartient pas (buyer A) pouvait la clôturer
    et créer une `Order` au nom du VRAI propriétaire (buyer B) — jamais à
    son propre profit direct, mais une mutation métier sur l'entité d'un
    autre acheteur, jamais approuvée par lui."""

    def _row(self, **auction_overrides):
        bid = _bid()
        auction_overrides.setdefault("status", "OPEN")
        auction = _auction(**auction_overrides)
        return (bid, auction, "Awa", "+22670000001", types.SimpleNamespace(name="Riz")), bid, auction

    def test_a_caller_without_a_phone_is_rejected(self):
        row, bid, _auction = self._row()
        session = _FakeSelectWinningBidSession(row, owner_phone="+22670000099")
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc_info:
            run(svc.select_winning_bid(bid_id=str(bid.id)))
        assert exc_info.value.reason == "not_owner"
        assert not session.added, "aucune Order ne doit être créée sans identité vérifiée"
        assert bid.is_winner is False

    def test_a_caller_whose_phone_does_not_match_the_auction_owner_is_rejected(self):
        """Le scénario exact de l'audit : buyer A (appelant) tente de
        clôturer une enchère qui appartient à buyer B."""
        row, bid, _auction = self._row()
        session = _FakeSelectWinningBidSession(row, owner_phone="+22670000099")  # buyer B
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc_info:
            run(svc.select_winning_bid(bid_id=str(bid.id), phone="+22670011111"))  # buyer A
        assert exc_info.value.reason == "not_owner"
        assert not session.added
        assert bid.is_winner is False
        assert bid.status == "PENDING", "le bid ne doit jamais passer WINNING sur un appelant non autorisé"

    def test_the_real_owner_can_still_select_their_own_winning_bid(self):
        """Non-régression : le correctif ne doit jamais bloquer le VRAI
        propriétaire — même numéro normalisé des deux côtés."""
        row, bid, _auction = self._row(quantity=10.0)
        session = _FakeSelectWinningBidSession(row, owner_phone="+22670000099")
        svc = _service(session)

        result = run(svc.select_winning_bid(bid_id=str(bid.id), phone="+226 70 00 00 99"))
        assert result["status"] == "success"
        assert bid.is_winner is True

    def test_status_guards_still_fire_before_the_ownership_check(self):
        """Ordre des gardes inchangé pour les tests déjà existants de ce
        fichier : un appelant SANS identité qui vise une enchère déjà hors
        cycle de vie voit toujours le message dédié à CE gap, jamais
        masqué par le nouveau garde de propriété."""
        row, bid, _auction = self._row(status="CLOSED")
        session = _FakeSelectWinningBidSession(row)
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc_info:
            run(svc.select_winning_bid(bid_id=str(bid.id)))
        assert exc_info.value.reason == "auction_already_closed"


class TestCancelAuctionLocksTheRow:
    def test_cancel_auction_select_is_for_update(self):
        """`cancel_auction` n'avait AUCUN verrou — une annulation concurrente
        à une sélection de gagnant pouvait écraser silencieusement un
        `Order` déjà créé avec `Auction.status="CANCELLED"`."""
        from sqlalchemy.dialects import postgresql

        class _CapturingSession:
            def __init__(self):
                self.captured = []

            async def scalar(self, stmt):
                self.captured.append(stmt)
                return None  # auction introuvable -> lève après la requête verrouillée

        session = _CapturingSession()
        svc = _service(session)

        with pytest.raises(Exception):
            run(svc.cancel_auction(str(uuid.uuid4()), "+22670000001"))

        assert session.captured
        sql = str(
            session.captured[0].compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}
            )
        )
        assert "FOR UPDATE" in sql.upper()
