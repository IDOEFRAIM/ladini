"""MarketBalanceService — deterministic read layer over `market_balance_daily_snapshot`
(Phase E). Mirrors the discipline of `analytics_service.py`/`producer_analytics_service.py`:
every rate is computed on the fly, never stored; a zero denominator yields `value=None`; physical
quantities are only ever combined inside one canonical unit. See `docs/analytics/MARKET_BALANCE.md`
for the semantic audit and formulas this implements.

Row contract (§21): zone, category, subcategory, canonical_unit, open_demand_quantity,
available_supply_quantity, potential_coverable_quantity, potential_coverage_rate,
demand_gap_quantity, excess_supply_quantity, demand_reliability, supply_reliability,
reliable_scope, as_of, notes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

TABLE = "analytics.market_balance_daily_snapshot"
_DIMENSIONS = ("zone_scope", "category_id", "sub_category_id", "canonical_unit")
_UUID_FILTERS = ("zone_scope", "category_id", "sub_category_id")

#: Static reliability classification of the demand SIDE, keyed by `demand_scope`
#: (docs/analytics/MARKET_BALANCE.md §3/§11) — RECURRING is the reliable_scope baseline; any TENDER
#: contribution carries the "closed auction never reopens on cancellation" caveat (§3.2).
DEMAND_RELIABILITY = {"RECURRING": "RELIABLE", "TENDER": "PARTIAL", "RECURRING+TENDER": "PARTIAL"}
#: `available_supply` is always a RELIABLE snapshot (same classification as Producer Analytics'
#: own `available_supply`) — never dependent on which demand journeys happened to contribute.
SUPPLY_RELIABILITY = "RELIABLE"


def _f(value: Any) -> Optional[float]:
    return float(value) if value is not None else None


def _coverage(coverable: Optional[float], open_demand: Optional[float]) -> Optional[float]:
    if not open_demand:
        return None
    return coverable / open_demand if coverable is not None else None


@dataclass(frozen=True)
class BalanceRow:
    zone_scope: str
    category_id: str
    sub_category_id: str
    canonical_unit: str
    open_demand_quantity: float
    available_supply_quantity: float
    potential_coverable_quantity: float
    demand_gap_quantity: float
    excess_supply_quantity: float
    demand_scope: str
    as_of: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "zone": self.zone_scope, "category": self.category_id, "subcategory": self.sub_category_id,
            "canonical_unit": self.canonical_unit,
            "open_demand_quantity": self.open_demand_quantity, "available_supply_quantity": self.available_supply_quantity,
            "potential_coverable_quantity": self.potential_coverable_quantity,
            "potential_coverage_rate": _coverage(self.potential_coverable_quantity, self.open_demand_quantity),
            "demand_gap_quantity": self.demand_gap_quantity, "excess_supply_quantity": self.excess_supply_quantity,
            "demand_reliability": DEMAND_RELIABILITY.get(self.demand_scope, "PARTIAL"),
            "supply_reliability": SUPPLY_RELIABILITY,
            "reliable_scope": self.demand_scope,
            "as_of": self.as_of,
            "notes": [] if self.demand_scope == "RECURRING" else [
                "Includes TENDER demand: a cancelled-after-award auction's demand does not reappear here (docs/analytics/MARKET_BALANCE.md §3.2)."
            ],
        }


class MarketBalanceService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ query plumbing

    @staticmethod
    def _where(filters: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        clauses: list[str] = []
        params: dict[str, Any] = {}
        for key, value in (filters or {}).items():
            if value is None:
                continue
            if key not in _DIMENSIONS:
                raise ValueError(f"Filter '{key}' is not a Market Balance dimension (allowed: {_DIMENSIONS}).")
            if isinstance(value, (list, tuple, set, frozenset)):
                clauses.append(f"{key} = ANY(:f_{key})")
                params[f"f_{key}"] = [uuid.UUID(str(v)) if key in _UUID_FILTERS else v for v in value]
                continue
            clauses.append(f"{key} = :f_{key}")
            params[f"f_{key}"] = uuid.UUID(str(value)) if key in _UUID_FILTERS else value
        return ("".join(f" AND {c}" for c in clauses), params)

    async def _latest_day(self) -> Optional[date]:
        return (await self.session.execute(text(f"SELECT max(snapshot_day) FROM {TABLE}"))).scalar()

    async def _rows(self, day: date, filters: Mapping[str, Any]) -> list[BalanceRow]:
        where, params = self._where(filters)
        sql = (
            f"SELECT zone_scope, category_id, sub_category_id, canonical_unit, demand_scope, "
            f"open_demand_quantity, available_supply_quantity, potential_coverable_quantity, "
            f"demand_gap_quantity, excess_supply_quantity FROM {TABLE} WHERE snapshot_day = :d{where}"
        )
        result = (await self.session.execute(text(sql), {"d": day, **params})).mappings().all()
        as_of = day.isoformat()
        return [
            BalanceRow(
                zone_scope=str(r["zone_scope"]), category_id=str(r["category_id"]), sub_category_id=str(r["sub_category_id"]),
                canonical_unit=r["canonical_unit"], open_demand_quantity=_f(r["open_demand_quantity"]) or 0.0,
                available_supply_quantity=_f(r["available_supply_quantity"]) or 0.0,
                potential_coverable_quantity=_f(r["potential_coverable_quantity"]) or 0.0,
                demand_gap_quantity=_f(r["demand_gap_quantity"]) or 0.0, excess_supply_quantity=_f(r["excess_supply_quantity"]) or 0.0,
                demand_scope=r["demand_scope"], as_of=as_of,
            )
            for r in result
        ]

    # ------------------------------------------------------------------ views

    async def get_current_balance(self, *, filters: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        day = await self._latest_day()
        if day is None:
            return {"status": "UNAVAILABLE", "as_of": None, "rows": [],
                    "notes": ["No Market Balance snapshot has been generated yet — run the daily refresh first."]}
        rows = await self._rows(day, filters or {})
        return {"status": "OK", "as_of": day.isoformat(), "rows": [r.as_dict() for r in rows]}

    async def get_demand_gaps(self, *, filters: Optional[Mapping[str, Any]] = None, limit: int = 50) -> dict[str, Any]:
        """Grouped by canonical unit (§26): never a single ranking mixing incompatible units."""
        balance = await self.get_current_balance(filters=filters)
        if balance["status"] != "OK":
            return {**balance, "groups": []}
        by_unit: dict[str, list[dict[str, Any]]] = {}
        for r in balance["rows"]:
            if r["demand_gap_quantity"] > 0:
                by_unit.setdefault(r["canonical_unit"], []).append(r)
        groups = [
            {"canonical_unit": u, "rows": sorted(rs, key=lambda r: -r["demand_gap_quantity"])[:limit]}
            for u, rs in sorted(by_unit.items())
        ]
        return {"status": "OK", "as_of": balance["as_of"], "groups": groups}

    async def get_excess_supply(self, *, filters: Optional[Mapping[str, Any]] = None, limit: int = 50) -> dict[str, Any]:
        """Grouped by canonical unit (§27), same rule as `get_demand_gaps`."""
        balance = await self.get_current_balance(filters=filters)
        if balance["status"] != "OK":
            return {**balance, "groups": []}
        by_unit: dict[str, list[dict[str, Any]]] = {}
        for r in balance["rows"]:
            if r["excess_supply_quantity"] > 0:
                by_unit.setdefault(r["canonical_unit"], []).append(r)
        groups = [
            {"canonical_unit": u, "rows": sorted(rs, key=lambda r: -r["excess_supply_quantity"])[:limit]}
            for u, rs in sorted(by_unit.items())
        ]
        return {"status": "OK", "as_of": balance["as_of"], "groups": groups}

    async def get_balance_breakdown(self, dimension: str, *, filters: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        if dimension not in ("zone_scope", "category_id", "sub_category_id"):
            raise ValueError("dimension must be one of zone_scope, category_id, sub_category_id.")
        day = await self._latest_day()
        if day is None:
            return {"status": "UNAVAILABLE", "as_of": None, "rows": []}
        where, params = self._where(filters or {})
        sql = (
            f"SELECT {dimension}, canonical_unit, "
            f"SUM(open_demand_quantity) AS open_demand_quantity, SUM(available_supply_quantity) AS available_supply_quantity, "
            f"SUM(potential_coverable_quantity) AS potential_coverable_quantity, SUM(demand_gap_quantity) AS demand_gap_quantity, "
            f"SUM(excess_supply_quantity) AS excess_supply_quantity "
            f"FROM {TABLE} WHERE snapshot_day = :d{where} GROUP BY {dimension}, canonical_unit ORDER BY {dimension}, canonical_unit"
        )
        result = (await self.session.execute(text(sql), {"d": day, **params})).mappings().all()
        rows = []
        for r in result:
            open_d, coverable = _f(r["open_demand_quantity"]), _f(r["potential_coverable_quantity"])
            rows.append({
                "id": str(r[dimension]), "canonical_unit": r["canonical_unit"],
                "open_demand_quantity": open_d, "available_supply_quantity": _f(r["available_supply_quantity"]),
                "potential_coverable_quantity": coverable, "potential_coverage_rate": _coverage(coverable, open_d),
                "demand_gap_quantity": _f(r["demand_gap_quantity"]), "excess_supply_quantity": _f(r["excess_supply_quantity"]),
            })
        return {"status": "OK", "as_of": day.isoformat(), "dimension": dimension, "rows": rows}

    async def get_balance_overview(self, *, filters: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        """Totals per canonical unit only (§24) — never a single fabricated cross-unit total."""
        balance = await self.get_current_balance(filters=filters)
        if balance["status"] != "OK":
            return balance
        totals: dict[str, dict[str, float]] = {}
        scopes: dict[str, set[str]] = {}
        for r in balance["rows"]:
            u = r["canonical_unit"]
            t = totals.setdefault(u, {"open_demand_quantity": 0.0, "available_supply_quantity": 0.0,
                                       "potential_coverable_quantity": 0.0, "demand_gap_quantity": 0.0, "excess_supply_quantity": 0.0})
            for k in t:
                t[k] += r[k]
            scopes.setdefault(u, set()).add(r["reliable_scope"])
        by_unit = [
            {"canonical_unit": u, **t, "potential_coverage_rate": _coverage(t["potential_coverable_quantity"], t["open_demand_quantity"]),
             "reliable_scope": sorted(scopes[u])}
            for u, t in sorted(totals.items())
        ]
        return {"status": "OK", "as_of": balance["as_of"], "by_unit": by_unit}

    async def get_balance_timeseries(self, start: date, end: date, *, filters: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        """Only as many points as real snapshots exist — `FROM_PHASE_E_ONLY` (§16/§18), never a
        fabricated point for a day before this phase's first real run."""
        where, params = self._where(filters or {})
        sql = (
            f"SELECT snapshot_day, canonical_unit, SUM(open_demand_quantity) AS open_demand_quantity, "
            f"SUM(available_supply_quantity) AS available_supply_quantity, SUM(potential_coverable_quantity) AS potential_coverable_quantity, "
            f"SUM(demand_gap_quantity) AS demand_gap_quantity, SUM(excess_supply_quantity) AS excess_supply_quantity "
            f"FROM {TABLE} WHERE snapshot_day >= :s AND snapshot_day <= :e{where} GROUP BY snapshot_day, canonical_unit ORDER BY snapshot_day, canonical_unit"
        )
        result = (await self.session.execute(text(sql), {"s": start, "e": end, **params})).mappings().all()
        points = [
            {"snapshot_day": r["snapshot_day"].isoformat(), "canonical_unit": r["canonical_unit"],
             "open_demand_quantity": _f(r["open_demand_quantity"]), "available_supply_quantity": _f(r["available_supply_quantity"]),
             "potential_coverable_quantity": _f(r["potential_coverable_quantity"]),
             "potential_coverage_rate": _coverage(_f(r["potential_coverable_quantity"]), _f(r["open_demand_quantity"])),
             "demand_gap_quantity": _f(r["demand_gap_quantity"]), "excess_supply_quantity": _f(r["excess_supply_quantity"])}
            for r in result
        ]
        return {"period": {"start": start.isoformat(), "end": end.isoformat()}, "points": points,
                "notes": ["History only exists from this phase's own snapshots forward — no past day was backfilled (docs/analytics/MARKET_BALANCE.md §18)."]}

    async def compare_balance_periods(self, day_a: date, day_b: date, *, filters: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        """Only meaningful once both snapshot days actually exist — never interpolated or assumed."""
        rows_a = await self._rows(day_a, filters or {})
        rows_b = await self._rows(day_b, filters or {})
        if not rows_a or not rows_b:
            missing = [d.isoformat() for d, r in ((day_a, rows_a), (day_b, rows_b)) if not r]
            return {"status": "UNAVAILABLE", "notes": [f"No snapshot for: {', '.join(missing)}."]}
        return {"status": "OK", "day_a": {"as_of": day_a.isoformat(), "rows": [r.as_dict() for r in rows_a]},
                "day_b": {"as_of": day_b.isoformat(), "rows": [r.as_dict() for r in rows_b]}}

    # ------------------------------------------------------------------ health

    async def freshness(self, *, now: Optional[datetime] = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        last = (await self.session.execute(text(f"SELECT max(computed_at) FROM {TABLE}"))).scalar()
        age = (now - last) if last is not None else None
        stale_after = timedelta(hours=36)
        stale = age is None or age > stale_after
        return {"last_refresh": last.isoformat() if last else None, "age_seconds": int(age.total_seconds()) if age is not None else None,
                "stale": stale, "stale_after_hours": int(stale_after.total_seconds() // 3600)}


__all__ = ["MarketBalanceService", "BalanceRow", "DEMAND_RELIABILITY", "SUPPLY_RELIABILITY"]
