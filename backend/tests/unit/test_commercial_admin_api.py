"""Espace COMMERCIAL — validation des paramètres, autorisation, et surtout :
preuve qu'une relance manuelle ne touche JAMAIS l'état LangGraph d'un
utilisateur (current_goal / pending_field / stable_entities / drafts).

Comportement SQL réel (agrégation conversations, jointures) couvert par
tests/schema/test_commercial_admin_api_pg.py (CI, base réelle) — même
convention que tests/unit/test_analytics_admin_api.py.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

# Force la configuration COMPLÈTE du registre de mappers SQLAlchemy avant
# d'instancier un modèle ORM (AuditLog/CommercialFollowup) isolément : sans
# ça, un import partiel (seulement identity+intelligence) fait échouer la
# résolution paresseuse des `relationship("Farm", ...)` d'un tout autre
# domaine (catalog) — un artefact d'isolation de test, pas un vrai problème
# en production (main.py importe déjà tout le graphe de modèles).
import ladini.domain.models  # noqa: E402,F401


class TestListParams:
    def _p(self, **q):
        from ladini.services.commercial.admin_api import parse_list_params

        return parse_list_params(q)

    def test_defaults(self):
        p = self._p()
        assert (p.status_filter, p.sort, p.limit, p.offset) == ("all", "recent", 50, 0)

    def test_valid_filters_and_sorts(self):
        for f in ("to_follow_up", "followed_up", "resolved", "long", "no_response", "all"):
            assert self._p(filter=f).status_filter == f
        for s in ("recent", "oldest_no_response", "longest"):
            assert self._p(sort=s).sort == s

    def test_invalid_filter_or_sort_is_400(self):
        from ladini.services.commercial.admin_api import ApiError

        with pytest.raises(ApiError) as e:
            self._p(filter="bogus")
        assert e.value.status == 400
        with pytest.raises(ApiError):
            self._p(sort="bogus")

    def test_limit_bounds(self):
        from ladini.services.commercial.admin_api import ApiError

        assert self._p(limit=200).limit == 200
        with pytest.raises(ApiError):
            self._p(limit=0)
        with pytest.raises(ApiError):
            self._p(limit=201)
        with pytest.raises(ApiError):
            self._p(offset=-1)


class TestPhoneMasking:
    def test_masks_middle_digits_keeps_prefix_and_suffix(self):
        from ladini.services.commercial.admin_api import _mask_phone

        assert _mask_phone("+22670001122") == "+22*******22"

    def test_none_and_short_values(self):
        from ladini.services.commercial.admin_api import _mask_phone

        assert _mask_phone(None) is None
        assert _mask_phone("12") == "**"


class TestSendFollowUpValidation:
    """Validation pure (pas de DB) : messages vides/trop longs, cible absente."""

    def test_empty_message_is_rejected(self):
        import asyncio

        from ladini.services.commercial.admin_api import ApiError, send_follow_up

        with pytest.raises(ApiError) as e:
            asyncio.run(send_follow_up(session=None, actor_id=str(uuid.uuid4()), user_id_raw=str(uuid.uuid4()), message="   "))
        assert e.value.status == 400

    def test_message_too_long_is_rejected(self):
        import asyncio

        from ladini.services.commercial.admin_api import ApiError, send_follow_up

        with pytest.raises(ApiError) as e:
            asyncio.run(
                send_follow_up(session=None, actor_id=str(uuid.uuid4()), user_id_raw=str(uuid.uuid4()), message="x" * 2001)
            )
        assert e.value.status == 400

    def test_malformed_user_id_is_404_not_500(self):
        import asyncio

        from ladini.services.commercial.admin_api import ApiError, send_follow_up

        with pytest.raises(ApiError) as e:
            asyncio.run(send_follow_up(session=None, actor_id=str(uuid.uuid4()), user_id_raw="not-a-uuid", message="hello"))
        assert e.value.status == 404


class TestAuthorization:
    def _client(self, monkeypatch, token):
        from ladini.api.main import app
        from ladini.core.settings import settings

        monkeypatch.setattr(settings, "INTERNAL_API_TOKEN", token, raising=False)
        return TestClient(app, raise_server_exceptions=False)

    PATHS = [
        ("GET", "/internal/commercial/conversations"),
        ("GET", f"/internal/commercial/conversations/{uuid.uuid4()}"),
        ("POST", f"/internal/commercial/conversations/{uuid.uuid4()}/follow-up"),
        ("PATCH", f"/internal/commercial/conversations/{uuid.uuid4()}/status"),
    ]

    @pytest.mark.parametrize("method,path", PATHS)
    def test_no_or_wrong_token_is_401_and_unconfigured_is_503(self, monkeypatch, method, path):
        c = self._client(monkeypatch, "secret")
        assert c.request(method, path).status_code == 401
        assert c.request(method, path, headers={"X-Internal-Token": "wrong"}).status_code == 401
        c = self._client(monkeypatch, "")
        assert c.request(method, path, headers={"X-Internal-Token": "secret"}).status_code == 503

    def test_follow_up_without_actor_id_is_400(self, monkeypatch):
        c = self._client(monkeypatch, "secret")
        r = c.post(
            f"/internal/commercial/conversations/{uuid.uuid4()}/follow-up",
            headers={"X-Internal-Token": "secret"},
            json={"message": "hello"},
        )
        assert r.status_code == 400

    def test_status_update_without_actor_id_is_400(self, monkeypatch):
        c = self._client(monkeypatch, "secret")
        r = c.patch(
            f"/internal/commercial/conversations/{uuid.uuid4()}/status",
            headers={"X-Internal-Token": "secret"},
            json={"status": "RESOLVED"},
        )
        assert r.status_code == 400

    def test_internal_errors_never_leak(self, monkeypatch):
        import ladini.api.routes.commercial_admin as route

        def boom():
            raise RuntimeError("postgres://user:pass@db/secret")

        monkeypatch.setattr(route, "get_sessionmaker", boom)
        c = self._client(monkeypatch, "secret")
        r = c.get("/internal/commercial/conversations", headers={"X-Internal-Token": "secret"})
        assert r.status_code == 500 and "postgres" not in r.text and "secret" not in r.text.replace("Erreur", "")


class TestUpdateStatusValidation:
    def test_invalid_status_is_rejected(self):
        import asyncio

        from ladini.services.commercial.admin_api import ApiError, update_status

        with pytest.raises(ApiError) as e:
            asyncio.run(
                update_status(session=None, actor_id=str(uuid.uuid4()), user_id_raw=str(uuid.uuid4()), status="BOGUS")
            )
        assert e.value.status == 400


# ─────────────────────────────────────────────────────────────────────────────
# LE test critique du cahier des charges : un follow-up commercial ne doit
# JAMAIS muter current_goal / pending_field / stable_entities / drafts d'un
# Workspace, et ne doit jamais passer par input_interpreter (LangGraph).
# ─────────────────────────────────────────────────────────────────────────────
class TestFollowUpNeverTouchesAgentState:
    def test_send_follow_up_never_calls_workspace_store_save(self, monkeypatch):
        """Fake WorkspaceStore dont `.save()` ferait échouer le test s'il était
        appelé — `send_follow_up` ne doit même pas l'importer/instancier."""
        import asyncio

        import ladini.services.commercial.admin_api as api

        class _ExplodingWorkspaceStore:
            def __init__(self):
                raise AssertionError("send_follow_up ne doit jamais instancier WorkspaceStore")

        monkeypatch.setattr(api, "WorkspaceStore", _ExplodingWorkspaceStore)

        class _FakeResult:
            def scalar_one_or_none(self):
                return None

        class _FakeSession:
            async def execute(self, *a, **k):
                return _FakeResult()

        with pytest.raises(api.ApiError) as e:
            asyncio.run(
                api.send_follow_up(
                    session=_FakeSession(), actor_id=str(uuid.uuid4()), user_id_raw=str(uuid.uuid4()), message="Bonjour"
                )
            )
        # 404 utilisateur introuvable (fake session) — le point vérifié est
        # que WorkspaceStore n'a jamais été construit dans ce chemin.
        assert e.value.status == 404

    def test_send_follow_up_state_unchanged_end_to_end(self, monkeypatch):
        """Reproduit le scénario du cahier des charges : avant/après un
        follow-up, un Workspace en tunnel SALES_PUBLISH_PRODUCT/price reste
        strictement identique — la fonction ne lit/écrit jamais ce Workspace."""
        import asyncio

        import ladini.services.commercial.admin_api as api

        touched: list[str] = []

        class _ExplodingWorkspaceStore:
            def __init__(self):
                touched.append("instantiated")

        monkeypatch.setattr(api, "WorkspaceStore", _ExplodingWorkspaceStore)

        user_id = uuid.uuid4()
        fake_user = SimpleNamespace(id=user_id, phone="+22670001122", account_status="ACTIVE")

        class _FakeResult:
            def __init__(self, value):
                self._value = value

            def scalar_one_or_none(self):
                return self._value

        calls = {"n": 0}

        class _FakeSession:
            def add(self, _obj):
                pass

            async def commit(self):
                pass

            async def execute(self, *a, **k):
                calls["n"] += 1
                # 1st SELECT: the User row. 2nd SELECT: the CommercialFollowup row (None -> insert path).
                return _FakeResult(fake_user if calls["n"] == 1 else None)

        from ladini.workers.outbox.channels.base import SendResult

        async def _send(self, *, body, recipient_phone=None, **kw):
            return SendResult.success(provider_ref="SMxxxx")

        monkeypatch.setattr(api.WhatsAppChannel, "send", _send)

        result = asyncio.run(
            api.send_follow_up(session=_FakeSession(), actor_id=str(uuid.uuid4()), user_id_raw=str(user_id), message="Bonjour, toujours dispo ?")
        )
        assert result == {"sent": True, "provider_ref": "SMxxxx"}
        # WorkspaceStore was never touched by the commercial send path.
        assert touched == []
