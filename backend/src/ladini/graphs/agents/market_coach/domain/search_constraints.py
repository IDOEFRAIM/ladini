"""Contraintes d'une demande d'achat dites DÈS LE PREMIER MESSAGE — appliquées à la première recherche, jamais relâchées en silence.

« Je veux 20 litres de lait en sachets de 500 ml à Ouaga, pas plus de 600 FCFA le litre » porte : produit, quantité, conditionnement
(sachet, 500 ml), prix maximum par litre. Ce module filtre les offres trouvées sur ce qui est PROUVABLEMENT incompatible, et seulement
avec les contraintes que le domaine sait comparer :

- **prix maximum** : comparé APRÈS normalisation d'unité (`quantity_unit.convert_quantity`) ; « 500 FCFA le sachet de 2 L » = 250 FCFA/L,
  jamais 500 FCFA/L. Offre dont le prix n'est pas un prix à l'unité (lot) ou dont l'unité n'est pas convertible : NON comparable —
  on ne l'écarte pas (rien de prouvé) ;
- **conditionnement** (type + contenu) : une offre sans conditionnement ne peut pas fournir de « sachets » -> écartée ; seuls les
  paliers qui correspondent restent proposés ;
- **région** : NON filtrée ici — dans « à Ouaga » la région est le lieu de LIVRAISON, pas celui du producteur.

Aucune comparaison textuelle, aucune relaxation automatique : si rien ne correspond, l'appelant le dit et propose d'élargir.
"""

from __future__ import annotations

import copy
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ladini.domain.quantity_unit import convert_quantity, normalize_unit


def _norm(value: Any) -> str:
    return unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower().strip()


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


@dataclass(frozen=True)
class SearchConstraints:
    max_price_per_unit: Optional[float] = None
    #: Unité du prix maximum (« le litre » -> LITRE) ; à défaut, l'unité de la quantité demandée.
    price_unit: Optional[str] = None
    package_type: Optional[str] = None
    #: Contenu d'un paquet dans l'unité de base (500 ml -> 0.5 LITRE).
    package_size: Optional[float] = None
    package_unit: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        return self.max_price_per_unit is None and not self.package_type and self.package_size is None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in {
            "max_price_per_unit": self.max_price_per_unit, "price_unit": self.price_unit, "package_type": self.package_type,
            "package_size": self.package_size, "package_unit": self.package_unit,
        }.items() if v not in (None, "")}

    def describe(self) -> str:
        parts: List[str] = []
        if self.package_type or self.package_size is not None:
            size = f" de {self.package_size:g} {(self.package_unit or '').lower()}".rstrip() if self.package_size is not None else ""
            parts.append(f"{self.package_type or 'conditionnement'}{size}")
        if self.max_price_per_unit is not None:
            parts.append(f"≤ {self.max_price_per_unit:g} FCFA/{(self.price_unit or 'unité').lower()}")
        return ", ".join(parts)


REMOVABLE = ("max_price", "packaging")


def constraints_from_dict(data: Optional[Mapping[str, Any]]) -> SearchConstraints:
    """Relit les contraintes SAUVEGARDÉES dans le snapshot du menu (`SearchConstraints.to_dict()`)."""
    d = dict(data or {})
    return SearchConstraints(
        max_price_per_unit=_num(d.get("max_price_per_unit")),
        price_unit=str(d["price_unit"]) if d.get("price_unit") else None,
        package_type=str(d["package_type"]) if d.get("package_type") else None,
        package_size=_num(d.get("package_size")),
        package_unit=str(d["package_unit"]) if d.get("package_unit") else None,
    )


def merge_constraints(
    current: SearchConstraints,
    *,
    max_price: Optional[float] = None,
    package_type: Optional[str] = None,
    package_size: Optional[float] = None,
    remove: Sequence[str] = (),
) -> SearchConstraints:
    """Contraintes après une édition : valeurs POSÉES / REMPLACÉES, puis les contraintes nommées dans `remove` SUPPRIMÉES.

    « le prix n'importe plus » retire réellement `max_price_per_unit` (`None`), jamais un plafond factice énorme. Une contrainte non mentionnée est conservée."""
    max_p = _num(max_price) if max_price is not None else current.max_price_per_unit
    ptype = (_norm(package_type) or None) if package_type else current.package_type
    psize = _num(package_size) if package_size is not None else current.package_size
    punit = current.package_unit if package_size is None else (current.price_unit or current.package_unit)
    removed = {str(r).lower() for r in remove}
    if "max_price" in removed:
        max_p = None
    if "packaging" in removed:
        ptype, psize, punit = None, None, None
    return SearchConstraints(
        max_price_per_unit=max_p, price_unit=current.price_unit, package_type=ptype, package_size=psize, package_unit=punit,
    )


