"""`services/database/base_service.py::transactional` + `errors.py` — le SEUL
cerveau transactionnel du backend (commit/rollback piloté à 100% par le flux
d'exécution Python) et la barrière anti-fuite d'informations techniques vers
l'agent. Priorité maximale : une régression ici touche TOUTE écriture DB.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import run


# =====================================================================
# errors.py — barrière anti-fuite (pure, testée en isolation d'abord)
# =====================================================================

class TestIsSafeBusinessException:
    def test_business_rule_exception_is_safe(self):
        from agriconnect.services.database.errors import BusinessRuleException, is_safe_business_exception
        assert is_safe_business_exception(BusinessRuleException("Stock insuffisant")) is True

    def test_value_error_is_safe_when_message_is_clean(self):
        from agriconnect.services.database.errors import is_safe_business_exception
        assert is_safe_business_exception(ValueError("Quantité négative")) is True

    def test_key_error_is_safe_when_message_is_clean(self):
        from agriconnect.services.database.errors import is_safe_business_exception
        assert is_safe_business_exception(KeyError("producer_id")) is True

    def test_value_error_leaking_technical_content_is_disqualified(self):
        from agriconnect.services.database.errors import is_safe_business_exception
        exc = ValueError('duplicate key value violates unique constraint "bids_pkey"')
        assert is_safe_business_exception(exc) is False

    def test_generic_technical_exception_is_not_safe(self):
        from agriconnect.services.database.errors import is_safe_business_exception
        assert is_safe_business_exception(RuntimeError("connection refused")) is False

    def test_safe_database_error_is_always_safe(self):
        from agriconnect.services.database.errors import SafeDatabaseError, is_safe_business_exception
        assert is_safe_business_exception(SafeDatabaseError("generic")) is True


class TestSanitizeErrorMessage:
    def test_business_message_passes_through_unchanged(self):
        from agriconnect.services.database.errors import sanitize_error_message
        assert sanitize_error_message(ValueError("Prix nul interdit")) == "Prix nul interdit"

    def test_safe_database_error_returns_its_own_safe_message(self):
        from agriconnect.services.database.errors import SafeDatabaseError, sanitize_error_message
        exc = SafeDatabaseError("custom safe msg")
        assert sanitize_error_message(exc) == "custom safe msg"

    def test_technical_exception_is_replaced_by_the_generic_message(self):
        from agriconnect.services.database.errors import sanitize_error_message, _GENERIC_SAFE_MESSAGE
        exc = RuntimeError("psycopg2.errors.UniqueViolation: duplicate key")
        assert sanitize_error_message(exc) == _GENERIC_SAFE_MESSAGE

    def test_business_exception_with_leaked_technical_content_falls_back_to_generic(self):
        from agriconnect.services.database.errors import sanitize_error_message, _GENERIC_SAFE_MESSAGE
        exc = ValueError('relation "orders" does not exist')
        assert sanitize_error_message(exc) == _GENERIC_SAFE_MESSAGE

    def test_empty_business_message_falls_back_to_generic(self):
        from agriconnect.services.database.errors import sanitize_error_message, _GENERIC_SAFE_MESSAGE
        assert sanitize_error_message(ValueError("")) == _GENERIC_SAFE_MESSAGE


class TestSafeErrorDict:
    def test_builds_a_standard_error_payload(self):
        from agriconnect.services.database.errors import safe_error_dict
        result = safe_error_dict(ValueError("Stock insuffisant"), context="add_stock", reason="insufficient")
        assert result == {"status": "error", "message": "Stock insuffisant", "reason": "insufficient"}


class TestScrubErrorResult:
    def test_non_dict_passes_through_unchanged(self):
        from agriconnect.services.database.errors import scrub_error_result
        assert scrub_error_result("not a dict") == "not a dict"

    def test_clean_message_is_left_untouched(self):
        from agriconnect.services.database.errors import scrub_error_result
        result = {"status": "error", "message": "Stock insuffisant"}
        assert scrub_error_result(result) == result

    def test_technical_message_is_neutralized(self):
        from agriconnect.services.database.errors import scrub_error_result, _GENERIC_SAFE_MESSAGE
        result = {"status": "error", "message": 'duplicate key value violates unique constraint "x"'}
        scrubbed = scrub_error_result(result)
        assert scrubbed["message"] == _GENERIC_SAFE_MESSAGE
        assert result["message"] != _GENERIC_SAFE_MESSAGE, "l'original ne doit pas être muté"

    def test_missing_message_key_is_a_noop(self):
        from agriconnect.services.database.errors import scrub_error_result
        assert scrub_error_result({"status": "ok"}) == {"status": "ok"}


# =====================================================================
# base_service.py — @transactional
# =====================================================================

class _FakeSession:
    def __init__(self, *, commit_exc=None, rollback_exc=None):
        self.committed = False
        self.rolled_back = False
        self._commit_exc = commit_exc
        self._rollback_exc = rollback_exc

    async def commit(self):
        if self._commit_exc:
            raise self._commit_exc
        self.committed = True

    async def rollback(self):
        if self._rollback_exc:
            raise self._rollback_exc
        self.rolled_back = True


class _FakeSessionCM:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _make_session_factory(sessions):
    """Renvoie un `session_factory` (callable) qui produit une nouvelle CM à
    chaque appel, dépilant `sessions` dans l'ordre (un par tentative)."""
    sessions_iter = iter(sessions)

    def _factory():
        return _FakeSessionCM(next(sessions_iter))

    return _factory


