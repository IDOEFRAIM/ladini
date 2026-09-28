"""Invariant de domaine : produit / sous-catégorie ↔ famille de mesure ↔ unité autorisée.

## Incident réel (2026-09-28)

Une offre `boeufs — price=461000 — quantity=461000 — unit=UNITE` a atteint la base : la
conversation avait pourtant résolu `TETE` (bovins) — mais `services/database/producer.py::
create_product` recopiait `unit.upper()` SANS aucun contrôle contre la nature du produit.
La taxonomie n'était qu'un *conseil* de la couche conversationnelle
(`quantity_unit.resolve_product_unit`), jamais un invariant que la couche service impose :
n'importe quel appelant (autre canal, futur outil MCP, script) pouvait écrire une unité
incompatible.

## Règle (générique par taxonomie, aucun nom de produit codé en dur ici)

1. **Config admin de la sous-catégorie** (`allowed_units`/`priority_unit`, quand elle existe
   en base — voir `BaseMixin.get_product_category_unit_config`) : l'unité doit appartenir à
   `allowed_units`, sinon REJET (jamais de conversion silencieuse : 500 KG ≠ 500 têtes).
2. **Repli taxonomique** (config absente, cas actuel : les colonnes n'existent pas encore) :
   un produit d'ÉLEVAGE (`quantity_unit.is_livestock_product`) se compte : famille `COUNT`
   (TETE / UNITE) ou `PACKAGE` (SAC / PANIER : « 20 sacs de poussins » reste plausible).
   - `UNITE` → canonicalisé en `TETE` : mapping EXPLICITE entre deux unités de la même
     famille de comptage (une unité de bœuf = une tête) ;
   - `KG` / `TONNE` / `LITRE`… (MASS / VOLUME) → REJET : la quantité changerait de sens.
3. Tout autre produit : accepté (aucune taxonomie connue ⇒ aucune fausse alerte).

Module pur : aucune E/S. Réutilise le registre d'unités (`quantity_unit`) et la classification
de familles (`analytics.units`) — ne crée aucun nouveau vocabulaire d'unités.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from ladini.domain.analytics.units import measurement_family_of
from ladini.domain.quantity_unit import is_livestock_product, normalize_unit


class UnitAction(str, Enum):
    ACCEPT = "ACCEPT"
    CANONICALIZE = "CANONICALIZE"
    REJECT = "REJECT"


@dataclass(frozen=True)
class UnitVerdict:
    action: UnitAction
    #: Unité à persister (canonique). Pour REJECT : l'unité reçue, jamais persistée.
    unit: str
    reason: Optional[str] = None
    #: Unités acceptables à proposer à l'utilisateur en cas de rejet.
    suggested: tuple = ()

    @property
    def ok(self) -> bool:
        return self.action != UnitAction.REJECT


#: Familles compatibles avec un produit d'élevage.
_LIVESTOCK_FAMILIES = frozenset({"COUNT", "PACKAGE"})
#: Mapping EXPLICITE entre unités de comptage d'un animal (même famille COUNT).
_LIVESTOCK_COUNT_ALIASES = {"UNITE": "TETE"}


def _canonical(unit: Any) -> str:
    raw = str(unit or "").strip()
    return normalize_unit(raw) or raw.upper()


def validate_product_unit(
    product_name: Any,
    unit: Any,
    *,
    category_config: Optional[Dict[str, Any]] = None,
) -> UnitVerdict:
    """Verdict sur `unit` pour ce produit. Ne lève jamais."""
    canonical = _canonical(unit)
    if not canonical:
        return UnitVerdict(UnitAction.ACCEPT, canonical)

    # 1. Config ADMIN de la sous-catégorie (source de vérité quand elle existe).
    allowed = [
        _canonical(u) for u in ((category_config or {}).get("allowed_units") or []) if u
    ]
    if allowed:
        if canonical in allowed:
            return UnitVerdict(UnitAction.ACCEPT, canonical)
        return UnitVerdict(
            UnitAction.REJECT,
            canonical,
            reason="unit_not_allowed_for_category",
            suggested=tuple(allowed),
        )

    # 2. Repli taxonomique : élevage => famille de comptage.
    if is_livestock_product(product_name):
        family = measurement_family_of(canonical)
        if family not in _LIVESTOCK_FAMILIES:
            return UnitVerdict(
                UnitAction.REJECT,
                canonical,
                reason="livestock_requires_count_unit",
                suggested=("TETE",),
            )
        mapped = _LIVESTOCK_COUNT_ALIASES.get(canonical)
        if mapped:
            return UnitVerdict(UnitAction.CANONICALIZE, mapped, reason="livestock_count_alias")

    return UnitVerdict(UnitAction.ACCEPT, canonical)


def offer_data_quality_flags(
    product_name: Any, unit: Any, price: Any
) -> "tuple[list, bool]":
    """Signaux de qualité d'une offre AVANT de la proposer à l'achat.

    Retourne `(flags, purchasable)` :

    - `invalid_price` (prix absent, nul, négatif ou non numérique) ⇒ NON achetable : aucune
      commande valide ne peut en découler, l'offre est écartée (et journalisée par l'appelant,
      jamais cachée en silence) ;
    - `unit_incompatible_with_product` / `unit_generic_for_livestock` ⇒ achetable mais
      SIGNALÉE : c'est une donnée héritée (créée avant l'invariant), la retirer du catalogue est
      une décision du propriétaire des données (voir le rapport d'audit), pas de la recherche.
    """
    flags: list = []
    purchasable = True
    try:
        price_value = float(price)
    except (TypeError, ValueError):
        price_value = 0.0
    if not (price_value > 0):
        flags.append("invalid_price")
        purchasable = False
    verdict = validate_product_unit(product_name, unit)
    if verdict.action == UnitAction.REJECT:
        flags.append("unit_incompatible_with_product")
    elif verdict.action == UnitAction.CANONICALIZE:
        flags.append("unit_generic_for_livestock")
    return flags, purchasable


__all__ = ["UnitAction", "UnitVerdict", "validate_product_unit", "offer_data_quality_flags"]
