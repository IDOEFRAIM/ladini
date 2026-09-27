"""Internal buyer-analytics API (Phase E) - server-to-server, read-only.

Auth: `require_internal_token` at the ROUTER level (fail-closed, same posture as `/api/market/*`).
The only caller is the Next.js admin adapter (`app/api/admin/analytics/*`), which first enforces the
admin session (`requireAdmin`). The browser never reaches this router, and no KPI is computed anywhere
but `AnalyticsService`.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Awaitable, Callable, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from ladini.api.security import require_internal_token
from ladini.core.database import get_sessionmaker
from ladini.services.analytics import admin_api as api
from ladini.services.analytics.cache import cached

logger = logging.getLogger("Ladini.API.AnalyticsAdmin")

router = APIRouter(prefix="/internal/analytics/buyers", tags=["Analytics (internal)"], dependencies=[Depends(require_internal_token)])


async def _serve(endpoint: str, query: Mapping[str, Any], build: Callable[..., Awaitable[dict[str, Any]]], **kwargs: Any) -> dict[str, Any]:
    try:
        params = api.parse_params(query)

        async def produce() -> dict[str, Any]:
            factory = get_sessionmaker()
            async with factory() as session:  # read-only use: nothing is ever committed here
                return await build(session, params, **kwargs)

        key = {**params.cache_key, **{k: str(v) for k, v in kwargs.items()}}
        result: dict[str, Any] = await cached(endpoint, key, produce)
        return result
    except api.ApiError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - never leak internals
        logger.exception("analytics endpoint failed | %s", endpoint)
        raise HTTPException(status_code=500, detail="Erreur lors du calcul des indicateurs.") from None


@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    return await _serve("overview", request.query_params, api.overview)


@router.get("/direct")
async def direct(request: Request) -> dict[str, Any]:
    return await _serve("direct", request.query_params, api.direct)


@router.get("/tenders")
async def tenders(request: Request) -> dict[str, Any]:
    return await _serve("tenders", request.query_params, api.tenders)


@router.get("/recurring")
async def recurring(request: Request) -> dict[str, Any]:
    return await _serve("recurring", request.query_params, api.recurring)


@router.get("/unmatched-demand")
async def unmatched_demand(request: Request) -> dict[str, Any]:
    try:
        limit, offset = api.parse_paging(request.query_params, default=50, maximum=200)
    except api.ApiError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    return await _serve("unmatched", request.query_params, api.unmatched_demand, limit=limit, offset=offset)


@router.get("/filters")
async def filters() -> dict[str, Any]:
    async def produce() -> dict[str, Any]:
        async with get_sessionmaker()() as session:
            options: dict[str, Any] = await api.filter_options(session)
            return options

    try:
        filters_payload: dict[str, Any] = await cached("filters", {}, produce, ttl=600)
        return filters_payload
    except Exception:  # noqa: BLE001
        logger.exception("analytics filters failed")
        raise HTTPException(status_code=500, detail="Erreur lors du chargement des filtres.") from None


@router.get("/health")
async def health() -> dict[str, Any]:
    async def produce() -> dict[str, Any]:
        async with get_sessionmaker()() as session:
            report: dict[str, Any] = await api.health(session)
            return report

    try:
        health_payload: dict[str, Any] = await cached("health", {}, produce, ttl=60)
        return health_payload
    except Exception:  # noqa: BLE001
        logger.exception("analytics health failed")
        raise HTTPException(status_code=500, detail="Erreur lors du contrôle de santé.") from None


@router.get("/metrics/{metric}/timeseries")
async def metric_timeseries(metric: str, request: Request) -> dict[str, Any]:
    return await _serve("timeseries", request.query_params, api.timeseries, metric=metric,
                        granularity=request.query_params.get("granularity"))


@router.get("/metrics/{metric}/breakdown")
async def metric_breakdown(metric: str, request: Request) -> dict[str, Any]:
    try:
        limit, offset = api.parse_paging(request.query_params, default=100, maximum=500)
    except api.ApiError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    dimension = request.query_params.get("dimension") or ""
    return await _serve("breakdown", request.query_params, api.breakdown, metric=metric, dimension=dimension, limit=limit, offset=offset)


@router.get("/compare")
async def compare(request: Request) -> dict[str, Any]:
    q = request.query_params
    metric = q.get("metric") or ""
    try:
        prev_from: Optional[date] = api._day(q.get("prev_from"), "prev_from")
        prev_to: Optional[date] = api._day(q.get("prev_to"), "prev_to")
    except api.ApiError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    return await _serve("compare", q, api.compare, metric=metric, prev_from=prev_from, prev_to=prev_to)
