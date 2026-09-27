"""``analytics.metric_targets`` — DESIGN CONTRACT ONLY, per the mission's own
Phase B scope ("préparer... les metric targets", not build the dashboard
that reads them, and no new migration is added in this phase).

This is a plain, inert dataclass describing the row shape a future
``analytics.metric_targets`` table (Phase C) will have — nothing here reads
or writes a database. Kept alongside the metric dictionary so Phase C does
not have to re-derive the design: the shape below is final, not a sketch.

Why no migration yet: targets are configuration, and configuring them
sensibly needs the metric dictionary (this module's sibling) and at least
one real aggregate to compare against — building the table before either
exists would just invite hardcoded, unvalidated numbers. See the
architecture doc's gate criteria.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional

from ladini.domain.analytics.metric_dictionary import get_metric


class TargetScopeType(str, Enum):
    GLOBAL = "GLOBAL"
    JOURNEY = "JOURNEY"
    CATEGORY = "CATEGORY"
    SUBCATEGORY = "SUBCATEGORY"
    ZONE = "ZONE"


@dataclass(frozen=True)
class MetricTarget:
    """One row of the future ``analytics.metric_targets`` table.

    ``scope_id`` is ``None`` for GLOBAL, a journey name for JOURNEY, and a
    UUID (as text) for CATEGORY/SUBCATEGORY/ZONE — never a hardcoded product
    name (mission: "pas de hardcode produit").
    """

    metric_name: str
    scope_type: TargetScopeType
    scope_id: Optional[str]
    target_value: float
    warning_threshold: Optional[float]
    critical_threshold: Optional[float]
    valid_from: date
    valid_until: Optional[date]

    def __post_init__(self) -> None:
        # Fails loudly at construction time rather than admitting a target
        # for a metric name that does not exist in the dictionary — targets
        # must never be free-floating strings a frontend just trusts.
        get_metric(self.metric_name)
        if self.scope_type == TargetScopeType.GLOBAL and self.scope_id is not None:
            raise ValueError("GLOBAL targets must not carry a scope_id.")
        if self.scope_type != TargetScopeType.GLOBAL and self.scope_id is None:
            raise ValueError(f"{self.scope_type} targets require a scope_id.")


# Illustrative examples from the mission brief — NOT seeded into any table,
# just proof the shape holds together (see tests/unit/test_analytics_metric_targets.py).
EXAMPLE_TARGETS = (
    MetricTarget(
        metric_name="successful_procurement_rate",
        scope_type=TargetScopeType.GLOBAL,
        scope_id=None,
        target_value=0.50,
        warning_threshold=0.40,
        critical_threshold=0.30,
        valid_from=date(2026, 1, 1),
        valid_until=None,
    ),
    MetricTarget(
        metric_name="recurring_coverage_rate",
        scope_type=TargetScopeType.CATEGORY,
        scope_id="00000000-0000-0000-0000-000000000001",  # placeholder: "Légumes" category id
        target_value=0.75,
        warning_threshold=0.60,
        critical_threshold=0.45,
        valid_from=date(2026, 1, 1),
        valid_until=None,
    ),
)


__all__ = ["TargetScopeType", "MetricTarget", "EXAMPLE_TARGETS"]
