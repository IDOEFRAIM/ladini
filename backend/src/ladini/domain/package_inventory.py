"""Inventaire PAR CONDITIONNEMENT d'un produit (B16, 2026-10-03).

## Le problème

`Product.quantity_for_sale` est un TOTAL en unité de base (58 L). Pour « 50 bidons de 500 ml + 100 bidons de
330 ml » ce total ne dit pas combien de bidons de CHAQUE taille existent : 60 bidons de 500 ml (30 L) passaient
parce que 30 <= 58, alors que seuls 50 existent. Les comptes étaient en plus PERDUS à la publication.

## Le modèle (aucune migration)

- **Source de vérité du stock d'un produit conditionné** : le COMPTE par variante (`available_count`), porté par le
  palier qui a un `packaging` dans `Product.pricing_tiers` (JSONB existant). `Product.quantity_for_sale` en est
  l'AGRÉGAT PHYSIQUE : invariant à la publication `quantity_for_sale == Σ available_count × base_unit_quantity`
  (Decimal, tolérance 0,0005). Un débit/restock de variante met à jour LES DEUX dans la MÊME transaction, sous le
  verrou `FOR UPDATE` du produit — jamais deux sources concurrentes.
- **Palier de PRIX ≠ variante PHYSIQUE** : un palier sans `packaging` (« 10 L à 900 FCFA ») reste un simple palier
  tarifaire ; seule la présence de `packaging` ET `available_count` fait de lui une variante d'inventaire.
- **Identité d'une variante** : (type de conditionnement, taille CANONIQUE `base_unit_quantity`) — « sachet 500 ml »
  == « sachet 0,5 L » == « sachet 50 cl » ; « sachet 500 ml » != « bidon 500 ml ».
- **Pas de stock libre** : le domaine ne représente PAS « 20 sachets + 40 L en vrac » pour un même produit. Un
  produit à inventaire par conditionnement est entièrement conditionné.
- **Dérive tolérée, jamais exploitée** : un autre chemin qui ne connaît pas les comptes (ajustement producteur,
  matching récurrent) peut baisser `quantity_for_sale` seul. La disponibilité d'une variante est alors
  `min(compte, ⌊quantité physique / taille⌋)` — les comptes ne sont JAMAIS un moyen de vendre plus que le stock
  physique.

Module pur (aucune E/S) : opère sur l'objet `Product` déjà verrouillé par l'appelant.
"""

from __future__ import annotations

import copy
import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

from ladini.domain.pricing_tiers import resolve_stock_debit

logger = logging.getLogger("ladini.domain.package_inventory")

#: tolérance de l'invariant somme(variantes) == stock physique (colonne Numeric(14,3)).
TOLERANCE = Decimal("0.0005")
_QUANT = Decimal("0.001")


class PackageInventoryError(ValueError):
    """Violation d'invariant d'inventaire par conditionnement (message métier + code machine)."""

    def __init__(self, message: str, *, reason: str, **extra: Any) -> None:
        super().__init__(message)
        self.reason = reason
        self.extra = extra


def _d(value: Any) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def is_physical_variant(tier: Any) -> bool:
    return (
        isinstance(tier, dict)
        and bool(str(tier.get("packaging") or "").strip())
        and tier.get("available_count") is not None
    )


def has_package_inventory(tiers: Any) -> bool:
    """Au moins une variante physique avec un compte -> le produit est géré PAR CONDITIONNEMENT."""
    return any(is_physical_variant(t) for t in (tiers or []))


def packaged_total(tiers: Any) -> Decimal:
    """Σ available_count × base_unit_quantity (Decimal exact — 100 × 0,33 == 33)."""
    return sum(
        (_d(t["available_count"]) * _d(t.get("base_unit_quantity")) for t in (tiers or []) if is_physical_variant(t)),
        Decimal(0),
    )


def validate_inventory_invariant(tiers: Any, quantity_for_sale: Any) -> None:
    """À la PUBLICATION : tous les conditionnements ont un compte ET Σ comptes × taille == stock physique.

    Sans aucun compte : rien à valider (produit historique / tarifs seuls). Compte sur certains conditionnements
    seulement : refus (inventaire partiel = stock libre implicite, non représentable)."""
    packaged = [t for t in (tiers or []) if isinstance(t, dict) and str(t.get("packaging") or "").strip()]
    counted = [t for t in packaged if t.get("available_count") is not None]
    if not counted:
        return
    if len(counted) != len(packaged):
        raise PackageInventoryError(
            "Donnez le nombre disponible de CHAQUE conditionnement (ou aucun).", reason="partial_package_inventory"
        )
    total = packaged_total(tiers)
    stock = _d(quantity_for_sale)
    if abs(total - stock) > TOLERANCE:
        raise PackageInventoryError(
            f"Le stock total ({stock} ) ne correspond pas à la somme des conditionnements ({total}).",
            reason="package_inventory_mismatch",
            expected=str(total),
            actual=str(stock),
        )


def _find(product: Any, tier_id: Any) -> Optional[Dict[str, Any]]:
    for t in getattr(product, "pricing_tiers", None) or []:
        if isinstance(t, dict) and str(t.get("tier_id")) == str(tier_id):
            return t
    return None


def effective_available_count(product: Any, tier: Dict[str, Any]) -> int:
    """`min(compte, ⌊stock physique / taille⌋)` : les comptes ne permettent jamais de vendre plus que le physique."""
    size = _d(tier.get("base_unit_quantity"))
    count = int(tier.get("available_count") or 0)
    if size <= 0:
        return 0
    physical = int((_d(getattr(product, "quantity_for_sale", 0)) / size + Decimal("1e-9")).to_integral_value(rounding="ROUND_FLOOR"))
    return max(0, min(count, physical))


