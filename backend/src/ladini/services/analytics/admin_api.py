"""Payload builders for the internal buyer-analytics API (Phase E).

The HTTP layer (`api/routes/analytics_admin.py`) and the Next.js admin adapter are thin: every number is
produced by `AnalyticsService` (the single source of truth for KPI formulas). This module only

* validates/normalizes query parameters (UTC calendar days, ids),
* expands a zone filter to its descendants (governance.zones hierarchy),
* calls the service and adds presentation metadata (reliability class, delta kind, labels),
* turns "filter not supported by this table" into an explicit UNAVAILABLE result (never a silent 0).

No KPI is computed here beyond `SUM`-level presentation helpers already provided by the service.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ladini.domain.analytics.daily_aggregation import NIL_UUID
from ladini.domain.analytics.metric_dictionary import METRICS
from ladini.domain.analytics.metric_layer import (
    BINDINGS,
    TABLE_DIMENSIONS,
    UNAVAILABLE,
    DataStatus,
    MetricResult,
    reliability_of,
)
from ladini.services.analytics.analytics_service import MATURITY_DAYS, AnalyticsService
from ladini.services.analytics.data_quality import STALE_AFTER, run_data_quality_checks

DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 400
JOURNEYS = ("DIRECT", "TENDER", "RECURRING")
ANALYTICS_TABLES = ("buyer_daily_metrics", "direct_daily_metrics", "tender_daily_metrics", "recurring_daily_metrics")

OVERVIEW_METRICS = ("active_buyers", "needs_created", "successful_procurement_rate", "repeat_buyer_rate",
                    "potential_gmv", "confirmed_gmv", "delivered_gmv")
DIRECT_METRICS = ("direct_searches", "direct_successful_searches", "direct_search_success_rate", "direct_orders_created",
                  "direct_orders_per_search", "direct_orders_confirmed", "direct_orders_delivered",
                  "direct_fulfillment_rate", "direct_order_delivery_rate", "direct_gmv")
TENDER_METRICS = ("tenders_created", "tenders_with_bid", "tender_response_rate", "average_bids_per_tender", "time_to_first_bid",
                  "tender_winner_rate", "tender_orders_created", "tender_orders_delivered", "tender_fulfillment_rate", "tender_gmv")
RECURRING_METRICS = ("recurring_occurrences", "recurring_requested_quantity", "recurring_matched_quantity",
                     "recurring_confirmed_quantity", "recurring_delivered_quantity", "recurring_coverage_rate",
                     "recurring_full_coverage_rate", "recurring_acceptance_rate", "recurring_skip_rate",
                     "recurring_received_occurrence_rate", "recurring_fulfillment_rate", "recurring_unmatched_quantity")
#: Metrics with a timeseries/breakdown (single-table bindings + the two buyer-level specials).
SERIES_METRICS = tuple(sorted(set(BINDINGS) - {"successful_procurement_rate"} | {"active_buyers", "successful_procurement_rate"}))


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Params:
    start: date
    end: date
    zone_id: Optional[str] = None
    category_id: Optional[str] = None
    sub_category_id: Optional[str] = None
    journey: Optional[str] = None

    @property
    def cache_key(self) -> dict[str, Any]:
        return {"from": self.start.isoformat(), "to": self.end.isoformat(), "zone": self.zone_id,
                "category": self.category_id, "sub_category": self.sub_category_id, "journey": self.journey}


def _uuid(value: Optional[str], name: str) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise ApiError(400, f"Paramètre '{name}' invalide (UUID attendu).") from None


def _day(value: Optional[str], name: str) -> Optional[date]:
    if value in (None, ""):
        return None
    try:
        if len(str(value)) != 10:
            raise ValueError
        return date.fromisoformat(str(value))
    except ValueError:
        raise ApiError(400, f"Paramètre '{name}' invalide (AAAA-MM-JJ attendu, jour UTC).") from None


def parse_params(query: Mapping[str, Any], *, today: Optional[date] = None) -> Params:
    """Dates are UTC calendar days, both bounds INCLUSIVE ('from=2026-09-01&to=2026-09-01' is one full UTC day);
    a time component is rejected so a local-time value can never silently shift the day."""
    today = today or datetime.now(timezone.utc).date()
    end = _day(query.get("to"), "to") or today
    start = _day(query.get("from"), "from") or end - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
    if start > end:
        raise ApiError(400, "'from' doit être antérieur ou égal à 'to'.")
    if (end - start).days + 1 > MAX_WINDOW_DAYS:
        raise ApiError(400, f"Fenêtre trop large (maximum {MAX_WINDOW_DAYS} jours).")
    journey = (str(query.get("journey") or "").upper() or None)
    if journey and journey not in JOURNEYS:
        raise ApiError(400, f"journey invalide (attendu : {', '.join(JOURNEYS)}).")
    return Params(start, end, _uuid(query.get("zone_id"), "zone_id"), _uuid(query.get("category_id"), "category_id"),
                  _uuid(query.get("sub_category_id"), "sub_category_id"), journey)


def parse_paging(query: Mapping[str, Any], *, default: int = 100, maximum: int = 500) -> tuple[int, int]:
    try:
        limit = int(query.get("limit") or default)
        offset = int(query.get("offset") or 0)
    except ValueError:
        raise ApiError(400, "limit/offset invalides.") from None
    if limit < 1 or limit > maximum or offset < 0:
        raise ApiError(400, f"limit doit être entre 1 et {maximum}, offset >= 0.")
    return limit, offset


# ---------------------------------------------------------------------------------------------- filters

async def resolve_filters(session: AsyncSession, p: Params) -> dict[str, Any]:
    """Service-level filters. A zone expands to the zone + all descendants; `zone_scope` keeps the
    selected zone id for target resolution."""
    filters: dict[str, Any] = {}
    if p.zone_id:
        rows = (await session.execute(
            text("WITH RECURSIVE t AS (SELECT id FROM governance.zones WHERE id = :z "
                 "UNION ALL SELECT z.id FROM governance.zones z JOIN t ON z.parent_id = t.id) SELECT id FROM t"),
            {"z": uuid.UUID(p.zone_id)})).all()
        if not rows:
            raise ApiError(400, "Zone inconnue.")
        filters["zone_id"] = [str(r[0]) for r in rows]
        filters["zone_scope"] = p.zone_id
    if p.category_id:
        filters["category_id"] = p.category_id
    if p.sub_category_id:
        filters["sub_category_id"] = p.sub_category_id
    return filters


async def filter_options(session: AsyncSession) -> dict[str, Any]:
    """Real taxonomy for the filter controls (never hardcoded)."""
    cats = (await session.execute(text("SELECT id, name FROM governance.categories ORDER BY name"))).all()
    subs = (await session.execute(text("SELECT id, name, category_id FROM governance.sub_categories ORDER BY name"))).all()
    zones = (await session.execute(text("SELECT id, name, parent_id, depth FROM governance.zones ORDER BY path NULLS LAST, name"))).all()
    return {
        "categories": [{"id": str(i), "name": n} for i, n in cats],
        "sub_categories": [{"id": str(i), "name": n, "category_id": str(c) if c else None} for i, n, c in subs],
        "zones": [{"id": str(i), "name": n, "parent_id": str(pid) if pid else None, "depth": d or 0} for i, n, pid, d in zones],
    }


async def _labels(session: AsyncSession) -> dict[str, dict[str, Any]]:
    opts = await filter_options(session)
    cat = {c["id"]: c["name"] for c in opts["categories"]}
    return {
        "zone": {z["id"]: z["name"] for z in opts["zones"]},
        "category": cat,
        "sub_category": {s["id"]: s["name"] for s in opts["sub_categories"]},
        "sub_category_parent": {s["id"]: s["category_id"] for s in opts["sub_categories"]},
    }


def _label(mapping: Mapping[str, Any], key: Any, unknown: str) -> str:
    k = str(key) if key is not None else None
    if not k or k == NIL_UUID:
        return unknown
    return str(mapping.get(k, k))


# ---------------------------------------------------------------------------------------------- metrics

def metric_payload(res: MetricResult) -> dict[str, Any]:
    d: dict[str, Any] = res.as_dict()
    d["label"] = METRICS[res.metric_name].description if res.metric_name in METRICS else res.metric_name
    d["reliability"] = reliability_of(res.metric_name)
    is_ratio = res.unit == "ratio"
    d["delta_kind"] = "percentage_points" if is_ratio else "relative"
    if res.delta is not None and res.previous_value:
        d["delta_pct"] = res.delta / abs(res.previous_value) if not is_ratio else None
    else:
        d["delta_pct"] = None
    if is_ratio and res.delta is not None:
        d["delta_points"] = res.delta * 100
    return d


async def safe_metric(svc: AnalyticsService, name: str, p: Params, filters: Mapping[str, Any], *, compare: bool = True) -> dict[str, Any]:
    """Compute a metric; a filter the metric's table cannot honour (e.g. category on buyer-level tables)
    is reported as UNAVAILABLE with the reason — never ignored, never a silent 0."""
    try:
        res = await svc.get_metric(name, p.start, p.end, filters=filters, compare=compare)
    except ValueError as exc:
        res = MetricResult(metric_name=name, value=None, numerator=None, denominator=None, unit=None,
                           status=DataStatus.UNAVAILABLE, period={"start": p.start.isoformat(), "end": p.end.isoformat()},
                           notes=[f"Not available for the selected filters: {exc}"])
    return metric_payload(res)


def not_available() -> list[dict[str, str]]:
    return [{"metric_name": n, "label": METRICS[n].description if n in METRICS else n, "reason": r} for n, r in sorted(UNAVAILABLE.items())]


def _meta(p: Params) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {"from": p.start.isoformat(), "to": p.end.isoformat(), "timezone": "UTC", "inclusive": True},
        "filters": {"zone_id": p.zone_id, "category_id": p.category_id, "sub_category_id": p.sub_category_id, "journey": p.journey},
        "maturity_days": MATURITY_DAYS,
    }


async def _metrics(svc: AnalyticsService, names: tuple[str, ...], p: Params, filters: Mapping[str, Any]) -> dict[str, Any]:
    return {n: await safe_metric(svc, n, p, filters) for n in names}


async def overview(session: AsyncSession, p: Params) -> dict[str, Any]:
    svc = AnalyticsService(session)
    filters = await resolve_filters(session, p)
    metrics = await _metrics(svc, OVERVIEW_METRICS, p, filters)
    spr = metrics["successful_procurement_rate"]
    mix = [{"journey": b["journey"], "needs": b["denominator"], "satisfied": b["numerator"], "reliability": b["reliability"]}
           for b in spr.get("breakdown", []) if b["journey"] in JOURNEYS]
    return {**_meta(p), "metrics": metrics, "journey_mix": mix, "not_available": not_available(),
            "freshness": await freshness(session)}


async def direct(session: AsyncSession, p: Params) -> dict[str, Any]:
    svc = AnalyticsService(session)
    filters = await resolve_filters(session, p)
    return {**_meta(p), "metrics": await _metrics(svc, DIRECT_METRICS, p, filters),
            "notes": ["direct_orders_per_search is a window-level ratio: searches and orders are not session-attributed (it can exceed 1 and is not a conversion rate)."]}


async def tenders(session: AsyncSession, p: Params) -> dict[str, Any]:
    svc = AnalyticsService(session)
    filters = await resolve_filters(session, p)
    return {**_meta(p), "metrics": await _metrics(svc, TENDER_METRICS, p, filters)}


async def recurring(session: AsyncSession, p: Params) -> dict[str, Any]:
    svc = AnalyticsService(session)
    filters = await resolve_filters(session, p)
    return {**_meta(p), "metrics": await _metrics(svc, RECURRING_METRICS, p, filters),
            "not_available": [n for n in not_available() if n["metric_name"] in ("active_recurring_needs", "recurring_modification_rate")]}


async def unmatched_demand(session: AsyncSession, p: Params, *, limit: int, offset: int) -> dict[str, Any]:
    """UNMATCHED demand (requested - matched) and, separately, confirmed-but-not-received demand,
    per (sub-category, zone, canonical unit). Different problems: not enough supply found vs supply
    confirmed but not yet confirmed received."""
    svc = AnalyticsService(session)
    filters = await resolve_filters(session, p)
    result = await svc.get_unfulfilled_demand(p.start, p.end, filters=filters, group_by=("sub_category_id", "zone_id"))
    labels = await _labels(session)
    rows = []
    for r in result["rows"]:
        sub = str(r["sub_category_id"])
        cat_id = labels["sub_category_parent"].get(sub)
        requested = float(r["requested"])
        rows.append({
            "sub_category_id": sub, "sub_category": _label(labels["sub_category"], sub, "Non attribuée"),
            "category_id": cat_id, "category": _label(labels["category"], cat_id, "Non attribuée"),
            "zone_id": str(r["zone_id"]), "zone": _label(labels["zone"], r["zone_id"], "Zone non renseignée"),
            "canonical_unit": r["canonical_unit"], "measurement_family": r["measurement_family"],
            "requested": requested, "matched": float(r["matched"]), "unmatched": float(r["unmatched"]),
            "coverage": (float(r["matched"]) / requested) if requested else None,
            "confirmed": float(r["confirmed"]), "delivered": float(r["delivered"]),
            "undelivered_confirmed": float(r["undelivered_confirmed"]),
        })
    unmatched = sorted((r for r in rows if r["unmatched"] > 0), key=lambda r: (-r["unmatched"], r["sub_category"], r["zone"]))
    undelivered = sorted((r for r in rows if r["undelivered_confirmed"] > 0), key=lambda r: (-r["undelivered_confirmed"], r["sub_category"], r["zone"]))
    totals: dict[str, dict[str, float]] = {}
    for r in rows:
        t = totals.setdefault(r["canonical_unit"], {"requested": 0.0, "matched": 0.0, "unmatched": 0.0, "undelivered_confirmed": 0.0})
        for k in t:
            t[k] += r[k]
    return {
        **_meta(p),
        "unmatched": {
            "definition": "requested - matched (matching engine outcome: not enough offer found). NOT undelivered demand.",
            "total": len(unmatched), "limit": limit, "offset": offset, "rows": unmatched[offset:offset + limit],
        },
        "undelivered_confirmed": {
            "definition": "confirmed - delivered: supply confirmed but not (yet) confirmed RECEIVED by the buyer. Lower-bound signal, PARTIAL.",
            "reliability": "PARTIAL", "total": len(undelivered), "rows": undelivered[:limit],
        },
        "totals_by_unit": [{"canonical_unit": u, **v} for u, v in sorted(totals.items())],
    }


def _series_binding(metric: str, p: Params) -> None:
    if metric in UNAVAILABLE:
        raise ApiError(400, f"{metric} est indisponible : {UNAVAILABLE[metric]}")
    if metric not in SERIES_METRICS:
        raise ApiError(404, "Métrique inconnue.")
    if metric in BINDINGS and p.journey and BINDINGS[metric].journey not in ("GLOBAL", p.journey):
        raise ApiError(400, f"La métrique {metric} n'appartient pas au journey {p.journey}.")


def pick_granularity(p: Params, requested: Optional[str]) -> str:
    if requested:
        if requested not in ("day", "week", "month"):
            raise ApiError(400, "granularity invalide (day|week|month).")
        return requested
    days = (p.end - p.start).days + 1
    return "day" if days <= 45 else ("week" if days <= 180 else "month")


async def timeseries(session: AsyncSession, p: Params, metric: str, granularity: Optional[str]) -> dict[str, Any]:
    _series_binding(metric, p)
    svc = AnalyticsService(session)
    filters = await resolve_filters(session, p)
    g = pick_granularity(p, granularity)
    try:
        points = await svc.get_metric_timeseries(metric, p.start, p.end, granularity=g, filters=filters)
        unavailable_note = None
    except ValueError as exc:
        points, unavailable_note = [], f"Not available for the selected filters: {exc}"
    b = BINDINGS.get(metric)
    return {**_meta(p), "metric_name": metric, "granularity": g, "reliability": reliability_of(metric),
            "unit": (b.unit if b else ("buyers" if metric == "active_buyers" else "ratio")),
            "physical": bool(b and b.physical), "points": points, "notes": [n for n in (b.note if b else "", unavailable_note) if n]}


async def breakdown(session: AsyncSession, p: Params, metric: str, dimension: str, *, limit: int, offset: int) -> dict[str, Any]:
    _series_binding(metric, p)
    if metric in ("active_buyers", "successful_procurement_rate"):
        raise ApiError(400, f"{metric} n'a pas de ventilation par {dimension} (table par acheteur).")
    if dimension not in ("zone_id", "category_id", "sub_category_id"):
        raise ApiError(400, "dimension invalide (zone_id|category_id|sub_category_id).")
    b = BINDINGS[metric]
    if dimension not in TABLE_DIMENSIONS[b.table]:
        raise ApiError(400, f"{metric} ne peut pas être ventilée par {dimension}.")
    svc = AnalyticsService(session)
    filters = await resolve_filters(session, p)
    try:
        rows = await svc.get_metric_breakdown(metric, p.start, p.end, dimension=dimension, filters=filters)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from None
    labels = await _labels(session)
    kind = {"zone_id": "zone", "category_id": "category", "sub_category_id": "sub_category"}[dimension]
    out = []
    for r in rows:
        key = r[dimension]
        item = {**r, "id": key, "label": _label(labels[kind], key, "Non attribuée")}
        if dimension == "sub_category_id":
            parent = labels["sub_category_parent"].get(str(key))
            item["category_id"], item["category"] = parent, _label(labels["category"], parent, "Non attribuée")
        out.append(item)
    out.sort(key=lambda r: (-(r.get("denominator") or r.get("numerator") or 0), str(r["label"])))
    return {**_meta(p), "metric_name": metric, "dimension": dimension, "reliability": reliability_of(metric),
            "unit": b.unit, "physical": b.physical, "total": len(out), "limit": limit, "offset": offset,
            "rows": out[offset:offset + limit], "notes": [b.note] if b.note else []}


async def compare(session: AsyncSession, p: Params, metric: str, prev_from: Optional[date], prev_to: Optional[date]) -> dict[str, Any]:
    _series_binding(metric, p)
    length = (p.end - p.start).days + 1
    pe = prev_to or (p.start - timedelta(days=1))
    ps = prev_from or (pe - timedelta(days=length - 1))
    if ps > pe:
        raise ApiError(400, "Période précédente invalide.")
    svc = AnalyticsService(session)
    filters = await resolve_filters(session, p)
    prev = Params(ps, pe, p.zone_id, p.category_id, p.sub_category_id, p.journey)
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
    for t in ANALYTICS_TABLES:
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
    """Healthy / Warning / Stale from freshness + the existing data-quality checks (last 30 days).
    A per-table `stale_refresh` is ignored here: a table with no facts legitimately has no fresh rows —
    staleness is judged on the latest refresh across all four tables."""
    now = now or datetime.now(timezone.utc)
    fresh = await freshness(session, now=now)
    end = now.date()
    issues = [i for i in await run_data_quality_checks(session, end - timedelta(days=29), end, now=now) if i.check != "stale_refresh"]
    status = "Stale" if fresh["stale"] else ("Warning" if issues else "Healthy")
    return {"status": status, "freshness": fresh,
            "issues": [{"check": i.check, "table": i.table, "severity": i.severity, "count": i.count, "detail": i.detail} for i in issues],
            "generated_at": now.isoformat()}


__all__ = [
    "ApiError", "Params", "parse_params", "parse_paging", "overview", "direct", "tenders", "recurring",
    "unmatched_demand", "timeseries", "breakdown", "compare", "filter_options", "freshness", "health",
    "metric_payload", "safe_metric", "SERIES_METRICS",
]
