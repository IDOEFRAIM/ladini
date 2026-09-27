"""Payload builders for the internal Market Balance API (Phase E).

Mirrors `admin_api.py`/`producer_admin_api.py` exactly in shape and posture, reusing their
generic, non-journey-specific pieces directly (`ApiError`, `_uuid`/`_day` parsing, `filter_options`,
`_labels`/`_label`) rather than re-implementing them. Market Balance has no `buyer_id`/`producer_id`
grain and no `journey` filter of its own (its dimensions are `zone_scope`, `category_id`,
`sub_category_id`, `canonical_unit` — see `docs/analytics/MARKET_BALANCE.md` §13/§21).

Every number is produced by `MarketBalanceService` — no KPI is computed here beyond presentation
metadata (zone/category/sub-category labels).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from ladini.services.analytics import admin_api as buyer_api
from ladini.services.analytics.data_quality import run_market_balance_quality_checks
from ladini.services.analytics.market_balance_service import MarketBalanceService

ApiError = buyer_api.ApiError
filter_options = buyer_api.filter_options
_labels = buyer_api._labels
_label = buyer_api._label
_uuid = buyer_api._uuid
_day = buyer_api._day

DEFAULT_WINDOW_DAYS = buyer_api.DEFAULT_WINDOW_DAYS
MAX_WINDOW_DAYS = buyer_api.MAX_WINDOW_DAYS


@dataclass(frozen=True)
class Params:
    zone_scope: Optional[str] = None
    category_id: Optional[str] = None
    sub_category_id: Optional[str] = None
    canonical_unit: Optional[str] = None

    @property
    def cache_key(self) -> dict[str, Any]:
        return {"zone": self.zone_scope, "category": self.category_id, "sub_category": self.sub_category_id, "unit": self.canonical_unit}


def parse_params(query: Mapping[str, Any]) -> Params:
    return Params(_uuid(query.get("zone_scope"), "zone_scope"), _uuid(query.get("category_id"), "category_id"),
                  _uuid(query.get("sub_category_id"), "sub_category_id"), (query.get("canonical_unit") or None))


async def resolve_filters(session: AsyncSession, p: Params) -> dict[str, Any]:
    """Zone expands to the zone + all descendants, same convention as the buyer/producer sides —
    `admin_api.resolve_filters` only reads `.zone_id`/`.category_id`/`.sub_category_id` off its
    argument (never `.canonical_unit`, which doesn't exist on the buyer side), so a tiny shim
    exposes this module's `Params` under those names rather than duplicating the recursive
    zone-hierarchy SQL a third time. Its result keys the expanded zone list as `zone_id` and the
    original single id as `zone_scope` (its own target-resolution convention) — neither name
    matches this table's actual `zone_scope` column, so both are re-keyed explicitly below."""
    raw: dict[str, Any] = await buyer_api.resolve_filters(session, _ZoneIdShim(p))  # type: ignore[arg-type]
    filters: dict[str, Any] = {}
    if "zone_id" in raw:
        filters["zone_scope"] = raw["zone_id"]  # the expanded [zone + descendants] list
    if "category_id" in raw:
        filters["category_id"] = raw["category_id"]
    if "sub_category_id" in raw:
        filters["sub_category_id"] = raw["sub_category_id"]
    if p.canonical_unit:
        filters["canonical_unit"] = p.canonical_unit
    return filters


@dataclass(frozen=True)
class _ZoneIdShim:
    p: "Params"

    @property
    def zone_id(self) -> Optional[str]:
        return self.p.zone_scope

    @property
    def category_id(self) -> Optional[str]:
        return self.p.category_id

    @property
    def sub_category_id(self) -> Optional[str]:
        return self.p.sub_category_id


def _meta() -> dict[str, Any]:
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "filters": {}}


async def overview(session: AsyncSession, p: Params) -> dict[str, Any]:
    svc = MarketBalanceService(session)
    filters = await resolve_filters(session, p)
    result = await svc.get_balance_overview(filters=filters)
    return {**_meta(), **result}


async def current(session: AsyncSession, p: Params) -> dict[str, Any]:
    svc = MarketBalanceService(session)
    filters = await resolve_filters(session, p)
    result = await svc.get_current_balance(filters=filters)
    labels = await _labels(session)
    for r in result.get("rows", []):
        r["zone_label"] = _label(labels["zone"], r["zone"], "Zone non renseignée")
        r["category_label"] = _label(labels["category"], r["category"], "Non attribuée")
        r["subcategory_label"] = _label(labels["sub_category"], r["subcategory"], "Non attribuée")
    return {**_meta(), **result}


async def demand_gaps(session: AsyncSession, p: Params, *, limit: int) -> dict[str, Any]:
    svc = MarketBalanceService(session)
    filters = await resolve_filters(session, p)
    result = await svc.get_demand_gaps(filters=filters, limit=limit)
    labels = await _labels(session)
    for group in result.get("groups", []):
        for r in group["rows"]:
            r["zone_label"] = _label(labels["zone"], r["zone"], "Zone non renseignée")
            r["subcategory_label"] = _label(labels["sub_category"], r["subcategory"], "Non attribuée")
    return {**_meta(), **result}


async def excess_supply(session: AsyncSession, p: Params, *, limit: int) -> dict[str, Any]:
    svc = MarketBalanceService(session)
    filters = await resolve_filters(session, p)
    result = await svc.get_excess_supply(filters=filters, limit=limit)
    labels = await _labels(session)
    for group in result.get("groups", []):
        for r in group["rows"]:
            r["zone_label"] = _label(labels["zone"], r["zone"], "Zone non renseignée")
            r["subcategory_label"] = _label(labels["sub_category"], r["subcategory"], "Non attribuée")
    return {**_meta(), **result}


async def timeseries(session: AsyncSession, p: Params, *, start: Optional[date], end: Optional[date]) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    e = end or today
    s = start or e - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
    if s > e:
        raise ApiError(400, "'from' doit être antérieur ou égal à 'to'.")
    if (e - s).days + 1 > MAX_WINDOW_DAYS:
        raise ApiError(400, f"Fenêtre trop large (maximum {MAX_WINDOW_DAYS} jours).")
    svc = MarketBalanceService(session)
    filters = await resolve_filters(session, p)
    result = await svc.get_balance_timeseries(s, e, filters=filters)
    return {**_meta(), **result}


async def health(session: AsyncSession, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Healthy / Warning / Stale — same posture as the buyer/producer `health` endpoints: a
    per-table `stale_refresh` alone (never refreshed yet, no facts to refresh) is ignored when the
    upstream dependency itself has no reason to have run (e.g. a fresh pilot environment)."""
    now = now or datetime.now(timezone.utc)
    svc = MarketBalanceService(session)
    fresh = await svc.freshness(now=now)
    issues = [i for i in await run_market_balance_quality_checks(session, now=now) if i.check != "stale_refresh"]
    status = "Stale" if fresh["stale"] else ("Warning" if issues else "Healthy")
    return {"status": status, "freshness": fresh,
            "issues": [{"check": i.check, "table": i.table, "severity": i.severity, "count": i.count, "detail": i.detail} for i in issues],
            "generated_at": now.isoformat()}


__all__ = [
    "ApiError", "Params", "parse_params", "resolve_filters", "overview", "current", "demand_gaps",
    "excess_supply", "timeseries", "filter_options", "health",
]
