"""Payload builders for the internal producer-analytics API (Phase D).

Mirrors `admin_api.py` (buyer, Phase E) exactly in shape and posture, reusing its
generic, non-buyer-specific pieces directly (`ApiError`, `_uuid`/`_day` parsing,
`parse_paging`, `filter_options`, `_labels`/`_label`, zone-hierarchy `resolve_filters`,
`cached`) rather than re-implementing them — per the mission's own instruction not
to build a second parallel architecture. Only what genuinely differs (no `journey`
dimension on the producer side; a `producer_id` grain instead of `buyer_id`; a live
`available_supply` gauge with no period/compare semantics) gets its own code here.

Every number is produced by `ProducerAnalyticsService` — no KPI is computed here
beyond presentation metadata (reliability, delta kind, labels).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ladini.domain.analytics.metric_dictionary import METRICS
from ladini.domain.analytics.metric_layer import DataStatus, MetricResult
from ladini.domain.analytics.producer_metric_layer import (
    BINDINGS_PRODUCER,
    TABLE_DIMENSIONS_PRODUCER,
    UNAVAILABLE_PRODUCER,
    reliability_of_producer,
)
from ladini.services.analytics import admin_api as buyer_api
from ladini.services.analytics.data_quality import (
    STALE_AFTER,
    run_producer_metric_quality_checks,
)
from ladini.services.analytics.producer_analytics_service import (
    ProducerAnalyticsService,
)

# Re-exported as-is: generic parsing/labeling/error plumbing with zero buyer-specific
# coupling (see module docstring). Importers of this module can use either name.
ApiError = buyer_api.ApiError
parse_paging = buyer_api.parse_paging
filter_options = buyer_api.filter_options
_labels = buyer_api._labels
_label = buyer_api._label
_uuid = buyer_api._uuid
_day = buyer_api._day

DEFAULT_WINDOW_DAYS = buyer_api.DEFAULT_WINDOW_DAYS
MAX_WINDOW_DAYS = buyer_api.MAX_WINDOW_DAYS

ANALYTICS_TABLES_PRODUCER = (
    "producer_daily_metrics", "producer_quantity_daily_metrics", "producer_supply_daily_snapshot",
)

OVERVIEW_METRICS_PRODUCER = (
    "active_producers", "producer_order_fulfillment_rate", "producer_quantity_fulfillment_rate",
    "producer_delivered_gmv", "delivered_gmv_per_active_producer", "repeat_producer_rate", "time_to_first_sale",
)
FULFILLMENT_METRICS_PRODUCER = ("producer_order_fulfillment_rate", "producer_quantity_fulfillment_rate")
GMV_METRICS_PRODUCER = ("producer_delivered_gmv", "delivered_gmv_per_active_producer", "producer_paid_gmv")
RETENTION_METRICS_PRODUCER = ("repeat_producer_rate",)
#: Metrics with a timeseries/breakdown (the 3 single-table bindings + the one service special).
SERIES_METRICS_PRODUCER = tuple(sorted(set(BINDINGS_PRODUCER) | {"active_producers"}))


@dataclass(frozen=True)
class Params:
    """Same shape as `admin_api.Params`, minus `journey` (no journey dimension exists
    on any producer table — see `TABLE_DIMENSIONS_PRODUCER`). Duck-typed so the
    buyer-side `resolve_filters`/`freshness`-style helpers work unchanged on it."""

    start: date
    end: date
    zone_id: Optional[str] = None
    category_id: Optional[str] = None
    sub_category_id: Optional[str] = None

    @property
    def cache_key(self) -> dict[str, Any]:
        return {"from": self.start.isoformat(), "to": self.end.isoformat(), "zone": self.zone_id,
                "category": self.category_id, "sub_category": self.sub_category_id}


def parse_params(query: Mapping[str, Any], *, today: Optional[date] = None) -> Params:
    """Dates are UTC calendar days, both bounds INCLUSIVE — identical contract to
    `admin_api.parse_params`, just without a `journey` parameter to parse."""
    today = today or datetime.now(timezone.utc).date()
    end = _day(query.get("to"), "to") or today
    start = _day(query.get("from"), "from") or end - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
    if start > end:
        raise ApiError(400, "'from' doit être antérieur ou égal à 'to'.")
    if (end - start).days + 1 > MAX_WINDOW_DAYS:
        raise ApiError(400, f"Fenêtre trop large (maximum {MAX_WINDOW_DAYS} jours).")
    return Params(start, end, _uuid(query.get("zone_id"), "zone_id"), _uuid(query.get("category_id"), "category_id"),
                  _uuid(query.get("sub_category_id"), "sub_category_id"))


async def resolve_filters(session: AsyncSession, p: Params) -> dict[str, Any]:
    """Zone expands to the zone + all descendants, exactly as the buyer side does —
    `admin_api.resolve_filters` only reads `p.zone_id`/`p.category_id`/`p.sub_category_id`,
    so it works unchanged on this module's `Params` too (no `journey` field to miss)."""
    result: dict[str, Any] = await buyer_api.resolve_filters(session, p)  # type: ignore[arg-type]
    return result


