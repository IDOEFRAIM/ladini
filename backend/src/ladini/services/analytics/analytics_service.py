"""AnalyticsService — deterministic read layer over the Phase D daily aggregates.

Reads the metric bindings (`domain/analytics/metric_layer.py`), the daily tables and
`analytics.metric_targets`; never touches transactional tables. Every rate is
`SUM(numerator)/SUM(denominator)` over the requested window (never an average of
rates); a zero denominator yields `value=None`; physical quantities are never mixed
across units (several units => `MIXED_UNITS` + per-unit breakdown, no global value).

No public API / dashboard here: this is the layer Phase E's endpoints will call.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ladini.domain.analytics.metric_layer import (
    BINDINGS,
    TABLE_BUYER,
    TABLE_DIMENSIONS,
    TABLE_RECURRING,
    UNAVAILABLE,
    Binding,
    DataStatus,
    MetricResult,
    TargetRow,
    compute_value,
    evaluate_target,
    resolve_target,
    unavailable_result,
)

#: A need cohort younger than this may still be awaiting delivery/receipt; rates that depend on
#: delivery read artificially low until it matures.
MATURITY_DAYS = 7

_UUID_FILTERS = ("zone_id", "category_id", "sub_category_id")
_BUCKETS = {"day": "day", "week": "week", "month": "month"}  # date_trunc units (constants only)

_BUYER_METRICS = ("active_buyers", "repeat_buyer_rate")


def _f(value: Any) -> Optional[float]:
    return float(value) if value is not None else None


class AnalyticsService:
    def __init__(self, session: AsyncSession, *, today: Optional[date] = None) -> None:
        self.session = session
        self._today = today

    def _now(self) -> date:
        return self._today or datetime.now(timezone.utc).date()

    # ------------------------------------------------------------------ query plumbing

    @staticmethod
    def _where(table: str, filters: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        allowed = TABLE_DIMENSIONS[table]
        clauses, params = [], {}
        for key, value in (filters or {}).items():
            if value is None:
                continue
            if key not in allowed:
                raise ValueError(f"Filter '{key}' is not a dimension of {table} (allowed: {allowed}).")
            clauses.append(f"{key} = :f_{key}")
            params[f"f_{key}"] = uuid.UUID(str(value)) if key in _UUID_FILTERS else value
        return ("".join(f" AND {c}" for c in clauses), params)

    async def _rows(
        self, binding: Binding, start: date, end: date, filters: Mapping[str, Any], group: tuple[str, ...] = (),
        bucket: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        allowed = TABLE_DIMENSIONS[binding.table]
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
            await self._targets(res.metric_name), on=end, zone_id=filters.get("zone_id"),
            category_id=filters.get("category_id"), sub_category_id=filters.get("sub_category_id"),
            journey=None if binding_journey == "GLOBAL" else binding_journey,
        )
        if target is not None:
            res.target = {
                "scope_type": target.scope_type, "scope_id": target.scope_id, "value": target.target_value,
                "warning_threshold": target.warning_threshold, "critical_threshold": target.critical_threshold,
            }
        res.target_status = evaluate_target(res.value, target, higher_is_better=higher_is_better)

    def _maturity_note(self, end: date) -> list[str]:
        if end >= self._now() - timedelta(days=MATURITY_DAYS):
            return [f"Window includes cohorts younger than {MATURITY_DAYS} days: delivery-dependent numerators may still grow."]
        return []

    # ------------------------------------------------------------------ core API

    async def get_metric(
        self, name: str, start: date, end: date, *, filters: Optional[Mapping[str, Any]] = None, compare: bool = True,
    ) -> MetricResult:
        filters = dict(filters or {})
        period = {"start": start.isoformat(), "end": end.isoformat()}
        if name in UNAVAILABLE:
            return unavailable_result(name, period)
        if name in _BUYER_METRICS:
            res = await self._buyer_metric(name, start, end, filters, period)
        elif name == "successful_procurement_rate":
            res = await self._spr(start, end, filters, period)
        elif name in BINDINGS:
            res = await self._binding_metric(BINDINGS[name], start, end, filters, period)
        else:
            raise KeyError(f"Unknown or unbound metric '{name}'.")

        if compare and res.status not in (DataStatus.UNAVAILABLE,):
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

    async def _spr(self, start: date, end: date, filters: Mapping[str, Any], period: dict[str, str]) -> MetricResult:
        b = BINDINGS["successful_procurement_rate"]
        where, params = self._where(TABLE_BUYER, filters)
        row = (
            await self.session.execute(
                text(
                    "SELECT COALESCE(SUM(needs_direct),0) nd, COALESCE(SUM(needs_tender),0) nt, COALESCE(SUM(needs_recurring),0) nr, "
                    "COALESCE(SUM(satisfied_direct),0) sd, COALESCE(SUM(satisfied_tender),0) st, COALESCE(SUM(satisfied_recurring),0) sr "
                    f"FROM {TABLE_BUYER} WHERE metric_date >= :s AND metric_date <= :e{where}"
                ),
                {"s": start, "e": end, **params},
            )
        ).mappings().one()
        nd, nt, nr, sd, st, sr = (float(row[k]) for k in ("nd", "nt", "nr", "sd", "st", "sr"))
        needs, satisfied = nd + nt + nr, sd + st + sr
        res = MetricResult(
            metric_name=b.name, value=compute_value(satisfied, needs), numerator=satisfied, denominator=needs,
            unit="ratio", status=DataStatus.PARTIAL, period=period, notes=[b.note] + self._maturity_note(end),
        )
        if not needs:
            res.status, res.value = DataStatus.NO_DATA, None
        reliable_needs, reliable_sat = nd + nt, sd + st
        res.breakdown = [
            {"journey": "DIRECT", "numerator": sd, "denominator": nd, "value": compute_value(sd, nd), "reliability": "RELIABLE"},
            {"journey": "TENDER", "numerator": st, "denominator": nt, "value": compute_value(st, nt), "reliability": "RELIABLE"},
            {"journey": "RECURRING", "numerator": sr, "denominator": nr, "value": compute_value(sr, nr), "reliability": "PARTIAL"},
            {"journey": "DIRECT+TENDER (reliable_scope)", "numerator": reliable_sat, "denominator": reliable_needs,
             "value": compute_value(reliable_sat, reliable_needs), "reliability": "RELIABLE"},
        ]
        await self._apply_target(res, "GLOBAL", True, end, filters)
        return res

    async def _buyer_metric(self, name: str, start: date, end: date, filters: Mapping[str, Any], period: dict[str, str]) -> MetricResult:
        where, params = self._where(TABLE_BUYER, filters)
        p = {"s": start, "e": end, **params}
        if name == "active_buyers":
            n = (
                await self.session.execute(
                    text(
                        f"SELECT count(DISTINCT buyer_id) FROM {TABLE_BUYER} WHERE metric_date >= :s AND metric_date <= :e"
                        f" AND (needs_direct + needs_tender + needs_recurring) > 0{where}"
                    ),
                    p,
                )
            ).scalar()
            res = MetricResult(name, float(n or 0), float(n or 0), None, "buyers", DataStatus.OK, period,
                               notes=["Distinct buyers with >= 1 need instance in the window (exact: one aggregate row per buyer-day)."])
            return res
        row = (
            await self.session.execute(
                text(
                    "WITH per_buyer AS (SELECT buyer_id, SUM(satisfied_direct + satisfied_tender + satisfied_recurring) AS s "
                    f"FROM {TABLE_BUYER} WHERE metric_date >= :s AND metric_date <= :e{where} GROUP BY buyer_id) "
                    "SELECT count(*) FILTER (WHERE s >= 2) AS num, count(*) FILTER (WHERE s >= 1) AS den FROM per_buyer"
                ),
                p,
            )
        ).mappings().one()
        num, den = float(row["num"]), float(row["den"])
        res = MetricResult(name, compute_value(num, den), num, den, "ratio", DataStatus.PARTIAL, period,
                           notes=["Buyers with >= 2 satisfied needs / buyers with >= 1, inside the window only. Satisfied includes RECURRING receipts (buyer-confirmed, lower bound)."])
        if not den:
            res.status, res.value = DataStatus.NO_DATA, None
        return res

    # ------------------------------------------------------------------ views

    async def get_buyer_overview(self, start: date, end: date, *, filters: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        names = ("active_buyers", "needs_created", "successful_procurement_rate", "repeat_buyer_rate",
                 "potential_gmv", "confirmed_gmv", "delivered_gmv", "fulfillment_rate")
        return {n: (await self.get_metric(n, start, end, filters=filters)).as_dict() for n in names}

    async def _many(self, names: tuple[str, ...], start: date, end: date, filters: Optional[Mapping[str, Any]]) -> dict[str, Any]:
        return {n: (await self.get_metric(n, start, end, filters=filters)).as_dict() for n in names}

    async def get_direct_metrics(self, start: date, end: date, *, filters: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        return await self._many(("direct_searches", "direct_search_success_rate", "direct_orders_per_search", "direct_orders_created",
                                 "direct_orders_confirmed", "direct_order_delivery_rate", "direct_fulfillment_rate", "direct_gmv"), start, end, filters)

    async def get_tender_metrics(self, start: date, end: date, *, filters: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        return await self._many(("tenders_created", "tender_response_rate", "average_bids_per_tender", "time_to_first_bid",
                                 "tender_winner_rate", "tender_fulfillment_rate", "tender_gmv"), start, end, filters)

    async def get_recurring_metrics(self, start: date, end: date, *, filters: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        return await self._many(("recurring_requested_quantity", "recurring_matched_quantity", "recurring_unmatched_quantity",
                                 "recurring_coverage_rate", "recurring_full_coverage_rate", "recurring_acceptance_rate",
                                 "recurring_skip_rate", "recurring_received_occurrence_rate", "recurring_fulfillment_rate",
                                 "recurring_delivered_quantity", "recurring_gmv"), start, end, filters)

    async def get_unfulfilled_demand(
        self, start: date, end: date, *, filters: Optional[Mapping[str, Any]] = None, group_by: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """RECURRING UNMATCHED demand (requested - matched), per canonical unit. It is what the
        matching engine could not cover — NOT demand that was matched but never delivered
        (`undelivered_confirmed` = confirmed - delivered is reported separately and never called unmatched)."""
        allowed = TABLE_DIMENSIONS[TABLE_RECURRING]
        for g in group_by:
            if g not in allowed or g == "canonical_unit":
                raise ValueError(f"Cannot group unfulfilled demand by '{g}'.")
        where, params = self._where(TABLE_RECURRING, filters or {})
        cols = ", ".join(list(group_by) + ["canonical_unit", "measurement_family"])
        sql = (
            f"SELECT {cols}, SUM(requested_quantity) AS requested, SUM(matched_quantity) AS matched, "
            f"SUM(unmatched_quantity) AS unmatched, SUM(confirmed_quantity) AS confirmed, "
            f"SUM(delivered_quantity) AS delivered FROM {TABLE_RECURRING} "
            f"WHERE metric_date >= :s AND metric_date <= :e{where} GROUP BY {cols} ORDER BY {cols}"
        )
        rows = (await self.session.execute(text(sql), {"s": start, "e": end, **params})).mappings().all()
        out = []
        for r in rows:
            d = {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in dict(r).items()}
            d["requested"], d["matched"], d["unmatched"] = float(r["requested"]), float(r["matched"]), float(r["unmatched"])
            d["unmatched_ratio"] = compute_value(d["unmatched"], d["requested"])
            confirmed, delivered = float(r["confirmed"]), float(r["delivered"])
            d["confirmed"], d["delivered"] = confirmed, delivered
            d["undelivered_confirmed"] = max(confirmed - delivered, 0.0)
            out.append(d)
        return {
            "kind": "UNMATCHED_DEMAND",
            "definition": "SUM(GREATEST(requested - matched, 0)) over active recurring occurrences, per canonical unit.",
            "undelivered_confirmed": "confirmed - delivered per row: confirmed supply the buyer has not (yet) confirmed RECEIVED (lower-bound signal; not the same as unmatched).",
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "rows": out,
        }

    async def get_metric_timeseries(
        self, name: str, start: date, end: date, *, granularity: str = "day", filters: Optional[Mapping[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        if granularity not in _BUCKETS:
            raise ValueError(f"granularity must be one of {tuple(_BUCKETS)}")
        b = self._binding_or_raise(name)
        rows = await self._rows(b, start, end, filters or {}, group=("canonical_unit",) if b.physical else (), bucket=granularity)
        return [
            {"bucket": r["bucket"].isoformat(), **({"canonical_unit": r["canonical_unit"]} if b.physical else {}),
             "numerator": _f(r["num"]), "denominator": _f(r["den"]), "value": compute_value(_f(r["num"]), _f(r["den"]))}
            for r in rows
        ]

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
        if name in UNAVAILABLE:
            raise ValueError(f"{name} is UNAVAILABLE: {UNAVAILABLE[name]}")
        if name not in BINDINGS or name == "successful_procurement_rate":
            raise ValueError(f"'{name}' has no timeseries/breakdown binding (single-table metrics only).")
        return BINDINGS[name]


__all__ = ["AnalyticsService", "MATURITY_DAYS"]
