"""`get_buyer_orders_dashboard` / `get_transaction_summary` — exclusion
explicite des `Order(status IN ("DRAFT", "SUPERSEDED"))` (2026-09-04, audit
d'impact Order(DRAFT) orphelin, filet de sécurité côté LECTURE).

## Le gap réel fermé par ce fichier

AVANT ce correctif, les DEUX requêtes ne filtraient AUCUN statut :
- `get_buyer_orders_dashboard` affichait un brouillon de checkout
  abandonné/remplacé comme une VRAIE commande dans le tableau de bord
  WhatsApp de l'acheteur (`status_map` n'a aucune entrée DRAFT/SUPERSEDED —
  repli sur `f"🔄 Status: {order.status}"`, littéralement "🔄 Status: DRAFT").
- `get_transaction_summary` (branche sans `order_id` explicite, celle
  utilisée par `order_tracking.py::check_order_status` quand l'acheteur
  demande "où en est ma commande ?" sans préciser laquelle) prenait
  littéralement `ORDER BY created_at DESC LIMIT 1` SANS filtre de statut —
  un brouillon abandonné/remplacé, étant la ligne la plus RÉCENTE après un
  CANCEL/ADD_MORE, masquait la VRAIE dernière commande de l'acheteur.

Ce fichier fait partie d'une défense en DEUX couches (voir
`tests/architecture/test_preorder_draft_order_lifecycle_sync.py` pour le
côté ÉCRITURE — la synchronisation `Order`↔`PreorderDraft` au CANCEL/
ADD_MORE) : même si la synchronisation écriture échoue en mode dégradé
(best-effort documenté), CETTE exclusion garantit qu'un `Order` resté
DRAFT/SUPERSEDED ne remonte jamais comme une commande réelle.

## Portée honnête

Aucune infrastructure Postgres de test dans ce dépôt — ce fichier prouve la
FORME de la requête réellement compilée (dialecte postgresql,
`literal_binds=True`) contient bien l'exclusion, même technique que
`tests/unit/test_auction_bid_row_locking.py`."""
from __future__ import annotations

import types
import uuid

import pytest

from tests.conftest import run

from ladini.services.database.buyer import BuyerMixin


def _fake_buyer_profile():
    user = types.SimpleNamespace(id=uuid.uuid4(), name="Acheteur Test", zone_id=None)
    profile = types.SimpleNamespace(id=uuid.uuid4(), establishment_name=None)
    return user, profile


async def _async_return(value):
    return value


def _compiled_sql_literal(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


class _EmptyResult:
    def unique(self):
        return self

    def scalars(self):
        return self

    def all(self):
        return []


class _CapturingSession:
    """Capture le `stmt` passé à `.execute()`/`.scalar()` — ne simule AUCUNE
    sémantique de filtrage réelle (aucun moteur SQL), seulement la FORME de
    la requête envoyée (voir portée en tête de fichier)."""

    def __init__(self) -> None:
        self.captured_statements: list = []

    async def execute(self, stmt):
        self.captured_statements.append(stmt)
        return _EmptyResult()

    async def scalar(self, stmt):
        self.captured_statements.append(stmt)
        return None


def _service(session):
    class _Svc(BuyerMixin):
        @property
        def session(self):
            return session

    svc = _Svc()
    svc.get_buyer_profile = lambda phone: _async_return(_fake_buyer_profile())
    return svc


class TestBuyerOrdersDashboardExcludesDraftAndSuperseded:
    def test_the_query_excludes_draft_and_superseded_orders(self, monkeypatch):
        session = _CapturingSession()
        svc = _service(session)

        result = run(svc.get_buyer_orders_dashboard(phone="+22670000001"))

        assert result["status"] == "success"
        assert session.captured_statements
        sql = _compiled_sql_literal(session.captured_statements[0]).upper()
        assert "NOT IN" in sql
        assert "DRAFT" in sql
        assert "SUPERSEDED" in sql


class TestTransactionSummaryLastOrderExcludesDraftAndSuperseded:
    def test_the_most_recent_order_lookup_by_phone_excludes_draft_and_superseded(
        self, monkeypatch
    ):
        """Branche SANS `order_id` explicite — celle utilisée par
        `order_tracking.py::check_order_status` pour "où en est ma
        commande ?" sans préciser laquelle."""
        session = _CapturingSession()
        svc = _service(session)

        result = run(svc.get_transaction_summary(buyer_phone="+22670000001"))

        assert result["status"] == "success"
        assert session.captured_statements
        sql = _compiled_sql_literal(session.captured_statements[0]).upper()
        assert "NOT IN" in sql
        assert "DRAFT" in sql
        assert "SUPERSEDED" in sql

    def test_an_explicit_order_id_lookup_is_never_filtered_by_status(self, monkeypatch):
        """Une consultation par `order_id` EXPLICITE (support/debug) reste
        consultable quel que soit son statut — portée volontairement
        limitée à la branche "dernière transaction" ambiguë ci-dessus."""
        session = _CapturingSession()
        svc = _service(session)
        oid = str(uuid.uuid4())

        result = run(svc.get_transaction_summary(order_id=oid))

        assert result["status"] == "success"
        sql = _compiled_sql_literal(session.captured_statements[0]).upper()
        assert "NOT IN" not in sql
