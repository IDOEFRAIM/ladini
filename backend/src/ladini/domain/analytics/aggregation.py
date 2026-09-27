"""Aggregation primitives shared by every ratio-shaped metric in the
dictionary (`metric_dictionary.py`) — the two rules the mission is most
explicit about:

1. a rate is never the average of child rates; it is the sum of numerators
   over the sum of denominators (mission: "820/1100, PAS (80+20)/2").
2. physical quantities are only ever summed when
   `analytics.units.units_are_aggregation_compatible` says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from ladini.domain.analytics.units import (
    convert_to_canonical,
    units_are_aggregation_compatible,
)


@dataclass(frozen=True)
class WeightedRate:
    """A ratio computed from summed parts, never from averaged rates."""

    numerator: float
    denominator: float

    @property
    def value(self) -> Optional[float]:
        """``None`` (not 0.0 or 1.0) when the denominator is zero — a rate
        with no expressed needs is undefined, not "0% coverage"."""
        if self.denominator == 0:
            return None
        return self.numerator / self.denominator


def weighted_rate(parts: Iterable[tuple[float, float]]) -> WeightedRate:
    """Combine per-group (numerator, denominator) pairs into ONE rate.

    ``weighted_rate([(800, 1000), (20, 100)]).value == 820/1100`` — never
    ``(0.8 + 0.2) / 2``. This is the only correct way to "roll up" a rate
    across categories/sub_categories/zones; never average `WeightedRate.value`
    across groups.
    """
    num = 0.0
    den = 0.0
    for n, d in parts:
        num += n
        den += d
    return WeightedRate(numerator=num, denominator=den)


@dataclass(frozen=True)
class QuantityAggregationResult:
    total: Optional[float]
    canonical_unit: Optional[str]
    #: (quantity, unit) pairs that could NOT be folded into `total` because
    #: they weren't compatible with `canonical_unit` — never silently
    #: dropped, always surfaced so a caller can decide (separate row,
    #: error, manual review).
    rejected: tuple[tuple[float, str], ...]


def aggregate_compatible_quantities(
    quantities: Sequence[tuple[float, str]], *, canonical_unit: str
) -> QuantityAggregationResult:
    """Sum every (quantity, unit) pair that is aggregation-compatible with
    *canonical_unit*, converting as needed (mission: "1000 G + 2 KG -> 3 KG").

    Pairs whose unit is incompatible (mission: "KG + L -> jamais additionnés")
    are never added in — they come back in `.rejected` instead of being
    dropped or coerced.
    """
    total = 0.0
    rejected: list[tuple[float, str]] = []
    any_included = False
    for qty, unit in quantities:
        converted = convert_to_canonical(qty, unit, canonical_unit)
        if converted is None:
            rejected.append((qty, unit))
            continue
        total += converted
        any_included = True
    return QuantityAggregationResult(
        total=total if any_included else None,
        canonical_unit=canonical_unit,
        rejected=tuple(rejected),
    )


def assert_compatible_or_raise(unit_a: str, unit_b: str) -> None:
    """Guard for call sites that must fail loudly rather than silently skip
    (mission test D: "MASS + VOLUME -> erreur/segmentation explicite")."""
    if not units_are_aggregation_compatible(unit_a, unit_b):
        raise IncompatibleUnitsError(unit_a, unit_b)


class IncompatibleUnitsError(ValueError):
    def __init__(self, unit_a: str, unit_b: str) -> None:
        self.unit_a = unit_a
        self.unit_b = unit_b
        super().__init__(
            f"Cannot aggregate incompatible units: '{unit_a}' and '{unit_b}'."
        )


__all__ = [
    "WeightedRate",
    "weighted_rate",
    "QuantityAggregationResult",
    "aggregate_compatible_quantities",
    "assert_compatible_or_raise",
    "IncompatibleUnitsError",
]
