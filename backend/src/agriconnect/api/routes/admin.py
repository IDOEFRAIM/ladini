"""Endpoints admin — §26 du brief LLM Gateway : "l'administrateur ne doit pas
avoir besoin de lire les logs pour savoir qu'un LLM est en panne".

Auth : header `X-Admin-Token` comparé à `settings.ADMIN_API_TOKEN` — même
esprit que `MCP_HTTP_AUTH_TOKEN` déjà en place ailleurs dans ce repo pour un
daemon interne. Fail-closed : token absent côté serveur → endpoint refusé
pour TOUT le monde (§26 "ne jamais exposer sans authentification"), pas de
mode "ouvert par défaut si non configuré".
"""

from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter, Header, HTTPException, Response

from agriconnect.core.settings import settings
from agriconnect.graphs.agents.market_coach.llm_gateway import get_llm_gateway
from agriconnect.graphs.agents.market_coach.llm_gateway.types import CircuitState, LLMProfile

router = APIRouter()


def _check_admin_token(x_admin_token: str | None) -> None:
    expected = (getattr(settings, "ADMIN_API_TOKEN", "") or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_API_TOKEN non configuré — endpoint admin désactivé par défaut.",
        )
    if not x_admin_token or x_admin_token.strip() != expected:
        raise HTTPException(status_code=401, detail="Token admin invalide ou absent.")


@router.get("/admin/llm/health")
async def llm_health(x_admin_token: str | None = Header(default=None)):
    """État courant du LLM Gateway — §26 : par profil (primary/selected/status)
    et par provider (agrégé). 503 si au moins un profil n'a AUCUN candidat
    disponible (§36 "tous les providers d'un profil down")."""
    _check_admin_token(x_admin_token)

    gateway = get_llm_gateway()
    registry = gateway.registry()
    health = gateway.health_registry()

    now = time.time()
    candidates_payload: Dict[str, Any] = {}
    provider_states: Dict[str, list] = {}
    profiles_payload: Dict[str, Any] = {}
    any_profile_fully_down = False

    for profile in (LLMProfile.FAST, LLMProfile.REASONING):
        candidates = registry.candidates_for(profile)
        primary = candidates[0].key if candidates else None
        selected = None
        for candidate in candidates:
            record = health.get(candidate.key)
            state = record.state
            available = state != CircuitState.OPEN or (
                record.cooldown_until is not None and now >= record.cooldown_until
            )
            if not record.config_error and available and selected is None:
                selected = candidate.key

            if candidate.key not in candidates_payload:
                percentiles = health.percentiles(candidate.key)
                candidates_payload[candidate.key] = {
                    "profile": profile.value,
                    "provider": candidate.provider,
                    "model": candidate.model,
                    "state": state.value,
                    "config_error": record.config_error,
                    "config_error_message": record.config_error_message,
                    "consecutive_failures": record.consecutive_failures,
                    "consecutive_successes": record.consecutive_successes,
                    "total_requests": record.total_requests,
                    "total_failures": record.total_failures,
                    "timeout_count": record.timeout_count,
                    "failure_rate": (
                        round(record.total_failures / record.total_requests, 3)
                        if record.total_requests
                        else None
                    ),
                    "p50_ms": percentiles.get("p50"),
                    "p95_ms": percentiles.get("p95"),
                    "cooldown_until": record.cooldown_until,
                    "last_success_at": record.last_success_at,
                    "last_failure_at": record.last_failure_at,
                }
            provider_states.setdefault(candidate.provider, []).append(
                candidates_payload[candidate.key]["state"]
                if not record.config_error
                else "CONFIG_ERROR"
            )

        profile_status = (
            "HEALTHY"
            if selected == primary
            else ("DEGRADED" if selected is not None else "DOWN")
        )
        if profile_status == "DOWN":
            any_profile_fully_down = True
        profiles_payload[profile.value] = {
            "primary": primary,
            "selected": selected,
            "status": profile_status,
        }

    providers_payload = {
        provider: (
            "DOWN"
            if all(s in ("OPEN", "CONFIG_ERROR") for s in states)
            else ("DEGRADED" if any(s in ("OPEN", "CONFIG_ERROR") for s in states) else "HEALTHY")
        )
        for provider, states in provider_states.items()
    }

    payload = {
        "status": "degraded" if any_profile_fully_down else "healthy",
        "profiles": profiles_payload,
        "providers": providers_payload,
        "candidates": candidates_payload,
    }
    return Response(
        content=__import__("json").dumps(payload),
        media_type="application/json",
        status_code=503 if any_profile_fully_down else 200,
    )