def _patch_sessionmaker(monkeypatch, module, factory):
    monkeypatch.setattr(module, "get_sessionmaker", lambda: factory)


class TestTransactionalRootCall:
    def test_write_true_commits_on_success(self, monkeypatch):
        import agriconnect.services.database.base_service as base_mod

        session = _FakeSession()
        _patch_sessionmaker(monkeypatch, base_mod, _make_session_factory([session]))

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def do_thing(self):
                return {"ok": True}

        result = run(Svc().do_thing())
        assert result == {"ok": True}
        assert session.committed is True
        assert base_mod.db_session_ctx.get() is None, "le ContextVar doit être remis à None après le tour"

    def test_write_false_never_commits(self, monkeypatch):
        import agriconnect.services.database.base_service as base_mod

        session = _FakeSession()
        _patch_sessionmaker(monkeypatch, base_mod, _make_session_factory([session]))

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=False)
            async def read_thing(self):
                return {"data": []}

        run(Svc().read_thing())
        assert session.committed is False

    def test_missing_sessionmaker_raises_immediately(self, monkeypatch):
        import agriconnect.services.database.base_service as base_mod

        monkeypatch.setattr(base_mod, "get_sessionmaker", lambda: None)

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def do_thing(self):
                return {}

        with pytest.raises(RuntimeError, match="sessionmaker unavailable"):
            run(Svc().do_thing())

    def test_dict_result_is_scrubbed_of_technical_leakage(self, monkeypatch):
        import agriconnect.services.database.base_service as base_mod

        session = _FakeSession()
        _patch_sessionmaker(monkeypatch, base_mod, _make_session_factory([session]))

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def do_thing(self):
                return {"status": "error", "message": 'duplicate key value violates unique constraint "x"'}

        result = run(Svc().do_thing())
        assert "duplicate key" not in result["message"]

    def test_non_dict_result_passes_through_untouched(self, monkeypatch):
        import agriconnect.services.database.base_service as base_mod

        session = _FakeSession()
        _patch_sessionmaker(monkeypatch, base_mod, _make_session_factory([session]))

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def do_thing(self):
                return ["a", "list", "result"]

        assert run(Svc().do_thing()) == ["a", "list", "result"]

    def test_session_is_passed_positionally_when_the_function_declares_it(self, monkeypatch):
        import agriconnect.services.database.base_service as base_mod

        session = _FakeSession()
        _patch_sessionmaker(monkeypatch, base_mod, _make_session_factory([session]))
        seen = {}

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def do_thing(self, session):
                seen["session"] = session
                return {}

        run(Svc().do_thing())
        assert seen["session"] is session

    def test_function_without_session_param_reads_it_from_the_context_var(self, monkeypatch):
        import agriconnect.services.database.base_service as base_mod

        session = _FakeSession()
        _patch_sessionmaker(monkeypatch, base_mod, _make_session_factory([session]))
        seen = {}

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def do_thing(self):
                seen["session"] = self.session
                return {}

        run(Svc().do_thing())
        assert seen["session"] is session