def check_variant_availability(product: Any, tier_id: Any, requested_count: int) -> Optional[Dict[str, Any]]:
    """`None` si OK (ou si ce palier n'a pas d'inventaire) ; sinon le détail du refus (variante saturée)."""
    tier = _find(product, tier_id)
    if tier is None or not is_physical_variant(tier):
        return None
    available = effective_available_count(product, tier)
    if int(requested_count) > available:
        return {
            "reason": "insufficient_package_stock",
            "tier_id": str(tier_id),
            "packaging": tier.get("packaging"),
            "package_size": tier.get("quantity"),
            "package_unit": tier.get("unit"),
            "requested_packages": int(requested_count),
            "available_packages": available,
        }
    return None


def _with_count(product: Any, tier_id: Any, new_count: int) -> None:
    """Réassigne `pricing_tiers` (NOUVELLE liste, NOUVEAUX dicts : le JSONB n'est pas suivi en place)."""
    tiers = copy.deepcopy(list(getattr(product, "pricing_tiers", None) or []))
    for t in tiers:
        if isinstance(t, dict) and str(t.get("tier_id")) == str(tier_id):
            t["available_count"] = int(new_count)
    product.pricing_tiers = tiers


def _set_quantity(product: Any, new_quantity: Decimal) -> None:
    product.quantity_for_sale = new_quantity.quantize(_QUANT, rounding=ROUND_HALF_UP)


def debit_stock_for_item(product: Any, item: Any) -> Optional[Dict[str, Any]]:
    """Débite UNE ligne de commande (produit DÉJÀ verrouillé par l'appelant). `None` = succès ; sinon le détail du
    refus (aucune mutation).

    Ligne sur une variante physique : le COMPTE ET le stock physique sont débités ensemble. Sinon : débit du
    stock physique seul (comportement historique inchangé : base-unit, palier tarifaire, produit sans inventaire)."""
    base_debit = _d(resolve_stock_debit(item))
    available = _d(getattr(product, "quantity_for_sale", 0))
    unit = str(getattr(product, "unit", None) or "KG").upper()
    tier_id = getattr(item, "tier_id", None)
    variant = _find(product, tier_id) if tier_id else None
    if variant is not None and is_physical_variant(variant):
        packages = int(float(getattr(item, "quantity", 0) or 0))
        refusal = check_variant_availability(product, tier_id, packages)
        if refusal is not None:
            return {**refusal, "requested": float(base_debit), "available": float(available), "unit": unit,
                    "name": getattr(product, "name", None), "product_id": str(getattr(product, "id", ""))}
        _with_count(product, tier_id, int(variant["available_count"]) - packages)
        _set_quantity(product, available - base_debit)
        logger.info(
            "PACKAGE_INVENTORY_DEBITED product_id=%s tier_id=%s packages=%d base_debit=%s remaining_packages=%d",
            getattr(product, "id", None), tier_id, packages, base_debit, int(variant["available_count"]) - packages,
        )
        return None
    if available < base_debit:
        return {"requested": float(base_debit), "available": float(available), "unit": unit,
                "name": getattr(product, "name", None), "product_id": str(getattr(product, "id", ""))}
    _set_quantity(product, available - base_debit)
    return None


def restore_stock_for_item(product: Any, item: Any) -> None:
    """Restitue UNE ligne (annulation/refus/expiration) : stock physique ET, pour une variante physique encore
    présente, son compte. Variante disparue (palier republié) : stock physique seul, journalisé."""
    base = _d(resolve_stock_debit(item))
    available = _d(getattr(product, "quantity_for_sale", 0))
    tier_id = getattr(item, "tier_id", None)
    variant = _find(product, tier_id) if tier_id else None
    if variant is not None and is_physical_variant(variant):
        packages = int(float(getattr(item, "quantity", 0) or 0))
        _with_count(product, tier_id, int(variant["available_count"]) + packages)
        logger.info(
            "PACKAGE_INVENTORY_RESTORED product_id=%s tier_id=%s packages=%d base_restore=%s",
            getattr(product, "id", None), tier_id, packages, base,
        )
    elif tier_id and has_package_inventory(getattr(product, "pricing_tiers", None)):
        logger.warning(
            "PACKAGE_INVENTORY_RESTORE_VARIANT_MISSING product_id=%s tier_id=%s — stock physique seul",
            getattr(product, "id", None), tier_id,
        )
    _set_quantity(product, available + base)


def describe_variants(tiers: Any) -> List[Dict[str, Any]]:
    """Projection LISIBLE (API/admin) : [{packaging, size, unit, available_count}] — jamais seulement « 58 L »."""
    return [
        {
            "tier_id": t.get("tier_id"),
            "packaging": t.get("packaging"),
            "package_size": t.get("quantity"),
            "package_unit": t.get("unit"),
            "canonical_size": t.get("base_unit_quantity"),
            "available_count": t.get("available_count"),
            "price": t.get("price"),
        }
        for t in (tiers or [])
        if is_physical_variant(t)
    ]


__all__ = [
    "PackageInventoryError",
    "TOLERANCE",
    "is_physical_variant",
    "has_package_inventory",
    "packaged_total",
    "validate_inventory_invariant",
    "effective_available_count",
    "check_variant_availability",
    "debit_stock_for_item",
    "restore_stock_for_item",
    "describe_variants",
]
