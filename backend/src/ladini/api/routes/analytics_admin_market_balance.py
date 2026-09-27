"""Internal Market Balance API (Phase E) - server-to-server, read-only.

Auth: `require_internal_token` at the ROUTER level (fail-closed), exactly the same posture as
`/internal/analytics/{buyers,producers}/*`. The only caller is the Next.js admin adapter
(`app/api/admin/analytics/market-balance/*`), which first enforces the admin session
(`requireAdmin`). The browser never reaches this router, and no KPI is computed anywhere but
`MarketBalanceService`.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Awaitable, Callable, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from ladini.api.security import require_internal_token
from ladini.core.database import get_sessionmaker
from ladini.services.analytics import market_balance_admin_api as api
from ladini.services.analytics.cache import cached

logger = logging.getLogger("Ladini.API.AnalyticsAdminMarketBalance")

router = APIRouter(prefix="/internal/analytics/market-balance", tags=["Analytics (internal)"], dependencies=[Depends(require_internal_token)])

#: Market Balance's own snapshot refreshes at most daily — a short cache still avoids hammering
#: the database on every dashboard tick, same posture as the buyer/producer routers.
_TTL = 120


async def _serve(endpoint: str, query: Mapping[str, Any], build: Callable[..., Awaitable[dict[str, Any]]], **kwargs: Any) -> dict[str, Any]:
    try:
        params = api.parse_params(query)

        async def produce() -> dict[str, Any]:
            factory = get_sessionmaker()
            async with factory() as session:  # read-only use: nothing is ever committed here
                return await build(session, params, **kwargs)

        key = {**params.cache_key, **{k: str(v) for k, v in kwargs.items()}}
        result: dict[str, Any] = await cached(f"market-balance:{endpoint}", key, produce, ttl=_TTL)
        return result
    except api.ApiError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - never leak internals
        logger.exception("market balance analytics endpoint failed | %s", endpoint)
        raise HTTPException(status_code=500, detail="Erreur lors du calcul de la balance de marché.") from None


@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    return await _serve("overview", request.query_params, api.overview)


@router.get("/current")
async def current(request: Request) -> dict[str, Any]:
    return await _serve("current", request.query_params, api.current)


@router.get("/demand-gaps")
async def demand_gaps(request: Request) -> dict[str, Any]:
    limit = int(request.query_params.get("limit") or 50)
    return await _serve("demand-gaps", request.query_params, api.demand_gaps, limit=limit)


@router.get("/excess-supply")
async def excess_supply(request: Request) -> dict[str, Any]:
    limit = int(request.query_params.get("limit") or 50)
    return await _serve("excess-supply", request.query_params, api.excess_supply, limit=limit)


@router.get("/timeseries")
async def timeseries(request: Request) -> dict[str, Any]:
    q = request.query_params
    try:
        raw_start: Optional[date] = api._day(q.get("from"), "from")
        raw_end: Optional[date] = api._day(q.get("to"), "to")
        start, end = api.resolve_window(raw_start, raw_end)
    except api.ApiError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    return await _serve("timeseries", q, api.timeseries, start=start, end=end)


@router.get("/filters")
async def filters() -> dict[str, Any]:
    async def produce() -> dict[str, Any]:
        async with get_sessionmaker()() as session:
            options: dict[str, Any] = await api.filter_options(session)
            return options

    try:
        filters_payload: dict[str, Any] = await cached("market-balance:filters", {}, produce, ttl=600)
        return filters_payload
    except Exception:  # noqa: BLE001
        logger.exception("market balance analytics filters failed")
        raise HTTPException(status_code=500, detail="Erreur lors du chargement des filtres.") from None


@router.get("/health")
async def health() -> dict[str, Any]:
    async def produce() -> dict[str, Any]:
        async with get_sessionmaker()() as session:
            report: dict[str, Any] = await api.health(session)
            return report

    try:
        health_payload: dict[str, Any] = await cached("market-balance:health", {}, produce, ttl=60)
        return health_payload
    except Exception:  # noqa: BLE001
        logger.exception("market balance analytics health failed")
        raise HTTPException(status_code=500, detail="Erreur lors du contrôle de santé.") from None