class TestTransactionalNestedCall:
    def test_nested_call_reuses_the_root_session_without_committing(self, monkeypatch):
        """L'appel imbriqué NE DOIT PAS committer — c'est la racine qui
        décide de l'issue transactionnelle."""
        import agriconnect.services.database.base_service as base_mod

        session = _FakeSession()

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def inner(self, session):
                return {"inner": True}

            @base_mod.transactional(write=True)
            async def outer(self):
                return await self.inner()

        _patch_sessionmaker(monkeypatch, base_mod, _make_session_factory([session]))
        result = run(Svc().outer())
        assert result == {"inner": True}
        assert session.committed is True, "le commit vient bien de la racine (outer), pas de inner"

    def test_nested_call_without_session_param_still_shares_the_context(self, monkeypatch):
        import agriconnect.services.database.base_service as base_mod

        session = _FakeSession()
        seen = {}

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def inner(self):
                seen["session"] = self.session
                return {}

            @base_mod.transactional(write=True)
            async def outer(self):
                return await self.inner()

        _patch_sessionmaker(monkeypatch, base_mod, _make_session_factory([session]))
        run(Svc().outer())
        assert seen["session"] is session


class TestTransactionalExceptionHandling:
    def test_business_rule_exception_rolls_back_and_reraises_intact(self, monkeypatch):
        import agriconnect.services.database.base_service as base_mod
        from agriconnect.services.database.errors import BusinessRuleException

        session = _FakeSession()
        _patch_sessionmaker(monkeypatch, base_mod, _make_session_factory([session]))

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def do_thing(self):
                raise BusinessRuleException("Stock insuffisant", reason="insufficient_stock")

        with pytest.raises(BusinessRuleException, match="Stock insuffisant"):
            run(Svc().do_thing())
        assert session.rolled_back is True
        assert session.committed is False

    def test_value_error_rolls_back_and_reraises_intact(self, monkeypatch):
        import agriconnect.services.database.base_service as base_mod

        session = _FakeSession()
        _patch_sessionmaker(monkeypatch, base_mod, _make_session_factory([session]))

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def do_thing(self):
                raise ValueError("Prix nul interdit")

        with pytest.raises(ValueError, match="Prix nul interdit"):
            run(Svc().do_thing())
        assert session.rolled_back is True

    def test_technical_exception_is_wrapped_in_safe_database_error(self, monkeypatch):
        import agriconnect.services.database.base_service as base_mod
        from agriconnect.services.database.errors import SafeDatabaseError

        session = _FakeSession()
        _patch_sessionmaker(monkeypatch, base_mod, _make_session_factory([session]))

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def do_thing(self):
                raise RuntimeError('psycopg2: relation "orders" does not exist')

        with pytest.raises(SafeDatabaseError) as excinfo:
            run(Svc().do_thing())
        assert "orders" not in str(excinfo.value), "le détail technique ne doit JAMAIS fuiter"
        assert session.rolled_back is True

    def test_technical_exception_chains_the_original_via_from(self, monkeypatch):
        import agriconnect.services.database.base_service as base_mod
        from agriconnect.services.database.errors import SafeDatabaseError

        session = _FakeSession()
        _patch_sessionmaker(monkeypatch, base_mod, _make_session_factory([session]))
        original = RuntimeError("boom")

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def do_thing(self):
                raise original

        with pytest.raises(SafeDatabaseError) as excinfo:
            run(Svc().do_thing())
        assert excinfo.value.__cause__ is original

    def test_cancelled_error_rolls_back_and_reraises_uncoerced(self, monkeypatch):
        import asyncio
        import agriconnect.services.database.base_service as base_mod

        session = _FakeSession()
        _patch_sessionmaker(monkeypatch, base_mod, _make_session_factory([session]))

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def do_thing(self):
                raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            run(Svc().do_thing())
        assert session.rolled_back is True

    def test_rollback_failure_is_swallowed_and_original_exception_still_raised(self, monkeypatch):
        import agriconnect.services.database.base_service as base_mod

        session = _FakeSession(rollback_exc=RuntimeError("rollback also failed"))
        _patch_sessionmaker(monkeypatch, base_mod, _make_session_factory([session]))

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def do_thing(self):
                raise ValueError("original business error")

        with pytest.raises(ValueError, match="original business error"):
            run(Svc().do_thing())

    def test_context_var_is_reset_even_after_an_exception(self, monkeypatch):
        import agriconnect.services.database.base_service as base_mod

        session = _FakeSession()
        _patch_sessionmaker(monkeypatch, base_mod, _make_session_factory([session]))

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def do_thing(self):
                raise ValueError("boom")

        with pytest.raises(ValueError):
            run(Svc().do_thing())
        assert base_mod.db_session_ctx.get() is None


