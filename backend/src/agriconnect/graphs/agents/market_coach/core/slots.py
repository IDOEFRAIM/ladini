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
        SLOT_REGISTRY,
    )
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional, Tuple


# ---------------------------------------------------------------------------
# Slot Definition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SlotDefinition:
    """Immutable descriptor for a single conversational slot.

    Attributes:
        canonical:   The one canonical key name used throughout the codebase.
        aliases:     All accepted synonyms / legacy names that map to canonical.
        value_type:  Expected Python type tag: "str" | "float" | "int" | "bool".
        blocking:    True if the slot MUST be answered before the tunnel can
                     be interrupted (CONFIRMATION, OTP…). False = soft slot.
        label_fr:    Human-readable French label used in prompt hints.
        example_fr:  Short example value shown in prompts to reduce "ok/merci" loops.
    """
    canonical: str
    aliases: FrozenSet[str]
    value_type: str
    blocking: bool = False
    label_fr: str = ""
    example_fr: str = ""


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
            "original_quantity_mentioned",
        }),
        value_type="float",
        blocking=False,
        label_fr="quantité",
        example_fr="50, 100, 200…",
    ),
    SlotDefinition(
        canonical="unit",
        aliases=frozenset({
            "unit_mentioned", "unite", "original_unit", "original_unit_mentioned",
        }),
        value_type="str",
        blocking=False,
        label_fr="unité",
        example_fr="KG, SAC, TONNE…",
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
    # Hard-blocking slot (used in OTP / final confirmation contexts only).
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

# alias (lower-stripped) → canonical
_ALIAS_TO_CANONICAL: Dict[str, str] = {}
for _slot in SLOT_REGISTRY:
    _ALIAS_TO_CANONICAL[_slot.canonical] = _slot.canonical
    for _alias in _slot.aliases:
        _ALIAS_TO_CANONICAL[_alias.lower().strip()] = _slot.canonical

# canonical → frozenset of aliases
_CANONICAL_TO_ALIASES: Dict[str, FrozenSet[str]] = {
    _slot.canonical: _slot.aliases for _slot in SLOT_REGISTRY
}

# canonical → SlotDefinition
_CANONICAL_TO_DEF: Dict[str, SlotDefinition] = {
    _slot.canonical: _slot for _slot in SLOT_REGISTRY
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
    # "CONFIRMATION" is always blocking regardless of slot registry.
    if str(expected_input).upper() == "CONFIRMATION":
        return True
    # Check via the registry (e.g. OTP_CODE).
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


def build_remap_dict() -> Dict[str, str]:
    """Generates the complete alias→canonical mapping dict.

    Drop-in replacement for ``_ENTITY_KEY_REMAP`` in ``interpreter/routing.py``.
    """
    return dict(_ALIAS_TO_CANONICAL)


def build_alias_mirrors() -> Dict[str, Tuple[str, ...]]:
    """Generates canonical→aliases mapping used by ``memory_update``.

    Drop-in replacement for ``_ALIAS_MIRRORS`` in ``nodes/memory.py``.
    Only includes entries that have at least one alias.
    """
    return {
        canonical: tuple(aliases)
        for canonical, aliases in _CANONICAL_TO_ALIASES.items()
        if aliases
    }


def build_canonical_field_aliases() -> Dict[str, str]:
    """Generates alias→canonical mapping for INTENT_CONFIG canonicalization.

    Drop-in replacement for ``_CANONICAL_FIELD_ALIASES`` in ``interpreter/intent.py``.
    """
    return dict(_ALIAS_TO_CANONICAL)


__all__ = [
    "SlotDefinition",
    "SLOT_REGISTRY",
    "resolve_canonical",
    "get_aliases",
    "get_slot",
    "is_blocking_slot",
    "get_slot_hint",
    "build_remap_dict",
    "build_alias_mirrors",
    "build_canonical_field_aliases",
]
