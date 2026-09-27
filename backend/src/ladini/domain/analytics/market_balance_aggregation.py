"""Pure aggregation of Market Balance (Phase E) "fact rows" into the
`market_balance_daily_snapshot` rows.

No SQL and no I/O here: `services/analytics/market_balance_refresh.py` fetches
the current RECURRING/TENDER demand facts and the latest producer supply
snapshot rows, and hands them to `aggregate_market_balance`. Reuses
`resolve_canonical`/`to_canonical_quantity`/`measurement_family_of` exactly
as the buyer/producer layers do — no new unit-conversion logic.

See `docs/analytics/MARKET_BALANCE.md` for the semantic audit this
implements (§3-§12 in particular): `current_open_demand` per journey,
`demand_scope` as part of the grain, and the exact formulas below.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Optional

from ladini.domain.analytics.daily_aggregation import (
    NIL_UUID,
    resolve_canonical,
    to_canonical_quantity,
)
from ladini.domain.analytics.units import measurement_family_of

_ZERO = Decimal("0")


def _id(value: Any) -> str:
    return str(value) if value else NIL_UUID


def _dec(value: Any) -> Decimal:
    if value is None:
        return _ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


# ---------------------------------------------------------------------------
# Fact rows (what the refresher fetches)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecurringDemandFact:
    """One still-actionable occurrence (`status IN ('OPEN','MATCHED')`,
    `docs/analytics/MARKET_BALANCE.md` §3.3). `open_quantity` is already the
    caller's `requested_quantity - quantity_confirmed` — this module only
    canonicalizes and aggregates it, never recomputes the subtraction (that
    stays in the refresher, next to the SQL that reads both columns)."""

    zone_id: Any
    category_id: Any
    sub_category_id: Any
    unit: str
    priority_unit: Optional[str]
    open_quantity: Any


@dataclass(frozen=True)
class TenderDemandFact:
    """One still-open auction (`status='OPEN' AND deadline > now`) — the full
    `Auction.quantity` is open demand: a winning bid always covers it entirely
    (no partial-quantity concept exists on `Bid`, §3.2)."""

    zone_id: Any
    category_id: Any
    sub_category_id: Any
    unit: str
    priority_unit: Optional[str]
    quantity: Any


@dataclass(frozen=True)
class SupplyFact:
    """One row of the latest `producer_supply_daily_snapshot` day, already
    canonicalized upstream (Producer Analytics Phase C) — aggregated here
    across producers into one market-level figure per cell."""

    zone_id: Any
    category_id: Any
    sub_category_id: Any
    canonical_unit: str
    available_quantity: Any


Cell = tuple[str, str, str, str]


def aggregate_market_balance(
    snapshot_day: date,
    recurring_facts: Iterable[RecurringDemandFact],
    tender_facts: Iterable[TenderDemandFact],
    supply_facts: Iterable[SupplyFact],
) -> list[dict[str, Any]]:
    """One row per `(zone_scope, category_id, sub_category_id, canonical_unit)` cell that has
    at least some demand or some supply (§15: no row is fabricated for a cell with neither —
    "no observable activity" is the absence of a row, not a stored 0/0)."""
    demand: dict[Cell, dict[str, Decimal]] = {}

    def _add_demand(zone: Any, category: Any, sub: Any, unit: str, priority_unit: Optional[str], qty: Decimal, journey: str) -> None:
        if qty <= 0:
            return
        canonical = resolve_canonical(unit, priority_unit)
        key = (_id(zone), _id(category), _id(sub), canonical)
        row = demand.setdefault(key, {"RECURRING": _ZERO, "TENDER": _ZERO})
        row[journey] += to_canonical_quantity(qty, unit, canonical)

    for rf in recurring_facts:
        _add_demand(rf.zone_id, rf.category_id, rf.sub_category_id, rf.unit, rf.priority_unit, _dec(rf.open_quantity), "RECURRING")
    for tf in tender_facts:
        _add_demand(tf.zone_id, tf.category_id, tf.sub_category_id, tf.unit, tf.priority_unit, _dec(tf.quantity), "TENDER")

    supply: dict[Cell, Decimal] = {}
    for s in supply_facts:
        key = (_id(s.zone_id), _id(s.category_id), _id(s.sub_category_id), s.canonical_unit)
        supply[key] = supply.get(key, _ZERO) + _dec(s.available_quantity)

    rows: list[dict[str, Any]] = []
    for key in sorted(set(demand) | set(supply)):
        zone, category, sub, canonical = key
        d = demand.get(key, {"RECURRING": _ZERO, "TENDER": _ZERO})
        recurring_qty, tender_qty = d["RECURRING"], d["TENDER"]
        open_demand = recurring_qty + tender_qty
        available = supply.get(key, _ZERO)
        if recurring_qty > 0 and tender_qty > 0:
            scope = "RECURRING+TENDER"
        elif tender_qty > 0:
            scope = "TENDER"
        else:
            scope = "RECURRING"
        rows.append({
            "snapshot_day": snapshot_day, "zone_scope": zone, "category_id": category, "sub_category_id": sub,
            "canonical_unit": canonical, "measurement_family": measurement_family_of(canonical), "demand_scope": scope,
            "open_demand_quantity": open_demand, "available_supply_quantity": available,
            "potential_coverable_quantity": min(open_demand, available),
            "demand_gap_quantity": max(open_demand - available, _ZERO),
            "excess_supply_quantity": max(available - open_demand, _ZERO),
        })
    return rows


__all__ = [
    "RecurringDemandFact", "TenderDemandFact", "SupplyFact", "aggregate_market_balance",
]