# ---------------------------------------------------------------------------------------------- metrics

def metric_payload(res: MetricResult) -> dict[str, Any]:
    d: dict[str, Any] = res.as_dict()
    d["label"] = METRICS[res.metric_name].description if res.metric_name in METRICS else res.metric_name
    d["reliability"] = reliability_of_producer(res.metric_name)
    is_ratio = res.unit == "ratio"
    d["delta_kind"] = "percentage_points" if is_ratio else "relative"
    if res.delta is not None and res.previous_value:
        d["delta_pct"] = res.delta / abs(res.previous_value) if not is_ratio else None
    else:
        d["delta_pct"] = None
    if is_ratio and res.delta is not None:
        d["delta_points"] = res.delta * 100
    return d


async def safe_metric(
    svc: ProducerAnalyticsService, name: str, p: Params, filters: Mapping[str, Any], *, compare: bool = True,
) -> dict[str, Any]:
    """A filter the metric's table cannot honour (e.g. zone_id on the quantity-fulfillment
    table, which only has canonical_unit) is reported as UNAVAILABLE with the reason —
    never ignored, never a silent 0. Same posture as `admin_api.safe_metric`."""
    try:
        res = await svc.get_metric(name, p.start, p.end, filters=filters, compare=compare)
    except ValueError as exc:
        res = MetricResult(metric_name=name, value=None, numerator=None, denominator=None, unit=None,
                           status=DataStatus.UNAVAILABLE, period={"start": p.start.isoformat(), "end": p.end.isoformat()},
                           notes=[f"Not available for the selected filters: {exc}"])
    return metric_payload(res)


def not_available() -> list[dict[str, str]]:
    return [{"metric_name": n, "label": METRICS[n].description if n in METRICS else n, "reason": r}
            for n, r in sorted(UNAVAILABLE_PRODUCER.items())]


def _meta(p: Params) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {"from": p.start.isoformat(), "to": p.end.isoformat(), "timezone": "UTC", "inclusive": True},
        "filters": {"zone_id": p.zone_id, "category_id": p.category_id, "sub_category_id": p.sub_category_id},
    }


async def _metrics(svc: ProducerAnalyticsService, names: tuple[str, ...], p: Params, filters: Mapping[str, Any]) -> dict[str, Any]:
    return {n: await safe_metric(svc, n, p, filters) for n in names}


async def overview(session: AsyncSession, p: Params) -> dict[str, Any]:
    svc = ProducerAnalyticsService(session)
    filters = await resolve_filters(session, p)
    return {**_meta(p), "metrics": await _metrics(svc, OVERVIEW_METRICS_PRODUCER, p, filters),
            "not_available": not_available(), "freshness": await freshness(session)}