class TestTransactionalConnectionLostRetry:
    def test_connection_lost_on_first_attempt_retries_and_succeeds(self, monkeypatch):
        import agriconnect.services.database.base_service as base_mod

        failing_session = _FakeSession()
        recovering_session = _FakeSession()
        sessions = [failing_session, recovering_session]
        factory = _make_session_factory(sessions)
        _patch_sessionmaker(monkeypatch, base_mod, factory)
        monkeypatch.setattr(base_mod, "close_db", AsyncMock())

        call_count = {"n": 0}

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def do_thing(self):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise RuntimeError("connection was closed unexpectedly")
                return {"ok": True}

        result = run(Svc().do_thing())
        assert result == {"ok": True}
        assert failing_session.rolled_back is True
        assert recovering_session.committed is True
        base_mod.close_db.assert_awaited_once()

    def test_connection_lost_on_both_attempts_surfaces_as_safe_database_error(self, monkeypatch):
        """Le retry est UNIQUE : un 2e échec de connexion n'entraîne pas un
        2e retry, il tombe dans le traitement d'erreur normal."""
        import agriconnect.services.database.base_service as base_mod
        from agriconnect.services.database.errors import SafeDatabaseError

        s1, s2 = _FakeSession(), _FakeSession()
        _patch_sessionmaker(monkeypatch, base_mod, _make_session_factory([s1, s2]))
        monkeypatch.setattr(base_mod, "close_db", AsyncMock())

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def do_thing(self):
                raise RuntimeError("connection does not exist")

        with pytest.raises(SafeDatabaseError):
            run(Svc().do_thing())
        assert s1.rolled_back is True
        assert s2.rolled_back is True

    def test_close_db_failure_during_recovery_does_not_prevent_the_retry(self, monkeypatch):
        import agriconnect.services.database.base_service as base_mod

        failing_session = _FakeSession()
        recovering_session = _FakeSession()
        _patch_sessionmaker(monkeypatch, base_mod, _make_session_factory([failing_session, recovering_session]))
        monkeypatch.setattr(base_mod, "close_db", AsyncMock(side_effect=RuntimeError("close_db exploded")))

        call_count = {"n": 0}

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def do_thing(self):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise RuntimeError("Connection was closed")
                return {"ok": True}

        assert run(Svc().do_thing()) == {"ok": True}

    def test_sessionmaker_unavailable_after_reinit_raises_chained(self, monkeypatch):
        import agriconnect.services.database.base_service as base_mod

        session = _FakeSession()
        calls = {"n": 0}

        def _factory_then_none():
            calls["n"] += 1
            if calls["n"] == 1:
                return lambda: _FakeSessionCM(session)
            return None

        monkeypatch.setattr(base_mod, "get_sessionmaker", _factory_then_none)
        monkeypatch.setattr(base_mod, "close_db", AsyncMock())

        class Svc(base_mod.BaseService):
            @base_mod.transactional(write=True)
            async def do_thing(self):
                raise RuntimeError("connection was closed")

        with pytest.raises(RuntimeError, match="Sessionmaker indisponible après reinit"):
            run(Svc().do_thing())


class TestIsConnectionLost:
    def test_detects_connection_was_closed_case_insensitively(self):
        from agriconnect.services.database.base_service import _is_connection_lost
        assert _is_connection_lost(RuntimeError("Connection Was Closed")) is True

    def test_detects_connection_does_not_exist(self):
        from agriconnect.services.database.base_service import _is_connection_lost
        assert _is_connection_lost(RuntimeError("connection does not exist")) is True

    def test_unrelated_exception_is_not_a_connection_loss(self):
        from agriconnect.services.database.base_service import _is_connection_lost
        assert _is_connection_lost(ValueError("Prix nul interdit")) is False

    def test_asyncpg_connection_does_not_exist_error_is_detected_by_type(self):
        from agriconnect.services.database.base_service import _is_connection_lost
        import asyncpg
        exc = asyncpg.exceptions.ConnectionDoesNotExistError("gone")
        assert _is_connection_lost(exc) is True


class TestSafeRollback:
    def test_rollback_success(self):
        from agriconnect.services.database.base_service import _safe_rollback
        session = _FakeSession()
        run(_safe_rollback(session))
        assert session.rolled_back is True

    def test_rollback_failure_is_swallowed(self):
        from agriconnect.services.database.base_service import _safe_rollback
        session = _FakeSession(rollback_exc=RuntimeError("already closed"))
        run(_safe_rollback(session))  # ne doit pas lever


class TestBaseServiceSessionProperty:
    def test_reads_from_the_context_var(self, monkeypatch):
        import agriconnect.services.database.base_service as base_mod
        svc = base_mod.BaseService()
        assert svc.session is None
        token = base_mod.db_session_ctx.set("fake-session-object")
        try:
            assert svc.session == "fake-session-object"
        finally:
            base_mod.db_session_ctx.reset(token)
