"""`api/routes/webchat.py` — canal chat web synchrone (2026-09-19).

Vérifie le contrat HTTP exact promis au site NextJS : auth par
`X-Internal-Token`, `phone_number`/`message` en entrée, `reply` (texte
direct, jamais un `task_id`) en sortie — et que le bon `workspace_type`
(producer/buyer) est transmis à l'orchestrateur selon la route appelée."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from ladini.core.settings import settings

    monkeypatch.setattr(settings, "INTERNAL_API_TOKEN", "test-secret", raising=False)
    from ladini.api.main import app

    return TestClient(app)


def _mock_orchestrator(monkeypatch, *, return_value=None, side_effect=None):
    from ladini.api.routes import webchat as mod

    mock_handle = AsyncMock(
        return_value=return_value
        or {
            "final_response": "Bonjour patron, comment puis-je aider ?",
            "agent": "market_coach",
            "workspace_id": "+22670000001",
            "interactive": None,
        },
        side_effect=side_effect,
    )
    monkeypatch.setattr(mod._orchestrator, "handle", mock_handle)
    return mock_handle


class TestAuthentication:
    def test_missing_token_is_rejected(self, client, monkeypatch):
        _mock_orchestrator(monkeypatch)
        resp = client.post(
            "/api/webchat/producer",
            json={"phone_number": "+22670000001", "message": "salut"},
        )
        assert resp.status_code == 401

    def test_wrong_token_is_rejected(self, client, monkeypatch):
        _mock_orchestrator(monkeypatch)
        resp = client.post(
            "/api/webchat/producer",
            json={"phone_number": "+22670000001", "message": "salut"},
            headers={"X-Internal-Token": "mauvais-secret"},
        )
        assert resp.status_code == 401

    def test_correct_token_is_accepted(self, client, monkeypatch):
        _mock_orchestrator(monkeypatch)
        resp = client.post(
            "/api/webchat/producer",
            json={"phone_number": "+22670000001", "message": "salut"},
            headers={"X-Internal-Token": "test-secret"},
        )
        assert resp.status_code == 200


class TestResponseContract:
    def test_the_agent_reply_text_is_returned_directly_not_a_task_id(
        self, client, monkeypatch
    ):
        """LE point central de ce canal : contrairement à /api/market/*, la
        réponse HTTP contient directement le TEXTE, jamais un task_id à
        poller."""
        _mock_orchestrator(
            monkeypatch,
            return_value={
                "final_response": "Voici les produits disponibles pres de Bobo.",
                "agent": "market_coach",
                "workspace_id": "+22670000001",
                "interactive": None,
            },
        )
        resp = client.post(
            "/api/webchat/buyer",
            json={"phone_number": "+22670000001", "message": "je cherche des tomates"},
            headers={"X-Internal-Token": "test-secret"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["reply"] == "Voici les produits disponibles pres de Bobo."
        assert "task_id" not in body

    def test_interactive_hint_is_forwarded_when_present(self, client, monkeypatch):
        _mock_orchestrator(
            monkeypatch,
            return_value={
                "final_response": "Confirmes-tu ?",
                "agent": "market_coach",
                "workspace_id": "+22670000001",
                "interactive": {"kind": "confirm"},
            },
        )
        resp = client.post(
            "/api/webchat/producer",
            json={"phone_number": "+22670000001", "message": "oui"},
            headers={"X-Internal-Token": "test-secret"},
        )
        assert resp.json()["interactive"] == {"kind": "confirm"}


class TestRoleIsPinnedByRouteNotByUserText:
    def test_producer_route_passes_producer_workspace_type(self, client, monkeypatch):
        mock_handle = _mock_orchestrator(monkeypatch)
        client.post(
            "/api/webchat/producer",
            json={"phone_number": "+22670000001", "message": "salut"},
            headers={"X-Internal-Token": "test-secret"},
        )
        _, kwargs = mock_handle.call_args
        assert kwargs["workspace_type"] == "producer"
        assert kwargs["force_role"] is True
        assert kwargs["phone"] == "+22670000001"
        assert kwargs["user_query"] == "salut"

    def test_buyer_route_passes_buyer_workspace_type(self, client, monkeypatch):
        mock_handle = _mock_orchestrator(monkeypatch)
        client.post(
            "/api/webchat/buyer",
            json={"phone_number": "+22670000002", "message": "je vends du mais"},
            headers={"X-Internal-Token": "test-secret"},
        )
        _, kwargs = mock_handle.call_args
        assert kwargs["workspace_type"] == "buyer"


class TestInputValidation:
    def test_empty_message_is_rejected(self, client, monkeypatch):
        _mock_orchestrator(monkeypatch)
        resp = client.post(
            "/api/webchat/producer",
            json={"phone_number": "+22670000001", "message": ""},
            headers={"X-Internal-Token": "test-secret"},
        )
        assert resp.status_code == 422

    def test_missing_phone_number_is_rejected(self, client, monkeypatch):
        _mock_orchestrator(monkeypatch)
        resp = client.post(
            "/api/webchat/producer",
            json={"message": "salut"},
            headers={"X-Internal-Token": "test-secret"},
        )
        assert resp.status_code == 422
