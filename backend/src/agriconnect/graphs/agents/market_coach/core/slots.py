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
        SLOT_FILLING_INPUTS,
        SLOT_REGISTRY,
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional, Tuple

from agriconnect.domain.quantity_unit import default_unit_for_product

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
    #: Défaut CALCULÉ à partir du payload déjà collecté, quand une constante
    #: ne peut pas être honnête. Prioritaire sur `default_value`.
    #: Motivation (incident réel 2026-09-08) : le slot `unit` portait
    #: `default_value="KG"` — une affirmation posée SANS AUCUNE PREUVE, qui
    #: gagnait ensuite définitivement contre la nature réelle du produit
    #: (« Vente de 6500 KG de poulets » : un poulet se compte à la tête).
    #: Un défaut qui dépend d'un autre slot doit être calculé, jamais figé.
    default_factory: Optional[Any] = field(default=None, hash=False, compare=False)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SLOT_REGISTRY: Tuple[SlotDefinition, ...] = (
    SlotDefinition(
        canonical="product",
        aliases=frozenset(
            {
                "product_name",
                "produit",
                "name",
                "item_name",
                "commodity",
                "culture",
            }
        ),
        value_type="str",
        blocking=False,
        label_fr="produit/culture",
        example_fr="Maïs, Riz, Oignons…",
    ),
    SlotDefinition(
        canonical="quantity",
        aliases=frozenset(
            {
                "quantity_mentioned",
                "quantite",
                "qty",
                "volume",
                "quantity_kg",
                "quantity_for_sale",
                "original_quantity",
            }
        ),
        value_type="float",
        blocking=False,
        label_fr="quantité",
        example_fr="50, 100, 200…",
    ),
    SlotDefinition(
        canonical="unit",
        aliases=frozenset(
            {
                "unit_mentioned",
                "unite",
                "original_unit",
            }
        ),
        value_type="str",
        blocking=False,
        label_fr="unité",
        example_fr="KG, SAC, TONNE…",
        # `default_value="KG"` (aveugle) remplacé le 2026-09-08 : voir
        # `default_factory` ci-dessus. `default_unit_for_product` renvoie
        # exactement "KG" pour une culture — comportement inchangé — et
        # "TETE" pour un animal d'élevage, qui ne se pèse pas.
        default_factory=lambda payload: default_unit_for_product(
            payload.get("product")
        ),
    ),
    SlotDefinition(
        canonical="price",
        aliases=frozenset(
            {
                "price_mentioned",
                "prix",
                "montant",
                "montant_enchere",
                "offered_price",
                "max_price",
                "prix_unitaire",
            }
        ),
        value_type="float",
        blocking=False,
        label_fr="prix",
        example_fr="250 FCFA/KG, 500…",
    ),
    SlotDefinition(
        canonical="zone",
        aliases=frozenset(
            {
                "zone_name",
                "region",
                "localite",
                "target_zone",
                "location",
            }
        ),
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
    SlotDefinition(
        canonical="farm_name",
        aliases=frozenset({"domain_name", "nom_domaine", "farm", "exploitation"}),
        value_type="str",
        blocking=False,
        label_fr="nom du domaine agricole",
        example_fr="Ferme du Soleil, Jardin d'Abondance…",
    ),
    # Date limite d'un appel d'offres (PROCUREMENT_CREATE_REQUEST). OPTIONNEL :
    # non listé dans INTENT_CONFIG.required et `procure_dto.deadline` est
    # Optional (le domaine applique un défaut/auto_extend si absent). Enregistré
    # ici uniquement pour être capté/normalisé s'il est mentionné, jamais forcé.
    SlotDefinition(
        canonical="deadline",
        aliases=frozenset({"end_date", "expiry", "echeance", "date_limite"}),
        value_type="str",
        blocking=False,
        label_fr="date limite",
        example_fr="30 septembre, dans 2 semaines…",
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
    "farm_name": "FARM_NAME",
    "deadline": "DATE",
    # `movement_type` (IN/OUT) est un vrai slot du registre, requis par
    # STOCK_RECORD_MOVEMENT — il manquait ici, donc l'agent le demandait avec
    # `expected_input=NONE` : aucun indice de slot pour l'interpréteur et slot
    # non ré-interrogeable. Détecté par tests/architecture.
    "movement_type": "MOVEMENT_TYPE",
    # (2026-09-09, audit Bloc 2, Blocker D) : `DECLARE_CROP_CYCLE` le requiert
    # (voir interpreter/intent.py) et `domain/agro.py`/rendering le
    # consomment déjà — absent d'ici, il retombait sur `expected_input=NONE`
    # ET, plus grave, était absent de `nodes/memory.py::
    # _EXPECTED_INPUT_ALLOWED_FIELDS["DATE"]` alors qu'il y était pourtant
    # référencé À LA MAIN (double dérive) : la table dérivée
    # (`fields_for_expected_input`, plus bas) le couvre maintenant par
    # construction, plus de copie manuelle qui peut décrocher.
    "expected_harvest_date": "DATE",
}

# ---------------------------------------------------------------------------
# Reverse lookup — quels CHAMPS sont légitimement attendus pour une catégorie
# `expected_input` donnée (2026-09-09, audit Bloc 2, Blocker D). Dérivé de
# `_EXPECTED_INPUT_MAP` ci-dessus (même source, sens inverse) — évite que
# `nodes/memory.py::_EXPECTED_INPUT_ALLOWED_FIELDS` maintienne sa propre
# copie manuelle susceptible de dériver (c'était le cas : `production_type`
# et `surface` mappent bien vers PRODUCT/QUANTITY ci-dessus mais étaient
# absents de l'allowlist à la main dans memory.py — une réponse dégradée au
# slot production_type/surface EXACTEMENT demandé se faisait donc
# silencieusement jeter par son propre garde-fou anti-hallucination).
_EXPECTED_INPUT_TO_FIELDS: Dict[str, FrozenSet[str]] = {}
for _field_name, _category in _EXPECTED_INPUT_MAP.items():
    _EXPECTED_INPUT_TO_FIELDS[_category] = _EXPECTED_INPUT_TO_FIELDS.get(
        _category, frozenset()
    ) | {_field_name}
del _field_name, _category


def fields_for_expected_input(category: str) -> FrozenSet[str]:
    """Réciproque de `expected_input_for_field` : l'ensemble des champs
    canoniques légitimement répondus quand cette catégorie `expected_input`
    est celle actuellement en attente. Source unique — voir
    `_EXPECTED_INPUT_MAP` ci-dessus."""
    return _EXPECTED_INPUT_TO_FIELDS.get(str(category or "").upper().strip(), frozenset())

# Ensemble CANONIQUE des `expected_input` qui représentent un CHAMP MÉTIER à
# collecter auprès de l'utilisateur (« soft slots » : on peut y re-demander le
# champ, et une nouvelle intention suffisamment confiante peut les interrompre).
# Dérivé automatiquement de `_EXPECTED_INPUT_MAP` : ajouter un slot au registre
# l'inclut PARTOUT sans édition manuelle. Historiquement, ce set était recopié
# à la main dans tunnel_manager (SOFT_EXPECTED_INPUTS), interpreter/routing.py
# (garde NEW_TASK→ANSWER) et interpreter/strategy.py (re-demande de slot) — les
# 3 copies avaient DÉRIVÉ (FARM_NAME absent des 3, DATE absent de strategy),
# donc la gestion de changement d'intention était incohérente selon le slot en
# cours. Source unique désormais.
SLOT_FILLING_INPUTS: FrozenSet[str] = frozenset(_EXPECTED_INPUT_MAP.values())


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
    "SLOT_REGISTRY",
    "SLOT_FILLING_INPUTS",
    "resolve_canonical",
    "get_aliases",
    "get_slot",
    "is_blocking_slot",
    "get_slot_hint",
    "expected_input_for_field",
    "fields_for_expected_input",
    "field_priority",
    "build_remap_dict",
    "build_alias_mirrors",
    "build_canonical_field_aliases",
]
