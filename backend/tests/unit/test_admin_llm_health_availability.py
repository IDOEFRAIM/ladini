"""`api/routes/admin.py::llm_health` — reflète le pré-check §7 (2026-09-05) :
un candidat `CLOSED` (jamais tenté, disjoncteur "sain") mais structurellement
inutilisable (identifiant manquant) ne doit JAMAIS apparaître `selected`/
disponible — sinon l'admin croit à tort qu'un repli fonctionnel existe.

Appelle la fonction endpoint DIRECTEMENT (pas de `TestClient`/app complète —
`admin.py` n'a aucune autre dépendance FastAPI que le header) : plus rapide,
et suffisant pour verrouiller ce contrat précis."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.conftest import run
from tests.unit.llm_gateway.conftest import make_fake_redis

from agriconnect.graphs.agents.market_coach.llm_gateway.circuit_breaker import (
    CircuitBreaker,
)
from agriconnect.graphs.agents.market_coach.llm_gateway.gateway import LLMGateway
from agriconnect.graphs.agents.market_coach.llm_gateway.health_registry import (
    HealthRegistry,
)
from agriconnect.graphs.agents.market_coach.llm_gateway.types import (
    LLMProfile,
    ModelCandidate,
)


class _StubRegistry:
    def __init__(self, candidates):
        self._candidates = candidates

    def candidates_for(self, profile):
        return [c for c in self._candidates if c.profile == profile]

    def primary_model_name(self, profile):
        matching = self.candidates_for(profile)
        return matching[0].model if matching else None


def _build_gateway(settings) -> LLMGateway:
    candidates = [
        ModelCandidate(
            provider="groq",
            model="llama-3.3-70b-versatile",
            profile=LLMProfile.REASONING,
            priority=0,
            timeout_seconds=2.0,
        ),
    ]
    health = HealthRegistry(redis_client=make_fake_redis({}))
    circuit = CircuitBreaker(
        health,
        failure_threshold=3,
        cooldown_seconds=30.0,
        half_open_probes=1,
        probe_lock_seconds=10.0,
    )
    return LLMGateway(
        registry=_StubRegistry(candidates),
        health_registry=health,
        circuit_breaker=circuit,
        settings=settings,
        client_factory=lambda provider: (_ for _ in ()).throw(AssertionError("never called")),
    )


class TestAdminHealthReflectsCredentialMissing:
    def test_a_never_attempted_but_credential_missing_candidate_is_not_selected(self):
        from agriconnect.api.routes import admin as admin_module

        fake_settings = SimpleNamespace(
            ADMIN_API_TOKEN="test-token",
            MOCK_EXTERNAL_APIS=False,
            llm_api_key="",  # groq: identifiant absent
            OPENAI_API_KEY="",
        )
        gateway = _build_gateway(fake_settings)

        with patch.object(admin_module, "settings", fake_settings), patch.object(
            admin_module, "get_llm_gateway", return_value=gateway
        ):
            response = run(admin_module.llm_health(x_admin_token="test-token"))

        payload = json.loads(response.body)
        reasoning = payload["profiles"]["REASONING"]
        # Le disjoncteur est CLOSED (jamais tenté) — sans le pré-check, ce
        # candidat serait à tort "selected" et le profil "HEALTHY".
        assert reasoning["selected"] is None
        assert reasoning["status"] == "DOWN"
        assert payload["candidates"]["groq:llama-3.3-70b-versatile"]["availability"] == (
            "CREDENTIAL_MISSING"
        )
        assert payload["providers"]["groq"] == "DOWN"
        assert response.status_code == 503

    def test_a_configured_and_healthy_candidate_is_selected_and_reported_ok(self):
        from agriconnect.api.routes import admin as admin_module

        fake_settings = SimpleNamespace(
            ADMIN_API_TOKEN="test-token",
            MOCK_EXTERNAL_APIS=False,
            llm_api_key="sk-real-key",
            OPENAI_API_KEY="",
        )
        gateway = _build_gateway(fake_settings)

        with patch.object(admin_module, "settings", fake_settings), patch.object(
            admin_module, "get_llm_gateway", return_value=gateway
        ):
            response = run(admin_module.llm_health(x_admin_token="test-token"))

        payload = json.loads(response.body)
        assert payload["profiles"]["REASONING"]["selected"] == "groq:llama-3.3-70b-versatile"
        assert payload["candidates"]["groq:llama-3.3-70b-versatile"]["availability"] == "OK"
        assert response.status_code == 200

    def test_never_leaks_the_credential_value_itself(self):
        from agriconnect.api.routes import admin as admin_module

        fake_settings = SimpleNamespace(
            ADMIN_API_TOKEN="test-token",
            MOCK_EXTERNAL_APIS=False,
            llm_api_key="sk-super-secret-value-should-never-appear",
            OPENAI_API_KEY="",
        )
        gateway = _build_gateway(fake_settings)

        with patch.object(admin_module, "settings", fake_settings), patch.object(
            admin_module, "get_llm_gateway", return_value=gateway
        ):
            response = run(admin_module.llm_health(x_admin_token="test-token"))

        assert b"sk-super-secret-value-should-never-appear" not in response.body


class TestAdminHealthAuth:
    def test_wrong_token_is_rejected(self):
        from fastapi import HTTPException

        from agriconnect.api.routes import admin as admin_module

        fake_settings = SimpleNamespace(ADMIN_API_TOKEN="test-token")
        with patch.object(admin_module, "settings", fake_settings):
            with pytest.raises(HTTPException) as excinfo:
                run(admin_module.llm_health(x_admin_token="wrong"))
        assert excinfo.value.status_code == 401