def constraints_from_payload(payload: Mapping[str, Any]) -> SearchConstraints:
    """Lit les contraintes déjà extraites (jamais devinées) ; valeurs non comparables ignorées."""
    price_unit = normalize_unit(str(payload.get("max_price_unit") or payload.get("price_unit") or payload.get("unit") or "")) or None
    package_unit = normalize_unit(str(payload.get("package_content_unit") or "")) or None
    amount = _num(payload.get("package_content_amount"))
    size: Optional[float] = None
    if amount is not None and package_unit:
        size = convert_quantity(amount, package_unit, price_unit) if price_unit else None
        if size is None:
            size, package_unit = amount, package_unit
        else:
            package_unit = price_unit
    return SearchConstraints(
        max_price_per_unit=_num(payload.get("max_price_per_unit")),
        price_unit=price_unit,
        package_type=_norm(payload.get("package_type")) or None,
        package_size=size,
        package_unit=package_unit,
    )


@dataclass
class FilterReport:
    kept: List[Dict[str, Any]] = field(default_factory=list)
    excluded: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def excluded_count(self) -> int:
        return len(self.excluded)


def _per_unit(price: Optional[float], unit: Optional[str], target_unit: Optional[str]) -> Optional[float]:
    """Prix par unité CIBLE, ou `None` quand la conversion n'est pas fiable."""
    if price is None or not unit or not target_unit:
        return None
    factor = convert_quantity(1.0, unit, target_unit)  # 1 `unit` = `factor` `target_unit`
    return price / factor if factor else None


def _tier_matches_package(tier: Mapping[str, Any], c: SearchConstraints) -> bool:
    if c.package_type and c.package_type not in _norm(tier.get("packaging")):
        return False
    if c.package_size is not None:
        base = _num(tier.get("base_unit_quantity") or tier.get("quantity"))
        tier_unit = normalize_unit(str(tier.get("unit") or "")) or c.package_unit
        if base is None:
            return False
        size_in_tier_unit = convert_quantity(base, tier_unit, c.package_unit) if tier_unit and c.package_unit else base
        if size_in_tier_unit is None or abs(size_in_tier_unit - c.package_size) > 1e-6:
            return False
    return True


def _tier_price_ok(tier: Mapping[str, Any], c: SearchConstraints) -> Optional[bool]:
    """`True`/`False` si le prix du palier est comparable au prix max ; `None` si NON comparable."""
    if c.max_price_per_unit is None:
        return True
    base = _num(tier.get("base_unit_quantity") or tier.get("quantity"))
    unit = normalize_unit(str(tier.get("unit") or ""))
    price = _num(tier.get("price"))
    if base is None or price is None:
        return None
    per_unit = _per_unit(price / base, unit, c.price_unit)
    return None if per_unit is None else per_unit <= c.max_price_per_unit + 1e-9


def apply_constraints(vendors: Sequence[Mapping[str, Any]], c: SearchConstraints) -> FilterReport:
    """Écarte les offres PROUVABLEMENT incompatibles ; ne touche à aucune offre quand `c` est vide."""
    report = FilterReport()
    if c.is_empty:
        report.kept = [dict(v) for v in vendors]
        return report
    wants_package = bool(c.package_type) or c.package_size is not None
    for raw in vendors:
        vendor = copy.deepcopy(dict(raw))
        name = str(vendor.get("vendor_name") or "")
        tiers = [t for t in (vendor.get("pricing_tiers") or []) if isinstance(t, Mapping)]
        if wants_package:
            matching = [t for t in tiers if _tier_matches_package(t, c)]
            if not matching:
                report.excluded.append((name, "package"))
                continue
            priced = [(t, _tier_price_ok(t, c)) for t in matching]
            if c.max_price_per_unit is not None and all(ok is False for _, ok in priced):
                report.excluded.append((name, "price"))
                continue
            vendor["pricing_tiers"] = [t for t, ok in priced if ok is not False]
            report.kept.append(vendor)
            continue
        if c.max_price_per_unit is not None:
            if tiers:
                oks = [_tier_price_ok(t, c) for t in tiers]
                if all(ok is False for ok in oks):
                    report.excluded.append((name, "price"))
                    continue
                vendor["pricing_tiers"] = [t for t, ok in zip(tiers, oks, strict=True) if ok is not False]
            elif str(vendor.get("price_basis") or "").upper() not in {"TOTAL_LOT", "LOT"}:
                per_unit = _per_unit(_num(vendor.get("price")), normalize_unit(str(vendor.get("unit") or "")), c.price_unit)
                if per_unit is not None and per_unit > c.max_price_per_unit + 1e-9:
                    report.excluded.append((name, "price"))
                    continue
        report.kept.append(vendor)
    return report


def no_result_message(c: SearchConstraints, product: str, excluded_count: int) -> str:
    """Aucune offre compatible : on le dit, on ne relâche RIEN, on propose d'élargir."""
    return (
        f"Je n'ai rien trouvé pour *{product}* avec ces critères ({c.describe()}). "
        f"J'ai écarté {excluded_count} offre{'s' if excluded_count > 1 else ''} qui n'y correspondaient pas.\n"
        "Tu peux élargir le prix, essayer un autre conditionnement, ou je lance un appel d'offres."
    )


__all__ = [
    "REMOVABLE",
    "constraints_from_dict",
    "merge_constraints",
    "FilterReport",
    "SearchConstraints",
    "apply_constraints",
    "constraints_from_payload",
    "no_result_message",
]
