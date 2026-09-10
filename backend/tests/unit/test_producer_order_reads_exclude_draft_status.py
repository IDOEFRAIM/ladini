"""`get_producer_orders` — exclusion par défaut des `Order(status IN
("DRAFT", "SUPERSEDED"))` (2026-09-04, audit d'impact Order(DRAFT), volet
producteur — suite directe de `test_buyer_order_reads_exclude_draft_status.py`).

## Le gap réel fermé par ce fichier

`SALES_LIST_ORDERS` (`interpreter/intent.py`) a `"required": []` — un
producteur demandant "mes commandes" sans préciser de statut atteint
`get_producer_orders(status=None)`. AVANT ce correctif, `status_filter`
vide ne posait AUCUN filtre (ni sur la sous-requête `candidate_ids`, ni sur
le `select(Order)` final) : un `Order(status="DRAFT")` — le checkout
acheteur pas encore confirmé, voir `services/database/buyer.py::create_preorder_draft`
— ou `"SUPERSEDED"` (ancien brouillon remplacé par un cycle "ajouter
d'autres produits") apparaissait comme une VRAIE commande dans la liste du
producteur, avant même que l'acheteur n'ait confirmé quoi que ce soit.

Même pattern que côté acheteur : l'exclusion ne s'applique QUE quand aucun
statut n'est explicitement demandé — un `status="DRAFT"` explicite
(support/debug) continue de fonctionner sans double-exclusion silencieuse.

## Portée honnête

Aucune infrastructure Postgres de test dans ce dépôt — ce fichier prouve la
FORME de la requête réellement compilée (dialecte postgresql,
`literal_binds=True`), même technique que
`test_buyer_order_reads_exclude_draft_status.py`/
`test_auction_bid_row_locking.py`."""
from __future__ import annotations

import types
import uuid

import pytest

from tests.conftest import run

from ladini.services.database.producer import ProducerMgmtMixin


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
    """Capture le `stmt` passé à `.execute()` — ne simule AUCUNE sémantique
    de filtrage réelle, seulement la FORME de la requête envoyée (voir
    portée en tête de fichier)."""

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


class TestNoStatusRequestedExcludesDraftAndSuperseded:
    def test_status_none_adds_the_exclusion_clause(self, monkeypatch):
        """Cas A — `status_filter=None` (le cas réel de "mes commandes" sans
        précision, `SALES_LIST_ORDERS` n'exige aucun champ)."""
        session = _CapturingSession()
        svc = _service(session)

        result = run(svc.get_producer_orders(phone="+22670000001", status=None))

        assert result["status"] == "success"
        assert session.captured_statements
        sql = _compiled_sql_literal(session.captured_statements[0]).upper()
        assert "NOT IN" in sql
        assert "DRAFT" in sql
        assert "SUPERSEDED" in sql

    def test_status_empty_string_is_treated_the_same_as_absent(self, monkeypatch):
        """Cas B — `status_filter=""` doit être traité comme "non fourni"
        (même normalisation `(status or "").strip().upper()` que le code)."""
        session = _CapturingSession()
        svc = _service(session)

        result = run(svc.get_producer_orders(phone="+22670000001", status=""))

        assert result["status"] == "success"
        sql = _compiled_sql_literal(session.captured_statements[0]).upper()
        assert "NOT IN" in sql
        assert "DRAFT" in sql
        assert "SUPERSEDED" in sql


class TestExplicitStatusRequestIsNeverSilentlyOverridden:
    def test_an_explicit_committed_status_keeps_its_own_filter_untouched(self, monkeypatch):
        """Cas C — un statut engagé explicite continue de fonctionner
        exactement comme avant (aucune exclusion implicite ajoutée EN PLUS)."""
        session = _CapturingSession()
        svc = _service(session)

        result = run(svc.get_producer_orders(phone="+22670000001", status="COMPLETED"))

        assert result["status"] == "success"
        sql = _compiled_sql_literal(session.captured_statements[0]).upper()
        assert "= 'COMPLETED'" in sql
        assert "NOT IN" not in sql

    def test_an_explicit_draft_request_is_honored_not_silently_excluded(self, monkeypatch):
        """Cas D — un appel support/debug demandant EXPLICITEMENT `DRAFT`
        ne doit jamais se voir opposer l'exclusion implicite : le système
        ne doit pas "corriger" une demande explicite."""
        session = _CapturingSession()
        svc = _service(session)

        result = run(svc.get_producer_orders(phone="+22670000001", status="DRAFT"))

        assert result["status"] == "success"
        sql = _compiled_sql_literal(session.captured_statements[0]).upper()
        assert "= 'DRAFT'" in sql
        assert "NOT IN" not in sql


class TestBusinessScenarioMesCommandes:
    def test_producer_asking_my_orders_without_a_status_sees_only_committed_orders(
        self, monkeypatch
    ):
        """Scénario métier : producteur → "mes commandes" → status_filter
        absent → seules les commandes RÉELLEMENT engagées doivent pouvoir
        remonter (vérifié au niveau construction de requête, convention du
        dépôt — aucune base de test réelle ici)."""
        session = _CapturingSession()
        svc = _service(session)

        result = run(svc.get_producer_orders(phone="+22670000001"))

        assert result["status"] == "success"
        sql = _compiled_sql_literal(session.captured_statements[0]).upper()
        # La requête exclut structurellement DRAFT/SUPERSEDED — un
        # `Order(status="CONFIRMED")` du même producteur, lui, ne serait
        # filtré par AUCUNE clause de cette requête (comportement inchangé
        # pour toute commande réellement engagée).
        assert "NOT IN" in sql
        assert "DRAFT" in sql and "SUPERSEDED" in sql
