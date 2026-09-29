"""Tarification PAR CONDITIONNEMENTS (`pricing_tiers`) de SALES_PUBLISH_PRODUCT.

Incident prod : « J'ai 60 l de miel » puis « le bidon de 5 l à 700 FCFA et celui de 9 l à 1000 FCFA »
finissait sur le fallback générique. Le gate commercial rendait `None` dès que `pricing_tiers` était
présent, l'`commercial_offer` incomplet du tour précédent survivait, le draft n'était jamais complet.

Ce module est le SEUL endroit qui décide, pour un message contenant des paliers, dans quel mode
tarifaire on est. Il réutilise `domain/pricing_tiers.validate_pricing_tiers` (familles d'unités,
doublons, tout-ou-rien) et le modèle B1 (`offer_from_package_tier`) — aucun moteur nouveau.

Modes (`PricingMode`) :

- `PER_BASE_UNIT` : « 500 FCFA par litre » — géré par `commercial_offer_flow` (inchangé) ;
- `PER_PACKAGE`   : EXACTEMENT un palier avec conditionnement explicite (« bidon de 5 L à 700 ») ->
  offre B1 `PER_PACKAGE` (prix DU bidon, contenu 5 L) ;
- `PACKAGING_TIERS` : au moins un palier sans autre lecture possible (>= 2 paliers, ou un palier sans
  conditionnement nommé). `pricing_tiers` est alors la vérité commerciale ; AUCUN prix par unité n'est
  fabriqué (700/5 L n'est PAS 140 FCFA/L) et la quantité disponible n'est JAMAIS déduite des
  contenances (5 L + 9 L n'est pas 14 L de stock).

Module pur : aucune E/S.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ladini.domain.commercial_offer import CommercialOffer
from ladini.domain.commercial_offer_flow import offer_from_package_tier
from ladini.domain.pricing_tiers import PricingTierError, validate_pricing_tiers


class PricingMode(str, Enum):
    PER_BASE_UNIT = "PER_BASE_UNIT"
    PER_PACKAGE = "PER_PACKAGE"
    PACKAGING_TIERS = "PACKAGING_TIERS"


class ClarificationReason(str, Enum):
    MISSING_PACKAGE_SIZE = "MISSING_PACKAGE_SIZE"
    AMBIGUOUS_PRICING = "AMBIGUOUS_PRICING"
    INVALID_PRICING_TIER = "INVALID_PRICING_TIER"
    MISSING_AVAILABLE_QUANTITY = "MISSING_AVAILABLE_QUANTITY"


QUANTITY_QUESTION = (
    "Quelle quantité totale avez-vous à vendre ? (ex : 60 L)\n"
    "_Les contenances de vos conditionnements ne sont pas votre stock disponible._"
)


@dataclass(frozen=True)
class TierPricingResult:
    """Décision du gate pour un message porteur de paliers."""

    mode: Optional[PricingMode] = None
    tiers: Tuple[Dict[str, Any], ...] = ()
    #: mode PER_PACKAGE : l'offre B1 certifiable (jamais `None` dans ce mode)
    offer: Optional[CommercialOffer] = None
    reason: Optional[ClarificationReason] = None
    message: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        return self.mode is not None and self.reason is None


def _number(raw: Any) -> Optional[float]:
    # le LLM type parfois les nombres en chaîne (« 500 ») : accepté, jamais deviné.
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip().replace(",", "."))
        except ValueError:
            return None
    return None


def clean_tiers(raw_tiers: Any) -> List[Dict[str, Any]]:
    """Forme canonique {quantity, unit, price, packaging} — `unit` reste LITTÉRAL (voir
    `domain/catalog/models.py::Product.pricing_tiers`), `packaging` n'est JAMAIS inventé : absent = `None`."""
    out: List[Dict[str, Any]] = []
    for raw in raw_tiers or []:
        if not isinstance(raw, Mapping):
            continue
        packaging = str(raw.get("packaging") or "").strip() or None
        out.append(
            {
                "quantity": _number(raw.get("quantity")),
                "unit": str(raw.get("unit") or "").strip(),
                "price": _number(raw.get("price")),
                "packaging": packaging,
            }
        )
    return out


