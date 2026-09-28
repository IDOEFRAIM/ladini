"""Base du prix ↔ unité de la quantité : jamais de réinterprétation silencieuse.

## Incident réel (2026-09-28, capture WhatsApp)

    Producteur : « 500f le sachet de lait »   (quantité déjà donnée : 50 LITRE)
    Récap      : « Vente de 50 LITRE de Lait de vache à 500 FCFA/SAC.
                   ⚠️ Le prix sera appliqué par LITRE (unité de l'offre), pas par SAC »

Le récap se contredit (« 500 FCFA/SAC » affiché, « par LITRE » exécuté) et l'exécution
(`create_product`) applique le prix à l'unité de la QUANTITÉ : 500 F le sachet devenait
500 F le LITRE. Si un sachet fait 0,5 L, le litre valait 1000 F ; s'il fait 5 L, 100 F —
l'écart peut être de plusieurs ordres de grandeur, sur un prix que le producteur a
pourtant confirmé. Même mécanisme pour « 200 tonnes à 500000 F la tonne » (quantité
normalisée en KG → 500000 F/KG, x1000).

## Règle

`price_unit` (base du prix dite par l'utilisateur) et `unit` (unité de la quantité, celle
qui est EXÉCUTÉE) doivent désigner la même chose au moment de confirmer :

1. identiques (ou `price_unit` absent) → rien à faire ;
2. même famille convertible (masse KG/TONNE/G, volume LITRE) → le prix est CONVERTI de façon
   exacte dans l'unité de la quantité (1000 F/TONNE = 1 F/KG) ;
3. sinon (conditionnement SAC/PANIER/sachet contre LITRE/KG, TETE contre KG…) → le contenu
   du conditionnement est INCONNU : conflit. On ne devine pas, on redemande le prix dans
   l'unité de la quantité.

Module pur : aucune E/S. Réutilise les facteurs de `pricing_tiers` (seule table MASS/VOLUME).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from ladini.domain.pricing_tiers import unit_factor, unit_family
from ladini.domain.quantity_unit import normalize_unit

_CONVERTIBLE_FAMILIES = frozenset({"MASS", "VOLUME"})


class PriceBasisAction(str, Enum):
    CONSISTENT = "CONSISTENT"
    CONVERTED = "CONVERTED"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class PriceBasisResult:
    action: PriceBasisAction
    #: Prix exprimé dans l'unité de la quantité (CONSISTENT/CONVERTED) ; None si CONFLICT.
    price: Optional[float] = None
    #: Unité de la quantité (celle du prix retourné).
    unit: Optional[str] = None
    #: Base du prix telle que dite par l'utilisateur (pour l'affichage / la question).
    original_price: Optional[float] = None
    original_price_unit: Optional[str] = None


def _canonical(unit: Any) -> str:
    raw = str(unit or "").strip()
    return normalize_unit(raw) or raw.upper()


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def reconcile_price_basis(price: Any, price_unit: Any, unit: Any) -> PriceBasisResult:
    """Ramène `price` à l'unité de la quantité, ou signale qu'on ne peut pas le faire."""
    price_value = _to_float(price)
    quantity_unit = _canonical(unit)
    basis_unit = _canonical(price_unit)

    if price_value is None or not basis_unit or not quantity_unit or basis_unit == quantity_unit:
        return PriceBasisResult(
            PriceBasisAction.CONSISTENT, price=price_value, unit=quantity_unit or None
        )

    family_basis = unit_family(basis_unit)
    family_qty = unit_family(quantity_unit)
    if family_basis == family_qty and family_basis in _CONVERTIBLE_FAMILIES:
        # 1 basis_unit = f_basis (base) ; 1 quantity_unit = f_qty (base)
        converted = price_value * unit_factor(quantity_unit) / unit_factor(basis_unit)
        return PriceBasisResult(
            PriceBasisAction.CONVERTED,
            price=round(converted, 2),
            unit=quantity_unit,
            original_price=price_value,
            original_price_unit=basis_unit,
        )

    return PriceBasisResult(
        PriceBasisAction.CONFLICT,
        price=None,
        unit=quantity_unit,
        original_price=price_value,
        original_price_unit=basis_unit,
    )


def conflict_question(result: PriceBasisResult, *, unit_label: str, basis_label: str) -> str:
    """Question déterministe posée quand la base du prix est inconvertible."""
    shown = f"{result.original_price:g}" if result.original_price is not None else "ce prix"
    return (
        f"Vous m'avez donné *{shown} FCFA par {basis_label}*, mais votre quantité est en "
        f"*{unit_label}* et je ne sais pas combien de {unit_label} contient un {basis_label}. "
        f"Je ne veux pas deviner votre prix.\n\n"
        f"👉 Quel est le prix *par {unit_label}* ? (ex : 500 FCFA le {unit_label.lower()})"
    )


__all__ = [
    "PriceBasisAction",
    "PriceBasisResult",
    "reconcile_price_basis",
    "conflict_question",
]
