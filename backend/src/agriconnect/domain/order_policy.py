"""Order Policy — seuil minimum de commande PAR TYPE DE PRODUIT (2026-09-02).

Demande explicite utilisateur, feature full-stack (web + agent) : la
plateforme (ADMIN) peut imposer, pour un TYPE de produit (`SubCategory` —
voir `domain/governance/models.py`), une quantité minimale qu'une commande de
ce type doit représenter pour être poursuivie. Ex: "Tomates" → 50 KG.

C'est une politique de PLATEFORME, jamais une propriété commerciale du
producteur (il ne la définit ni ne la voit comme un champ éditable) et jamais
dérivée de `pricing_tiers`/du prix — voir `governance/models.py::SubCategory`
pour la colonne source unique. Ce module est LE seul endroit qui sait
comparer une quantité totale à ce seuil ; le web (Next.js) applique la MÊME
règle via son propre service côté DB partagée (`orders.service.ts`), jamais
une copie de la logique.

Ne PAS confondre avec `PricingTier.min_order_quantity`
(domain/pricing_tiers.py) : ce dernier est un minimum de NOMBRE DE PAQUETS
pour UN palier tarifaire précis (ex: "au moins 2 bidons de 10L"), configuré
implicitement par palier — un axe totalement différent de la politique de
plateforme ici.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from agriconnect.domain.quantity_unit import (
    convert_quantity,
    normalize_unit,
)


@dataclass(frozen=True)
class MinimumOrderCheck:
    """Verdict de `validate_minimum_order_quantity` — jamais construit à la main."""

    passed: bool
    reason: str  # "NO_RULE" | "OK" | "BELOW_MINIMUM" | "UNIT_INCOMPATIBLE"
    minimum_quantity: Optional[float] = None
    # Unité LITTÉRALE du seuil (ex: "KG"), telle que configurée par l'admin —
    # jamais renormalisée pour l'affichage.
    minimum_unit: Optional[str] = None
    # Le seuil converti dans `total_unit` — seulement quand une conversion
    # fiable existe (voir `quantity_unit.convert_quantity`). `None` sur
    # UNIT_INCOMPATIBLE : on ne devine jamais un taux de conversion.
    minimum_in_total_unit: Optional[float] = None


def validate_minimum_order_quantity(
    *,
    minimum_order_quantity: Optional[float],
    minimum_order_unit: Optional[str],
    total_quantity: float,
    total_unit: str,
) -> MinimumOrderCheck:
    """LA seule fonction qui décide "cette commande atteint-elle le seuil du
    type de produit ?" — appelée sur `total_quantity` (déjà résolue : pour un
    palier, `package_count * tier.quantity`, jamais `package_count` seul —
    voir cart_service.py::add_to_cart_with_ref, qui passe
    `computed_line.base_unit_quantity`/`qty` exactement comme pour la
    vérification de stock).

    `minimum_order_quantity is None` (ou <= 0, garde défensive — la validation
    à l'écriture ADMIN interdit déjà 0/négatif, voir web
    `dr-governance.service.ts::updateSubCategoryThreshold`) → `NO_RULE` :
    comportement historique intact, aucun produit ne devient soudainement
    impossible à commander.

    Une incompatibilité d'unité (ex: seuil en KG, commande en SAC — familles
    sans équivalence universelle) N'EST PAS ignorée : elle bloque
    explicitement (`UNIT_INCOMPATIBLE`) plutôt que de laisser passer une
    commande dont on ne peut pas prouver qu'elle respecte la règle — même
    discipline anti-devinette que le reste du moteur de conversion
    (`quantity_unit.py`).
    """
    if minimum_order_quantity is None:
        return MinimumOrderCheck(passed=True, reason="NO_RULE")

    minimum = float(minimum_order_quantity)
    if minimum <= 0:
        return MinimumOrderCheck(passed=True, reason="NO_RULE")

    min_unit = normalize_unit(minimum_order_unit) or str(minimum_order_unit or "").upper()
    order_unit = normalize_unit(total_unit) or str(total_unit or "").upper()

    minimum_in_total_unit = convert_quantity(minimum, min_unit, order_unit)
    if minimum_in_total_unit is None:
        return MinimumOrderCheck(
            passed=False,
            reason="UNIT_INCOMPATIBLE",
            minimum_quantity=minimum,
            minimum_unit=min_unit,
        )

    # Tolérance flottante minime (conversions successives) — jamais assez
    # large pour laisser passer un écart réel.
    if float(total_quantity) + 1e-9 < minimum_in_total_unit:
        return MinimumOrderCheck(
            passed=False,
            reason="BELOW_MINIMUM",
            minimum_quantity=minimum,
            minimum_unit=min_unit,
            minimum_in_total_unit=minimum_in_total_unit,
        )

    return MinimumOrderCheck(
        passed=True,
        reason="OK",
        minimum_quantity=minimum,
        minimum_unit=min_unit,
        minimum_in_total_unit=minimum_in_total_unit,
    )


__all__ = ["MinimumOrderCheck", "validate_minimum_order_quantity"]
