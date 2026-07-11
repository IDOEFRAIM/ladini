"""Market Coach — Central Slot Registry.

Single source of truth for all canonical slot keys, their accepted aliases,
expected value types, and normalizer names.  Every module that remaps or
validates slot keys (routing.py, memory.py, validation.py, intent.py) now
consumes this registry instead of maintaining independent dictionaries.

Usage
-----
    from agriconnect.graphs.agents.market_coach.core.slots import (
        resolve_canonical,
        build_remap_dict,
        build_alias_mirrors,
        build_canonical_field_aliases,
        compute_slot_status,
        SLOT_REGISTRY,
    )
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Slot Definition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SlotDefinition:
    """Immutable descriptor for a single conversational slot."""
    canonical: str
    aliases: FrozenSet[str]
    value_type: str
    blocking: bool = False
    label_fr: str = ""
    example_fr: str = ""
    auto_resolvable: bool = False
    default_value: Optional[Any] = field(default=None, hash=False, compare=False)


# ---------------------------------------------------------------------------
# Slot Status (runtime, per-turn)
# ---------------------------------------------------------------------------

@dataclass
class SlotStatus:
    """Runtime status of a single slot for the current turn."""
    name: str
    required: bool
    value: Any
    filled: bool
    valid: bool
    error: Optional[str] = None
    source: str = "unknown"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SLOT_REGISTRY: Tuple[SlotDefinition, ...] = (
    SlotDefinition(
        canonical="product",
        aliases=frozenset({
            "product_name", "produit", "name", "item_name", "commodity",
            "culture",
        }),
        value_type="str",
        blocking=False,
        label_fr="produit/culture",
        example_fr="Maïs, Riz, Oignons…",
    ),
    SlotDefinition(
        canonical="quantity",
        aliases=frozenset({
            "quantity_mentioned", "quantite", "qty", "volume",
            "quantity_kg", "quantity_for_sale", "original_quantity",
        }),
        value_type="float",
        blocking=False,
        label_fr="quantité",
        example_fr="50, 100, 200…",
    ),
    SlotDefinition(
        canonical="unit",
        aliases=frozenset({
            "unit_mentioned", "unite", "original_unit",
        }),
        value_type="str",
        blocking=False,
        label_fr="unité",
        example_fr="KG, SAC, TONNE…",
        default_value="KG",
    ),
    SlotDefinition(
        canonical="price",
        aliases=frozenset({
            "price_mentioned", "prix", "montant", "montant_enchere",
            "offered_price", "max_price", "prix_unitaire",
        }),
        value_type="float",
        blocking=False,
        label_fr="prix",
        example_fr="250 FCFA/KG, 500…",
    ),
    SlotDefinition(
        canonical="zone",
        aliases=frozenset({
            "zone_name", "region", "localite", "target_zone", "location",
        }),
        value_type="str",
        blocking=False,
        label_fr="zone géographique",
        example_fr="Bobo-Dioulasso, Ouagadougou…",
        auto_resolvable=True,
    ),
    SlotDefinition(
        canonical="selection_index",
        aliases=frozenset(),
        value_type="int",
        blocking=False,
        label_fr="choix (numéro)",
        example_fr="1, 2, 3…",
    ),
    SlotDefinition(
        canonical="selected_value",
        aliases=frozenset(),
        value_type="str",
        blocking=False,
        label_fr="choix (texte)",
        example_fr="Ferme Diallo, Option A…",
    ),
    SlotDefinition(
        canonical="movement_type",
        aliases=frozenset(),
        value_type="str",
        blocking=False,
        label_fr="sens du mouvement",
        example_fr="IN (entrée) ou OUT (sortie)",
    ),
    SlotDefinition(
        canonical="reason",
        aliases=frozenset(),
        value_type="str",
        blocking=False,
        label_fr="motif",
        example_fr="Vente directe, Perte sèche…",
    ),
    SlotDefinition(
        canonical="otp_code",
        aliases=frozenset({"otp", "code", "code_confirmation"}),
        value_type="str",
        blocking=True,
        label_fr="code de confirmation",
        example_fr="123456",
    ),
)


# ---------------------------------------------------------------------------
# Derived lookup tables (built once at import time)
# ---------------------------------------------------------------------------

_ALIAS_TO_CANONICAL: Dict[str, str] = {}
for _slot in SLOT_REGISTRY:
    _ALIAS_TO_CANONICAL[_slot.canonical] = _slot.canonical
    for _alias in _slot.aliases:
        _ALIAS_TO_CANONICAL[_alias.lower().strip()] = _slot.canonical

_CANONICAL_TO_ALIASES: Dict[str, FrozenSet[str]] = {
    _slot.canonical: _slot.aliases for _slot in SLOT_REGISTRY
}

_CANONICAL_TO_DEF: Dict[str, SlotDefinition] = {
    _slot.canonical: _slot for _slot in SLOT_REGISTRY
}

_FIELD_PRIORITY: Dict[str, int] = {
    "product": 0,
    "quantity": 1,
    "price": 2,
    "unit": 3,
    "zone": 4,
    "farm_id": 5,
    "stock_id": 5,
    "auction_id": 5,
    "bid_id": 5,
    "cycle_id": 5,
    "movement_type": 6,
    "intervention_type": 6,
}

_EXPECTED_INPUT_MAP: Dict[str, str] = {
    "product": "PRODUCT",
    "price": "PRICE",
    "quantity": "QUANTITY",
    "unit": "UNIT",
    "zone": "LOCATION",
    "surface": "QUANTITY",
    "production_type": "PRODUCT",
    "estimated_available_at": "DATE",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_canonical(key: str) -> str:
    """Returns the canonical key for any alias; returns key unchanged if unknown."""
    return _ALIAS_TO_CANONICAL.get(str(key).lower().strip(), key)


def get_aliases(canonical: str) -> FrozenSet[str]:
    """Returns all known aliases for a canonical key (empty frozenset if unknown)."""
    return _CANONICAL_TO_ALIASES.get(canonical, frozenset())


def get_slot(canonical: str) -> Optional[SlotDefinition]:
    """Returns the SlotDefinition for a canonical key, or None if unknown."""
    return _CANONICAL_TO_DEF.get(canonical)


def is_blocking_slot(expected_input: str) -> bool:
    """Returns True if the expected_input maps to a hard-blocking slot."""
    if str(expected_input).upper() == "CONFIRMATION":
        return True
    canonical = resolve_canonical(str(expected_input).lower())
    slot = _CANONICAL_TO_DEF.get(canonical)
    return bool(slot and slot.blocking)


def get_slot_hint(canonical: str) -> str:
    """Returns a human-readable hint string for a missing slot (used in prompts)."""
    slot = _CANONICAL_TO_DEF.get(canonical)
    if not slot:
        return canonical
    parts = [slot.label_fr]
    if slot.example_fr:
        parts.append(f"(ex : {slot.example_fr})")
    return " ".join(parts)


def expected_input_for_field(field_name: str) -> str:
    """Maps a canonical field name to its expected_input token."""
    return _EXPECTED_INPUT_MAP.get(field_name, "NONE")


def field_priority(field_name: str) -> int:
    """Returns the priority ordering for a field (lower = ask first)."""
    return _FIELD_PRIORITY.get(field_name, 99)


def _slot_value_filled(value: Any) -> bool:
    return value not in (None, "", [], {}, 0)


def compute_slot_status(
    payload: Dict[str, Any],
    required_fields: List[str],
    *,
    goal: Optional[str] = None,
) -> Dict[str, SlotStatus]:
    """Compute SlotStatus for each required field given a payload.

    Returns a dict keyed by canonical field name. Fields are sorted by
    priority so callers can iterate in ask-order.
    """
    result: Dict[str, SlotStatus] = {}
    sorted_fields = sorted(required_fields, key=lambda f: _FIELD_PRIORITY.get(f, 99))
    for field_name in sorted_fields:
        slot_def = _CANONICAL_TO_DEF.get(field_name)
        value = payload.get(field_name)
        filled = _slot_value_filled(value)

        if not filled and slot_def and slot_def.auto_resolvable:
            filled = True
            source = "auto"
        elif not filled and slot_def and slot_def.default_value is not None:
            source = "default"
        elif filled:
            source = "user"
        else:
            source = "missing"

        error = None
        valid = True
        if filled and slot_def:
            if slot_def.value_type == "float" and value is not None:
                try:
                    fv = float(value)
                    if fv <= 0:
                        valid = False
                        error = f"{slot_def.label_fr} doit être supérieur(e) à 0."
                except (TypeError, ValueError):
                    valid = False
                    error = f"{slot_def.label_fr} n'est pas un nombre valide."

        result[field_name] = SlotStatus(
            name=field_name,
            required=True,
            value=value,
            filled=filled,
            valid=valid,
            error=error,
            source=source,
        )
    return result


def missing_from_status(slot_status: Dict[str, SlotStatus]) -> List[str]:
    """Returns ordered list of missing field names from a SlotStatus dict."""
    return [s.name for s in slot_status.values() if not s.filled]


def errors_from_status(slot_status: Dict[str, SlotStatus]) -> List[str]:
    """Returns list of validation error messages from a SlotStatus dict."""
    return [s.error for s in slot_status.values() if s.error]


def build_remap_dict() -> Dict[str, str]:
    """Generates the complete alias→canonical mapping dict."""
    return dict(_ALIAS_TO_CANONICAL)


def build_alias_mirrors() -> Dict[str, Tuple[str, ...]]:
    """Generates canonical→aliases mapping used by ``memory_update``."""
    return {
        canonical: tuple(aliases)
        for canonical, aliases in _CANONICAL_TO_ALIASES.items()
        if aliases
    }


def build_canonical_field_aliases() -> Dict[str, str]:
    """Generates alias→canonical mapping for INTENT_CONFIG canonicalization."""
    return dict(_ALIAS_TO_CANONICAL)


__all__ = [
    "SlotDefinition",
    "SlotStatus",
    "SLOT_REGISTRY",
    "resolve_canonical",
    "get_aliases",
    "get_slot",
    "is_blocking_slot",
    "get_slot_hint",
    "expected_input_for_field",
    "field_priority",
    "compute_slot_status",
    "missing_from_status",
    "errors_from_status",
    "build_remap_dict",
    "build_alias_mirrors",
    "build_canonical_field_aliases",
]
