"""Canonical unit / measurement-family model for analytics — single source of
truth for "can these two quantities be safely added together?".

This module deliberately does NOT introduce a new unit vocabulary. It is a
thin classification layer on top of two already-existing, already-tested
sources:

- ``domain.quantity_unit`` — the repo-wide unit *registry* (``VALID_UNITS``):
  the enumerable set of units an admin can configure as a SubCategory's
  ``priority_unit``/``allowed_units`` (KG, TONNE, SAC, PANIER, TETE, UNITE,
  LITRE). This module never adds a unit to that set.
- ``domain.pricing_tiers`` — the repo's most complete mass/volume conversion
  table (``unit_family``/``unit_factor``, includes G/GRAMME, which
  ``quantity_unit.py``'s narrower ``_UNIT_TO_KG`` does not). Reused as-is,
  not re-copied, per the Phase B audit finding that pricing_tiers.py was
  already the more complete of the two.

``canonical_unit`` itself is NOT a new column: ``SubCategory.priority_unit``
already plays that role (see the architecture doc, section "Canonical unit
source of truth" for the audit that led to this decision) — this module
reads it, never duplicates it.
"""

from __future__ import annotations

from typing import Optional

from ladini.domain.pricing_tiers import unit_factor, unit_family
from ladini.domain.quantity_unit import VALID_UNITS, convert_quantity, normalize_unit

#: Coarse, business-facing grouping on top of `pricing_tiers.unit_family`'s
#: finer MASS/VOLUME/<self> scheme. Only covers `VALID_UNITS` — the
#: enumerable set an admin can actually configure. Deliberately does NOT
#: merge SAC/PANIER into TETE/UNITE's COUNT family: a "sac" has no
#: universal fixed size (25kg? 50kg? — set per product, see
#: `pricing_tiers.py::PricingTier.base_unit_quantity`), so grouping it with
#: a literal head-count would silently license summing incommensurable
#: things. TETE/UNITE are true atomic counts (1 head = 1 head, 1 piece = 1
#: piece) — see mission "Ne force pas SAC/PANIER dans COUNT si leur taille
#: varie."
MeasurementFamily = str  # "MASS" | "VOLUME" | "COUNT" | "PACKAGE" | "OTHER"

_FAMILY_OVERRIDES: dict[str, MeasurementFamily] = {
    "TETE": "COUNT",
    "UNITE": "COUNT",
    "SAC": "PACKAGE",
    "PANIER": "PACKAGE",
}


def measurement_family_of(unit: Optional[str]) -> MeasurementFamily:
    """Classify *unit* (already canonical, e.g. from ``normalize_unit``) into
    one of MASS / VOLUME / COUNT / PACKAGE / OTHER.

    Never raises. An unrecognized or missing unit is ``OTHER`` — analytics
    code must treat ``OTHER`` as "cannot aggregate, needs a decision", never
    silently sum it with anything.
    """
    if not unit:
        return "OTHER"
    fine = str(unit_family(unit))  # "MASS" | "VOLUME" | <unit itself, uppercased>
    if fine in ("MASS", "VOLUME"):
        return fine
    return _FAMILY_OVERRIDES.get(fine, "OTHER")


def units_are_aggregation_compatible(unit_a: Optional[str], unit_b: Optional[str]) -> bool:
    """True iff quantities in *unit_a* and *unit_b* can be summed into one
    number without lying about what it means.

    Rule (mission "RÈGLE CRITIQUE D'AGRÉGATION"): identical unit, always; a
    genuinely convertible pair (MASS: KG/TONNE/G..., VOLUME: L/LITRE...),
    yes; anything else (different families, or same singleton family but
    different literal units — e.g. two different COUNT units), no. Family
    membership alone is NOT sufficient for COUNT/PACKAGE: those are
    singleton families by construction (`measurement_family_of` maps each
    distinct unit in them individually), so this reduces correctly to
    "only the identical unit" for that case without a separate branch.
    """
    if not unit_a or not unit_b:
        return False
    if unit_a == unit_b:
        return True
    fam_a, fam_b = measurement_family_of(unit_a), measurement_family_of(unit_b)
    if fam_a != fam_b or fam_a == "OTHER":
        return False
    return fam_a in ("MASS", "VOLUME")


def convert_to_canonical(quantity: float, unit: str, canonical_unit: str) -> Optional[float]:
    """Convert *quantity* (in *unit*) into *canonical_unit*, or ``None`` when
    the pair isn't aggregation-compatible (caller must not guess further).

    Uses `pricing_tiers.unit_factor` (covers G, unlike
    `quantity_unit.convert_quantity`) so "1000 G + 2 KG -> 3 KG" (mission
    example) actually works for analytics, without changing what the
    conversational parser recognizes as a typed-in unit.
    """
    if not units_are_aggregation_compatible(unit, canonical_unit):
        return None
    return float(quantity) * float(unit_factor(unit)) / float(unit_factor(canonical_unit))


def resolve_subcategory_canonical_unit(sub_category: object) -> Optional[str]:
    """The one place analytics code reads a SubCategory's canonical unit.

    Returns ``sub_category.priority_unit`` verbatim (already canonical per
    `resolve_product_unit`'s own invariant — the DB config is normalised at
    write time, not read time) — deliberately NOT a fallback guess (no
    "most common unit ever sold", no default KG): a SubCategory with no
    ``priority_unit`` configured is analytics-`OTHER`/not-yet-comparable
    for canonical-unit-keyed aggregation, and that must stay visible to the
    caller rather than be silently papered over.
    """
    priority_unit = getattr(sub_category, "priority_unit", None)
    if not priority_unit:
        return None
    return normalize_unit(priority_unit) or str(priority_unit).strip().upper() or None


def is_recognized_unit(unit: Optional[str]) -> bool:
    """Whether *unit* belongs to the admin-configurable unit registry
    (`VALID_UNITS`). Used by data-quality checks — a `business_events` row
    whose `unit` isn't in this set is a red flag, not a silent pass."""
    return bool(unit) and unit in VALID_UNITS


__all__ = [
    "MeasurementFamily",
    "measurement_family_of",
    "units_are_aggregation_compatible",
    "convert_to_canonical",
    "resolve_subcategory_canonical_unit",
    "is_recognized_unit",
    "convert_quantity",  # re-exported for callers who only need domain.analytics
]