async def supply(session: AsyncSession, p: Params) -> dict[str, Any]:
    """LIVE gauge only — `available_supply` is never a period flow (§ProducerAnalyticsService
    docstring). `p.start`/`p.end` are accepted for a uniform request shape but ignored by the
    service itself, which always reads 'today'."""
    svc = ProducerAnalyticsService(session)
    filters = await resolve_filters(session, p)
    result = await svc.get_available_supply(filters=filters)
    return {**_meta(p), "metric": {**result, "label": METRICS["available_supply"].description if "available_supply" in METRICS else "available_supply",
                                    "reliability": reliability_of_producer("available_supply")}}


async def fulfillment(session: AsyncSession, p: Params) -> dict[str, Any]:
    svc = ProducerAnalyticsService(session)
    filters = await resolve_filters(session, p)
    return {**_meta(p), "metrics": await _metrics(svc, FULFILLMENT_METRICS_PRODUCER, p, filters),
            "notes": ["producer_quantity_fulfillment_rate is DIRECT+RECURRING only — TENDER has no OrderItem "
                      "(structurally absent, not degraded to 0). It also cannot be filtered by zone (its table "
                      "has no zone_id dimension); such a request reports UNAVAILABLE with the reason, not a silent 0."]}


async def gmv(session: AsyncSession, p: Params) -> dict[str, Any]:
    svc = ProducerAnalyticsService(session)
    filters = await resolve_filters(session, p)
    return {**_meta(p), "metrics": await _metrics(svc, GMV_METRICS_PRODUCER, p, filters)}


async def retention(session: AsyncSession, p: Params) -> dict[str, Any]:
    svc = ProducerAnalyticsService(session)
    filters = await resolve_filters(session, p)
    return {**_meta(p), "metrics": await _metrics(svc, RETENTION_METRICS_PRODUCER, p, filters),
            "notes": ["repeat_producer_rate's window is PROVISIONAL (not yet calibrated against real cadence "
                      "data) — segment by category before treating it as a stable target."]}


def _series_binding(metric: str, p: Params) -> None:
    if metric in UNAVAILABLE_PRODUCER:
        raise ApiError(400, f"{metric} est indisponible : {UNAVAILABLE_PRODUCER[metric]}")
    if metric not in SERIES_METRICS_PRODUCER:
        raise ApiError(404, "Métrique inconnue.")


def pick_granularity(p: Params, requested: Optional[str]) -> str:
    if requested:
        if requested not in ("day", "week", "month"):
            raise ApiError(400, "granularity invalide (day|week|month).")
        return requested
    days = (p.end - p.start).days + 1
    return "day" if days <= 45 else ("week" if days <= 180 else "month")


async def timeseries(session: AsyncSession, p: Params, metric: str, granularity: Optional[str]) -> dict[str, Any]:
    _series_binding(metric, p)
    svc = ProducerAnalyticsService(session)
    filters = await resolve_filters(session, p)
    g = pick_granularity(p, granularity)
    try:
        points = await svc.get_metric_timeseries(metric, p.start, p.end, granularity=g, filters=filters)
        unavailable_note = None
    except ValueError as exc:
        points, unavailable_note = [], f"Not available for the selected filters: {exc}"
    b = BINDINGS_PRODUCER.get(metric)
    return {**_meta(p), "metric_name": metric, "granularity": g, "reliability": reliability_of_producer(metric),
            "unit": (b.unit if b else "producers"), "physical": bool(b and b.physical), "points": points,
            "notes": [n for n in (b.note if b else "", unavailable_note) if n]}