def evaluate_tier_pricing(
    payload: Mapping[str, Any], *, category_config: Optional[Mapping[str, Any]] = None
) -> TierPricingResult:
    """Paliers du payload -> mode tarifaire, ou clarification ciblée. Pur, sans effet de bord."""
    tiers = clean_tiers(payload.get("pricing_tiers"))
    if not tiers:
        return TierPricingResult(
            reason=ClarificationReason.INVALID_PRICING_TIER,
            message=(
                "Je n'ai pas pu lire vos tarifs. Donnez pour chaque conditionnement sa contenance et son "
                "prix, par exemple « bidon de 5 L à 700 FCFA »."
            ),
        )
    unit = str(payload.get("unit") or "").strip()
    quantity = _number(payload.get("quantity"))
    if quantity is None or quantity <= 0 or not unit:
        return TierPricingResult(
            tiers=tuple(tiers),
            reason=ClarificationReason.MISSING_AVAILABLE_QUANTITY,
            message=QUANTITY_QUESTION,
        )
    try:
        validate_pricing_tiers(tiers, unit)
    except PricingTierError as exc:
        return TierPricingResult(
            reason=ClarificationReason.INVALID_PRICING_TIER,
            message=(
                f"Je n'ai pas pu valider vos conditionnements ({exc}) Donnez pour chacun sa contenance et "
                "son prix, par exemple « bidon de 5 L à 700 FCFA et bidon de 9 L à 1000 FCFA »."
            ),
        )
    if len(tiers) == 1 and tiers[0]["packaging"]:
        try:
            offer, verdict = offer_from_package_tier(payload, tiers[0], category_config=category_config)
        except ValueError:
            offer, verdict = None, None
        if offer is not None and verdict is not None and verdict.is_valid:
            return TierPricingResult(mode=PricingMode.PER_PACKAGE, tiers=tuple(tiers), offer=offer)
        return TierPricingResult(
            reason=ClarificationReason.INVALID_PRICING_TIER,
            message=(
                "Ce conditionnement ne correspond pas à l'unité de votre stock. Redonnez sa contenance et "
                "son prix, par exemple « bidon de 5 L à 700 FCFA »."
            ),
        )
    return TierPricingResult(mode=PricingMode.PACKAGING_TIERS, tiers=tuple(tiers))


__all__ = [
    "PricingMode",
    "ClarificationReason",
    "TierPricingResult",
    "QUANTITY_QUESTION",
    "clean_tiers",
    "evaluate_tier_pricing",
    "short_unit_label",
    "render_tiers_summary",
]


# ---------------------------------------------------------------------------
# Affichage — projection PURE des paliers (jamais un prix par unité)
# ---------------------------------------------------------------------------

_SHORT_UNITS = {"LITRE": "L", "LITRES": "L", "L": "L", "KG": "kg", "KGS": "kg", "G": "g", "TONNE": "t"}


def short_unit_label(unit: Any) -> str:
    """« LITRE » / « l » -> « L », « KG » -> « kg » ; toute autre unité est rendue telle que dite."""
    text = str(unit or "").strip()
    return _SHORT_UNITS.get(text.upper(), text)


def render_tiers_summary(product: Any, quantity: Any, unit: Any, tiers: Any) -> str:
    """Miel : 60 L disponibles / - Bidon de 5 L : 700 FCFA / - Bidon de 9 L : 1 000 FCFA.

    Les paliers sont affichés TELS QUE dits : ni « 140 FCFA/L », ni « à partir de … » — un prix de
    conditionnement n'est pas un prix par unité de base."""
    from ladini.core.formatting import fmt_num

    name = str(product or "").strip()
    head = f"{name[:1].upper()}{name[1:]}" if name else "Produit"
    lines = [f"{head} : {fmt_num(quantity)} {short_unit_label(unit)} disponibles"]
    for tier in clean_tiers(tiers):
        content = f"{fmt_num(tier['quantity'])} {short_unit_label(tier['unit'])}"
        packaging = tier["packaging"]
        label = f"{packaging[:1].upper()}{packaging[1:]} de {content}" if packaging else content
        lines.append(f"- {label} : {fmt_num(tier['price'])} FCFA")
    return "\n".join(lines)
