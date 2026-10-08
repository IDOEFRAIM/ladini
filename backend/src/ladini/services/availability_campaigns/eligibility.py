"""Éligibilité d'une offre à une campagne de disponibilités — règle PURE (aucune I/O).

Une offre n'est présentée que si chaque fait affiché est vérifiable. Les raisons d'exclusion sont des codes
STABLES (l'opérateur les voit dans « offres exclues » ; les tests les verrouillent).

Fraîcheur : le schéma n'a AUCUNE colonne « disponibilité confirmée le … ». La seule preuve est
`Product.updated_at` (le producteur met son produit à jour -> `updated_at` avance). Une offre dont la dernière
mise à jour dépasse `max_age_hours` est donc exclue (`STALE`) : elle n'est jamais présentée comme garantie.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from ladini.domain.quantity_unit import normalize_unit

# Raisons d'exclusion (vocabulaire stable).
INACTIVE = "INACTIVE"
EXPIRED = "EXPIRED"
ZERO_QUANTITY = "ZERO_QUANTITY"
STALE = "STALE"
PRODUCER_NOT_APPROVED = "PRODUCER_NOT_APPROVED"
MISSING_FIELD = "MISSING_FIELD"
INCOMPATIBLE_UNIT = "INCOMPATIBLE_UNIT"
PAST_DELIVERY_DATE = "PAST_DELIVERY_DATE"
PRICE_REQUIRED = "PRICE_REQUIRED"

ALL_REASONS: FrozenSet[str] = frozenset(
    {INACTIVE, EXPIRED, ZERO_QUANTITY, STALE, PRODUCER_NOT_APPROVED, MISSING_FIELD,
     INCOMPATIBLE_UNIT, PAST_DELIVERY_DATE, PRICE_REQUIRED}
)

DEFAULT_APPROVED_PRODUCER_STATUSES: FrozenSet[str] = frozenset({"APPROVED", "VERIFIED", "ACTIVE"})
DEFAULT_MAX_AGE_HOURS = 96
MAX_OFFERS_PER_MESSAGE = 5


@dataclass(frozen=True)
class OfferCandidate:
    """Offre immédiatement disponible (une ligne `marketplace.products` + son producteur), déjà lue en base."""

    product_id: str
    producer_id: str
    name: str
    unit: str
    quantity: float
    updated_at: Optional[datetime]
    is_available: bool = True
    producer_status: Optional[str] = None
    category_label: Optional[str] = None
    region: Optional[str] = None
    zone_id: Optional[str] = None
    harvest_date: Optional[datetime] = None
    quality_class: Optional[str] = None
    #: libellé de prix FIABLE (`product_pricing_view(...).pricing_label`) ou None si le prix n'est pas affichable.
    price_label: Optional[str] = None
    #: `CERTIFIED` / `LEGACY_PARTIAL` / `UNKNOWN_BASIS`.
    price_status: Optional[str] = None
    #: aucune date d'expiration n'existe sur `Product` : renseignée seulement par une source qui en a une.
    expires_at: Optional[datetime] = None
    #: quantité déjà réservée (absente de `Product` aujourd'hui -> 0) ; la quantité présentée = quantity - reserved.
    reserved_quantity: float = 0.0


@dataclass(frozen=True)
class EligibilityRules:
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS
    approved_producer_statuses: FrozenSet[str] = DEFAULT_APPROVED_PRODUCER_STATUSES
    #: la campagne n'accepte que des offres au prix affichable (sinon : « prix à confirmer » est toléré).
    require_price: bool = False
    #: date de livraison annoncée par la campagne : déjà dépassée -> aucune offre n'est présentable.
    delivery_date: Optional[date] = None


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reasons: Tuple[str, ...] = field(default_factory=tuple)


def available_quantity(c: OfferCandidate) -> float:
    return max(0.0, float(c.quantity or 0.0) - max(0.0, float(c.reserved_quantity or 0.0)))


def evaluate_offer(c: OfferCandidate, now: datetime, rules: EligibilityRules) -> Verdict:
    """`(ok, raisons)` — TOUTES les raisons applicables sont listées (pas seulement la première)."""
    reasons: List[str] = []
    if not c.is_available:
        reasons.append(INACTIVE)
    if c.expires_at is not None and c.expires_at <= now:
        reasons.append(EXPIRED)
    if available_quantity(c) <= 0:
        reasons.append(ZERO_QUANTITY)
    if not str(c.name or "").strip() or not str(c.unit or "").strip():
        reasons.append(MISSING_FIELD)
    elif normalize_unit(str(c.unit)) is None:
        reasons.append(INCOMPATIBLE_UNIT)
    if str(c.producer_status or "").upper() not in rules.approved_producer_statuses:
        reasons.append(PRODUCER_NOT_APPROVED)
    # Fraîcheur : pas de date de mise à jour = non vérifiable = exclue (jamais « présumée fraîche »).
    if c.updated_at is None or c.updated_at < now - timedelta(hours=max(1, int(rules.max_age_hours))):
        reasons.append(STALE)
    if rules.delivery_date is not None and rules.delivery_date < now.date():
        reasons.append(PAST_DELIVERY_DATE)
    if rules.require_price and not c.price_label:
        reasons.append(PRICE_REQUIRED)
    return Verdict(ok=not reasons, reasons=tuple(reasons))


def select_offers(
    candidates: List[OfferCandidate],
    now: datetime,
    rules: EligibilityRules,
    *,
    max_offers: int = MAX_OFFERS_PER_MESSAGE,
) -> Tuple[List[OfferCandidate], List[Dict[str, Any]]]:
    """Retient au plus `max_offers` (<= 5) offres éligibles, ordre DÉTERMINISTE ; retourne aussi les exclues + raisons.

    Ordre : fraîcheur décroissante, puis quantité décroissante, puis identifiant (reproductible à l'identique).
    Une offre éligible au-delà du plafond est tracée avec la raison `OVER_LIMIT` (information, pas un défaut).
    """
    cap = max(1, min(int(max_offers), MAX_OFFERS_PER_MESSAGE))
    ok: List[OfferCandidate] = []
    excluded: List[Dict[str, Any]] = []
    for c in candidates:
        v = evaluate_offer(c, now, rules)
        if v.ok:
            ok.append(c)
        else:
            excluded.append({"product_id": c.product_id, "name": c.name, "reasons": list(v.reasons)})
    ok.sort(key=lambda c: (-(c.updated_at.timestamp() if c.updated_at else 0.0), -available_quantity(c), c.product_id))
    for c in ok[cap:]:
        excluded.append({"product_id": c.product_id, "name": c.name, "reasons": ["OVER_LIMIT"]})
    return ok[:cap], excluded