async def breakdown(session: AsyncSession, p: Params, metric: str, dimension: str, *, limit: int, offset: int) -> dict[str, Any]:
    _series_binding(metric, p)
    if metric == "active_producers":
        raise ApiError(400, f"{metric} n'a pas de ventilation (pas de binding table unique).")
    b = BINDINGS_PRODUCER[metric]
    if dimension not in TABLE_DIMENSIONS_PRODUCER[b.table]:
        raise ApiError(400, f"{metric} ne peut pas être ventilée par {dimension} (dimensions valides : {TABLE_DIMENSIONS_PRODUCER[b.table]}).")
    svc = ProducerAnalyticsService(session)
    filters = await resolve_filters(session, p)
    try:
        rows = await svc.get_metric_breakdown(metric, p.start, p.end, dimension=dimension, filters=filters)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    out = list(rows)
    if dimension == "zone_id":
        labels = await _labels(session)
        for r in out:
            r["id"] = r["zone_id"]
            r["label"] = _label(labels["zone"], r["zone_id"], "Zone non renseignée")
    else:
        for r in out:
            r["id"] = r[dimension]
            r["label"] = r[dimension]
    out.sort(key=lambda r: (-(r.get("denominator") or r.get("numerator") or 0), str(r["label"])))
    return {**_meta(p), "metric_name": metric, "dimension": dimension, "reliability": reliability_of_producer(metric),
            "unit": b.unit, "physical": b.physical, "total": len(out), "limit": limit, "offset": offset,
            "rows": out[offset:offset + limit], "notes": [b.note] if b.note else []}


async def compare(session: AsyncSession, p: Params, metric: str, prev_from: Optional[date], prev_to: Optional[date]) -> dict[str, Any]:
    _series_binding(metric, p)
    length = (p.end - p.start).days + 1
    pe = prev_to or (p.start - timedelta(days=1))
    ps = prev_from or (pe - timedelta(days=length - 1))
    if ps > pe:
        raise ApiError(400, "Période précédente invalide.")
    svc = ProducerAnalyticsService(session)
    filters = await resolve_filters(session, p)
    prev = Params(ps, pe, p.zone_id, p.category_id, p.sub_category_id)
    cur_payload = await safe_metric(svc, metric, p, filters, compare=False)
    prev_payload = await safe_metric(svc, metric, prev, filters, compare=False)
    delta = None
    if cur_payload["value"] is not None and prev_payload["value"] is not None:
        delta = cur_payload["value"] - prev_payload["value"]
    return {**_meta(p), "metric_name": metric, "current": cur_payload, "previous": prev_payload, "delta": delta,
            "delta_kind": cur_payload["delta_kind"],
            "previous_period": {"from": ps.isoformat(), "to": pe.isoformat()}}


# ---------------------------------------------------------------------------------------------- health

async def freshness(session: AsyncSession, *, now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    tables = {}
    latest: Optional[datetime] = None
    for t in ANALYTICS_TABLES_PRODUCER:
        last = (await session.execute(text(f"SELECT max(computed_at) FROM analytics.{t}"))).scalar()
        tables[t] = last.isoformat() if last else None
        if last and (latest is None or last > latest):
            latest = last
    age = (now - latest) if latest else None
    stale = latest is None or age > STALE_AFTER  # type: ignore[operator]
    return {"last_refresh": latest.isoformat() if latest else None,
            "age_seconds": int(age.total_seconds()) if age is not None else None,
            "stale": bool(stale), "stale_after_hours": int(STALE_AFTER.total_seconds() // 3600), "tables": tables}


async def health(session: AsyncSession, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Healthy / Warning / Stale — identical posture to `admin_api.health`: a per-table
    `stale_refresh` issue is ignored (a table with zero facts ever isn't stale), staleness
    is judged on the latest refresh across all three producer tables."""
    now = now or datetime.now(timezone.utc)
    fresh = await freshness(session, now=now)
    end = now.date()
    issues = [i for i in await run_producer_metric_quality_checks(session, end - timedelta(days=29), end, now=now) if i.check != "stale_refresh"]
    status = "Stale" if fresh["stale"] else ("Warning" if issues else "Healthy")
    return {"status": status, "freshness": fresh,
            "issues": [{"check": i.check, "table": i.table, "severity": i.severity, "count": i.count, "detail": i.detail} for i in issues],
            "generated_at": now.isoformat()}


__all__ = [
    "ApiError", "Params", "parse_params", "parse_paging", "overview", "supply", "fulfillment", "gmv",
    "retention", "timeseries", "breakdown", "compare", "filter_options", "freshness", "health",
    "metric_payload", "safe_metric", "SERIES_METRICS_PRODUCER",
]
