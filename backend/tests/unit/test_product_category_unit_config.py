"""`BaseMixin.get_product_category_unit_config` (2026-09-19, retour produit).

Design demandé : au lieu de deviner l'unité depuis le texte libre, l'agent
doit se référer à une config ADMIN par sous-catégorie — un ENSEMBLE
d'unités autorisées + une unité PRIORITAIRE pour standardiser. Cette config
(`priority_unit`/`allowed_units`) N'EXISTE PAS ENCORE dans la vraie table
`governance.sub_categories` (gérée côté site par Drizzle, hors de ce
dépôt) : ce fichier verrouille que la méthode se dégrade PROPREMENT
(status "error", jamais une exception) tant que ces colonnes n'existent
pas, et qu'elle consomme correctement la config dès qu'elles apparaissent.

Style « mixin instancié directement avec une session stubée » — voir
`tests/unit/test_onboarding_zone_region_level.py` pour le même pattern.
"""
from __future__ import annotations

import uuid

from sqlalchemy.exc import ProgrammingError

from tests.conftest import run


class _NestedTxn:
    """Simule `AsyncSession.begin_nested()` : propage toute exception levée
    à l'intérieur du `async with` (comme un vrai SAVEPOINT après rollback),
    ne l'avale jamais elle-même — c'est au code appelant de la rattraper."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Mappings:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _RawResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return _Mappings(self._row)


class _FakeSession:
    def __init__(self, *, sub_category_id=None, raw_row=None, missing_columns=False):
        self.sub_category_id = sub_category_id
        self.raw_row = raw_row
        self.missing_columns = missing_columns
        self.raw_sql_calls = 0

    async def scalar(self, stmt):
        return self.sub_category_id

    def begin_nested(self):
        return _NestedTxn()

    async def execute(self, stmt, params=None):
        self.raw_sql_calls += 1
        if self.missing_columns:
            raise ProgrammingError(
                "SELECT priority_unit, allowed_units FROM governance.sub_categories WHERE id = :id",
                params,
                Exception('column "priority_unit" does not exist'),
            )
        return _RawResult(self.raw_row)


def _mixin_with(session):
    from ladini.services.database.base import BaseMixin

    class _Svc(BaseMixin):
        @property
        def session(self):
            return session

    return _Svc()


class TestGracefulDegradationWhenColumnsDoNotExistYet:
    """État RÉEL de la base aujourd'hui : `priority_unit`/`allowed_units`
    n'existent pas encore dans `governance.sub_categories`."""

    def test_missing_columns_returns_error_status_not_a_crash(self):
        session = _FakeSession(sub_category_id=uuid.uuid4(), missing_columns=True)
        svc = _mixin_with(session)
        result = run(svc.get_product_category_unit_config("lait"))
        assert result["status"] == "error"
        assert session.raw_sql_calls == 1

    def test_missing_columns_does_not_poison_the_session_transaction(self):
        """La lecture défensive doit passer par un SAVEPOINT dédié
        (`begin_nested`) : une colonne absente ne doit jamais laisser la
        session dans un état où un appel SUIVANT échouerait aussi."""
        session = _FakeSession(sub_category_id=uuid.uuid4(), missing_columns=True)
        svc = _mixin_with(session)
        first = run(svc.get_product_category_unit_config("lait"))
        second = run(svc.get_product_category_unit_config("boeufs"))
        assert first["status"] == "error"
        assert second["status"] == "error"
        assert session.raw_sql_calls == 2


class TestSubCategoryResolution:
    def test_unresolvable_product_name_returns_error_without_a_raw_sql_call(self):
        session = _FakeSession(sub_category_id=None)
        svc = _mixin_with(session)
        result = run(svc.get_product_category_unit_config("produit-totalement-inconnu"))
        assert result["status"] == "error"
        assert session.raw_sql_calls == 0

    def test_empty_product_name_is_rejected_immediately(self):
        session = _FakeSession(sub_category_id=uuid.uuid4())
        svc = _mixin_with(session)
        result = run(svc.get_product_category_unit_config(""))
        assert result["status"] == "error"
        assert session.raw_sql_calls == 0


class TestConfigIsConsumedOnceColumnsExist:
    """Comportement futur : dès que le site ajoute les colonnes, cette
    méthode doit les exposer telles quelles, sans changement de code."""

    def test_priority_and_allowed_units_are_returned(self):
        session = _FakeSession(
            sub_category_id=uuid.uuid4(),
            raw_row={"priority_unit": "LITRE", "allowed_units": ["LITRE"]},
        )
        svc = _mixin_with(session)
        result = run(svc.get_product_category_unit_config("lait"))
        assert result["status"] == "success"
        assert result["data"]["priority_unit"] == "LITRE"
        assert result["data"]["allowed_units"] == ["LITRE"]

    def test_a_subcategory_with_no_config_row_yet_degrades_to_error(self):
        """Colonnes présentes mais NULL (sous-catégorie pas encore
        configurée par l'admin) : pas de config exploitable, jamais un crash."""
        session = _FakeSession(
            sub_category_id=uuid.uuid4(),
            raw_row={"priority_unit": None, "allowed_units": None},
        )
        svc = _mixin_with(session)
        result = run(svc.get_product_category_unit_config("mais"))
        assert result["status"] == "error"
