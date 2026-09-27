"""ProducerAnalyticsService — deterministic read layer over the Producer Metric
Layer (Phase C) daily aggregates.

Same discipline as `analytics_service.py::AnalyticsService`, which this module
mirrors in shape rather than inherits from (no shared base class existed before
this phase, and Phase C's own instruction is "don't force a premature
abstraction" — see `producer_metric_layer.py`'s docstring for the parts that
ARE reused directly): every rate is `SUM(numerator)/SUM(denominator)` over the
requested window, never an average of rates; a zero denominator yields
`value=None`; physical quantities are never mixed across units.

No public API / dashboard here: Phase D's endpoints will call this, not before.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ladini.domain.analytics.metric_layer import (
    Binding,
    DataStatus,
    MetricResult,
    TargetRow,
    compute_value,
    evaluate_target,
    resolve_target,
)
from ladini.domain.analytics.producer_metric_layer import (
    BINDINGS_PRODUCER,
    TABLE_DIMENSIONS_PRODUCER,
    TABLE_PRODUCER,
    TABLE_PRODUCER_SUPPLY,
    UNAVAILABLE_PRODUCER,
)

_UUID_FILTERS = ("zone_id", "category_id", "sub_category_id")
_BUCKETS = {"day": "day", "week": "week", "month": "month"}

_SPECIAL_METRICS = (
    "active_producers", "repeat_producer_rate", "time_to_first_sale",
    "available_supply", "delivered_gmv_per_active_producer",
)


def _f(value: Any) -> Optional[float]:
    return float(value) if value is not None else None


class ProducerAnalyticsService:
    def __init__(self, session: AsyncSession, *, today: Optional[date] = None) -> None:
        self.session = session
        self._today = today

    def _now(self) -> date:
        return self._today or datetime.now(timezone.utc).date()

    # ------------------------------------------------------------------ query plumbing

    @staticmethod
    def _where(table: str, filters: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        allowed = TABLE_DIMENSIONS_PRODUCER[table]
        clauses: list[str] = []
        params: dict[str, Any] = {}
        for key, value in (filters or {}).items():
            if value is None or key == "zone_scope":
                continue
            if key not in allowed:
                raise ValueError(f"Filter '{key}' is not a dimension of {table} (allowed: {allowed}).")
            if isinstance(value, (list, tuple, set, frozenset)):
                clauses.append(f"{key} = ANY(:f_{key})")
                params[f"f_{key}"] = [uuid.UUID(str(v)) if key in _UUID_FILTERS else v for v in value]
                continue
            clauses.append(f"{key} = :f_{key}")
            params[f"f_{key}"] = uuid.UUID(str(value)) if key in _UUID_FILTERS else value
        return ("".join(f" AND {c}" for c in clauses), params)

    async def _rows(
        self, binding: Binding, start: date, end: date, filters: Mapping[str, Any], group: tuple[str, ...] = (),
        bucket: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        allowed = TABLE_DIMENSIONS_PRODUCER[binding.table]
        for g in group:
            if g not in allowed:
                raise ValueError(f"Cannot group {binding.name} by '{g}' (allowed: {allowed}).")
        select = list(group)
        group_by = list(group)
        if bucket:
            trunc = f"date_trunc('{_BUCKETS[bucket]}', metric_date)::date"
            select.insert(0, f"{trunc} AS bucket")
            group_by.insert(0, "bucket")
        den = f", {binding.denominator} AS den" if binding.denominator else ", NULL AS den"
        where, params = self._where(binding.table, filters)
        sql = (
            f"SELECT {', '.join(select + [binding.numerator + ' AS num'])}{den}, count(*) AS n_rows "
            f"FROM {binding.table} WHERE metric_date >= :s AND metric_date <= :e{where}"
            + (f" GROUP BY {', '.join(group_by)} ORDER BY {', '.join(group_by)}" if group_by else "")
        )
        result = await self.session.execute(text(sql), {"s": start, "e": end, **params})
        return [dict(r) for r in result.mappings().all() if r["n_rows"]]

    async def _targets(self, metric: str) -> list[TargetRow]:
        result = await self.session.execute(
            text(
                "SELECT scope_type, scope_id, target_value, warning_threshold, critical_threshold, valid_from, valid_until "
                "FROM analytics.metric_targets WHERE metric_name = :m"
            ),
            {"m": metric},
        )
        return [
            TargetRow(
                scope_type=r["scope_type"], scope_id=str(r["scope_id"]) if r["scope_id"] else None,
                target_value=float(r["target_value"]), warning_threshold=_f(r["warning_threshold"]),
                critical_threshold=_f(r["critical_threshold"]), valid_from=r["valid_from"], valid_until=r["valid_until"],
            )
            for r in result.mappings().all()
        ]

    async def _apply_target(self, res: MetricResult, binding_journey: str, higher_is_better: bool, end: date, filters: Mapping[str, Any]) -> None:
        if res.value is None:
            return
        target = resolve_target(
            await self._targets(res.metric_name), on=end,
            zone_id=filters.get("zone_scope") or (
                filters.get("zone_id") if isinstance(filters.get("zone_id"), (str, uuid.UUID)) else None),
            category_id=filters.get("category_id"), sub_category_id=filters.get("sub_category_id"),
            journey=None if binding_journey == "GLOBAL" else binding_journey,
        )
        if target is not None:
            res.target = {
                "scope_type": target.scope_type, "scope_id": target.scope_id, "value": target.target_value,
                "warning_threshold": target.warning_threshold, "critical_threshold": target.critical_threshold,
            }
        res.target_status = evaluate_target(res.value, target, higher_is_better=higher_is_better)

    # ------------------------------------------------------------------ core API

    async def get_metric(
        self, name: str, start: date, end: date, *, filters: Optional[Mapping[str, Any]] = None, compare: bool = True,
    ) -> MetricResult:
        filters = dict(filters or {})
        period = {"start": start.isoformat(), "end": end.isoformat()}
        if name in UNAVAILABLE_PRODUCER:
            return unavailable_result_producer(name, period)
        if name == "active_producers":
            res = await self._active_producers(start, end, filters, period)
        elif name == "repeat_producer_rate":
            res = await self._repeat_producer_rate(start, end, filters, period)
        elif name == "time_to_first_sale":
            res = await self._time_to_first_sale(start, end, filters, period)
        elif name == "available_supply":
            res = await self._available_supply(filters, period)
        elif name == "delivered_gmv_per_active_producer":
            res = await self._delivered_gmv_per_active_producer(start, end, filters, period)
        elif name in BINDINGS_PRODUCER:
            res = await self._binding_metric(BINDINGS_PRODUCER[name], start, end, filters, period)
        else:
            raise KeyError(f"Unknown or unbound producer metric '{name}'.")

        if compare and res.status not in (DataStatus.UNAVAILABLE,) and name != "available_supply":
            length = (end - start).days + 1
            prev_end = start - timedelta(days=1)
            prev_start = prev_end - timedelta(days=length - 1)
            previous = await self.get_metric(name, prev_start, prev_end, filters=filters, compare=False)
            res.previous_value = previous.value
            if res.value is not None and previous.value is not None:
                res.delta = res.value - previous.value
        return res

    async def _binding_metric(self, b: Binding, start: date, end: date, filters: Mapping[str, Any], period: dict[str, str]) -> MetricResult:
        rows = await self._rows(b, start, end, filters, group=("canonical_unit",) if b.physical else ())
        notes = [b.note] if b.note else []
        res = MetricResult(metric_name=b.name, value=None, numerator=None, denominator=None, unit=b.unit,
                           status=b.status, period=period, notes=notes)
        if not rows or all(r["num"] is None and r["den"] is None for r in rows):
            res.status = DataStatus.NO_DATA
            return res
        if b.physical and len(rows) > 1:
            res.status = DataStatus.MIXED_UNITS
            res.unit = None
            res.notes.append("Several canonical units in the window: no single physical numerator/denominator; see breakdown.")
            res.breakdown = [
                {"canonical_unit": r["canonical_unit"], "numerator": _f(r["num"]), "denominator": _f(r["den"]),
                 "value": compute_value(_f(r["num"]), _f(r["den"])), "unit": r["canonical_unit"]}
                for r in rows
            ]
            return res
        row = rows[0]
        res.numerator, res.denominator = _f(row["num"]), _f(row["den"])
        if b.physical:
            res.unit = row["canonical_unit"] if not b.denominator else "ratio"
        res.value = compute_value(res.numerator, res.denominator)
        if b.denominator is not None and not res.denominator:
            res.status = DataStatus.NO_DATA
            res.value = None
        await self._apply_target(res, b.journey, b.higher_is_better, end, filters)
        return res

    async def _active_producers(self, start: date, end: date, filters: Mapping[str, Any], period: dict[str, str]) -> MetricResult:
        """A producer row only exists for a (day, producer) with >= 1 qualifying
        activity fact (see `producer_daily_aggregation.py`) — so a plain DISTINCT
        count over the window is exact, no extra WHERE needed."""
        where, params = self._where(TABLE_PRODUCER, filters)
        n = (
            await self.session.execute(
                text(f"SELECT count(DISTINCT producer_id) FROM {TABLE_PRODUCER} WHERE metric_date >= :s AND metric_date <= :e{where}"),
                {"s": start, "e": end, **params},
            )
        ).scalar()
        return MetricResult(
            "active_producers", float(n or 0), float(n or 0), None, "producers", DataStatus.OK, period,
            notes=["Distinct producers with >= 1 qualifying activity fact in the window (product published, "
                   "sellable quantity changed, bid received, order confirmed or delivered) — exact: one row per producer-day."],
        )

    async def _repeat_producer_rate(self, start: date, end: date, filters: Mapping[str, Any], period: dict[str, str]) -> MetricResult:
        where, params = self._where(TABLE_PRODUCER, filters)
        row = (
            await self.session.execute(
                text(
                    "WITH per_producer AS (SELECT producer_id, SUM(orders_delivered_direct + orders_delivered_tender + orders_delivered_recurring) AS s "
                    f"FROM {TABLE_PRODUCER} WHERE metric_date >= :s AND metric_date <= :e{where} GROUP BY producer_id) "
                    "SELECT count(*) FILTER (WHERE s >= 2) AS num, count(*) FILTER (WHERE s >= 1) AS den FROM per_producer"
                ),
                {"s": start, "e": end, **params},
            )
        ).mappings().one()
        num, den = float(row["num"]), float(row["den"])
        res = MetricResult(
            "repeat_producer_rate", compute_value(num, den), num, den, "ratio", DataStatus.PARTIAL, period,
            notes=["Producers with >= 2 delivered orders / producers with >= 1, inside the window only. "
                   "Window is PROVISIONAL (not calibrated against real cadence data yet) — segment by "
                   "category before treating this as a stable target."],
        )
        if not den:
            res.status, res.value = DataStatus.NO_DATA, None
        return res

    async def _time_to_first_sale(self, start: date, end: date, filters: Mapping[str, Any], period: dict[str, str]) -> MetricResult:
        """Duration (days) from a producer's first PRODUCT_PUBLISHED_FOR_SALE event
        (activation) to their first successful DELIVERED sale. DIRECT/TENDER use the
        business_events (producer_id-carrying since Phase B); RECURRING has no
        BusinessEvent for delivery (mark_order_delivery_status/record_order_reception
        emit none — see PRODUCER_ANALYTICS_ARCHITECTURE.md §19) so it falls back to
        the transactional RECEIVED order, using `updated_at` as an imperfect proxy
        for the reception timestamp (no dedicated column exists). PARTIAL: only
        activations observable via the event (Phase B forward) are covered; a
        producer's very first product published before this phase's deploy is invisible."""
        if filters:
            raise ValueError("time_to_first_sale does not support dimension filters (producer-level metric only).")
        start_tz = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        end_tz = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
        row = (
            await self.session.execute(
                text(
                    """
                    WITH activation AS (
                        SELECT producer_id, min(occurred_at) AS activated_at
                        FROM analytics.business_events
                        WHERE event_name = 'PRODUCT_PUBLISHED_FOR_SALE' AND producer_id IS NOT NULL
                        GROUP BY producer_id
                    ),
                    event_sale AS (
                        SELECT producer_id, min(occurred_at) AS sold_at
                        FROM analytics.business_events
                        WHERE event_name IN ('DIRECT_ORDER_DELIVERED', 'TENDER_DELIVERED') AND producer_id IS NOT NULL
                        GROUP BY producer_id
                    ),
                    recurring_sale AS (
                        SELECT p.producer_id, min(o.updated_at) AS sold_at
                        FROM marketplace.orders o
                        JOIN marketplace.order_items oi ON oi.order_id = o.id
                        JOIN marketplace.products p ON p.id = oi.product_id
                        WHERE o.order_type = 'RECURRING_SUPPLY' AND o.delivery_status = 'RECEIVED'
                        GROUP BY p.producer_id
                    ),
                    first_sale AS (
                        SELECT producer_id, min(sold_at) AS sold_at FROM (
                            SELECT * FROM event_sale UNION ALL SELECT * FROM recurring_sale
                        ) x GROUP BY producer_id
                    )
                    SELECT percentile_cont(0.5) WITHIN GROUP (
                        ORDER BY EXTRACT(EPOCH FROM (fs.sold_at - a.activated_at)) / 86400.0
                    ) AS median_days, count(*) AS n
                    FROM activation a JOIN first_sale fs ON fs.producer_id = a.producer_id AND fs.sold_at >= a.activated_at
                    WHERE a.activated_at >= :s AND a.activated_at < :e
                    """
                ),
                {"s": start_tz, "e": end_tz},
            )
        ).mappings().one()
        n = int(row["n"] or 0)
        res = MetricResult(
            "time_to_first_sale", _f(row["median_days"]), _f(row["median_days"]), float(n) if n else None,
            "days", DataStatus.PARTIAL, period,
            notes=[f"Median (p50) days from first PRODUCT_PUBLISHED_FOR_SALE to first delivered sale, over "
                   f"{n} producer(s) activated in the window. Only activations observable via the event "
                   "(Phase B forward) are covered; RECURRING delivery date is a transactional proxy "
                   "(Order.updated_at), not a dedicated timestamp."],
        )
        if not n:
            res.status, res.value = DataStatus.NO_DATA, None
        return res

    async def _available_supply(self, filters: Mapping[str, Any], period: dict[str, str]) -> MetricResult:
        """LIVE gauge only — reads the most recent `producer_supply_daily_snapshot`
        day present, never summed across days (a snapshot is not a flow)."""
        where, params = self._where(TABLE_PRODUCER_SUPPLY, filters)
        latest = (await self.session.execute(text(f"SELECT max(metric_date) FROM {TABLE_PRODUCER_SUPPLY}"))).scalar()
        if latest is None:
            return MetricResult(
                "available_supply", None, None, None, None, DataStatus.UNAVAILABLE, period,
                notes=["No supply snapshot has been generated yet (producer_supply_daily_snapshot is empty) — "
                       "run snapshot_producer_supply for today first."],
            )
        rows = (
            await self.session.execute(
                text(f"SELECT canonical_unit, SUM(available_quantity) AS q, SUM(product_count) AS n FROM {TABLE_PRODUCER_SUPPLY} "
                     f"WHERE metric_date = :d{where} GROUP BY canonical_unit"),
                {"d": latest, **params},
            )
        ).mappings().all()
        res = MetricResult(
            "available_supply", None, None, None, None, DataStatus.OK, period,
            notes=[f"Live snapshot as of {latest.isoformat()} (end of that day's `snapshot_producer_supply` run) — "
                   "a gauge, never a historical series."],
        )
        if not rows:
            res.status = DataStatus.NO_DATA
            return res
        if len(rows) > 1:
            res.status = DataStatus.MIXED_UNITS
            res.breakdown = [
                {"canonical_unit": r["canonical_unit"], "value": _f(r["q"]), "numerator": _f(r["q"]), "product_count": int(r["n"])}
                for r in rows
            ]
            res.notes.append("Several canonical units in scope: no single physical value; see breakdown.")
            return res
        res.value = _f(rows[0]["q"])
        res.numerator = res.value
        res.unit = rows[0]["canonical_unit"]
        return res

    async def _delivered_gmv_per_active_producer(self, start: date, end: date, filters: Mapping[str, Any], period: dict[str, str]) -> MetricResult:
        gmv = await self._binding_metric(BINDINGS_PRODUCER["producer_delivered_gmv"], start, end, filters, period)
        active = await self._active_producers(start, end, filters, period)
        value = compute_value(gmv.value, active.value if active.value else None)
        return MetricResult(
            "delivered_gmv_per_active_producer", value, gmv.value, active.value, "FCFA",
            DataStatus.NO_DATA if not active.value else DataStatus.OK, period,
            notes=["SUM(producer_delivered_gmv) / COUNT(DISTINCT active producers), same window. "
                   "Null (never 0) when there are no active producers in the window."],
        )

    # ------------------------------------------------------------------ views

    async def get_producer_overview(self, start: date, end: date, *, filters: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        names = ("active_producers", "producer_order_fulfillment_rate", "producer_quantity_fulfillment_rate",
                 "producer_delivered_gmv", "delivered_gmv_per_active_producer", "repeat_producer_rate")
        return {n: (await self.get_metric(n, start, end, filters=filters)).as_dict() for n in names}

    async def get_producer_fulfillment(self, start: date, end: date, *, filters: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        names = ("producer_order_fulfillment_rate", "producer_quantity_fulfillment_rate")
        return {n: (await self.get_metric(n, start, end, filters=filters)).as_dict() for n in names}

    async def get_producer_gmv(self, start: date, end: date, *, filters: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        names = ("producer_delivered_gmv", "delivered_gmv_per_active_producer", "producer_paid_gmv")
        return {n: (await self.get_metric(n, start, end, filters=filters)).as_dict() for n in names}

    async def get_repeat_producers(self, start: date, end: date, *, filters: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        return {"repeat_producer_rate": (await self.get_metric("repeat_producer_rate", start, end, filters=filters)).as_dict()}

    async def get_available_supply(self, *, filters: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        today = self._now()
        result = await self.get_metric("available_supply", today, today, filters=filters, compare=False)
        out: dict[str, Any] = result.as_dict()
        return out

    async def get_time_to_first_sale(self, start: date, end: date, *, filters: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        result = await self.get_metric("time_to_first_sale", start, end, filters=filters, compare=False)
        out: dict[str, Any] = result.as_dict()
        return out

    async def get_metric_timeseries(
        self, name: str, start: date, end: date, *, granularity: str = "day", filters: Optional[Mapping[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        if granularity not in _BUCKETS:
            raise ValueError(f"granularity must be one of {tuple(_BUCKETS)}")
        if name == "active_producers":
            return await self._active_producers_timeseries(start, end, granularity, filters or {})
        b = self._binding_or_raise(name)
        rows = await self._rows(b, start, end, filters or {}, group=("canonical_unit",) if b.physical else (), bucket=granularity)
        return [
            {"bucket": r["bucket"].isoformat(), **({"canonical_unit": r["canonical_unit"]} if b.physical else {}),
             "numerator": _f(r["num"]), "denominator": _f(r["den"]), "value": compute_value(_f(r["num"]), _f(r["den"]))}
            for r in rows
        ]

    async def _active_producers_timeseries(self, start: date, end: date, granularity: str, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
        where, params = self._where(TABLE_PRODUCER, filters)
        trunc = f"date_trunc('{_BUCKETS[granularity]}', metric_date)::date"
        sql = (f"SELECT {trunc} AS bucket, count(DISTINCT producer_id) AS num, NULL AS den FROM {TABLE_PRODUCER} "
               f"WHERE metric_date >= :s AND metric_date <= :e{where} GROUP BY bucket ORDER BY bucket")
        result = await self.session.execute(text(sql), {"s": start, "e": end, **params})
        return [{"bucket": r["bucket"].isoformat(), "numerator": _f(r["num"]), "denominator": None, "value": _f(r["num"])}
                for r in result.mappings().all()]

    async def get_metric_breakdown(
        self, name: str, start: date, end: date, *, dimension: str, filters: Optional[Mapping[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        b = self._binding_or_raise(name)
        group = (dimension,) if not b.physical or dimension == "canonical_unit" else (dimension, "canonical_unit")
        rows = await self._rows(b, start, end, filters or {}, group=group)
        out = []
        for r in rows:
            d = {k: (str(r[k]) if isinstance(r[k], uuid.UUID) else r[k]) for k in group}
            d.update({"numerator": _f(r["num"]), "denominator": _f(r["den"]), "value": compute_value(_f(r["num"]), _f(r["den"]))})
            out.append(d)
        return out

    async def compare_periods(
        self, name: str, current: tuple[date, date], previous: tuple[date, date], *, filters: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        cur = await self.get_metric(name, current[0], current[1], filters=filters, compare=False)
        prev = await self.get_metric(name, previous[0], previous[1], filters=filters, compare=False)
        delta = cur.value - prev.value if cur.value is not None and prev.value is not None else None
        return {"metric_name": name, "current": cur.as_dict(), "previous": prev.as_dict(), "delta": delta}

    @staticmethod
    def _binding_or_raise(name: str) -> Binding:
        if name in UNAVAILABLE_PRODUCER:
            raise ValueError(f"{name} is UNAVAILABLE: {UNAVAILABLE_PRODUCER[name]}")
        if name not in BINDINGS_PRODUCER:
            raise ValueError(f"'{name}' has no timeseries/breakdown binding (single-table producer metrics only).")
        return BINDINGS_PRODUCER[name]


def unavailable_result_producer(name: str, period: dict[str, str]) -> MetricResult:
    return MetricResult(metric_name=name, value=None, numerator=None, denominator=None, unit=None,
                        status=DataStatus.UNAVAILABLE, period=period, notes=[UNAVAILABLE_PRODUCER[name]])


__all__ = ["ProducerAnalyticsService"]
