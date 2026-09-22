"""Allocation d'une occurrence de besoin récurrent à des offres producteur — fonction PURE, sans DB,
sans LLM, entièrement déterministe (mandat Phase 3 §2/§6).

Le moteur ne décide QUE de « quelle offre couvre quelle part de la quantité demandée » ; il ne lit ni
n'écrit jamais la base — c'est `workers/automation/need_matching_service.py` qui rassemble les
candidats, appelle `allocate()`, et persiste le résultat. Cette séparation rend l'algorithme
testable avec des dates/quantités fixes, sans jamais toucher PostgreSQL.

Filtre d'éligibilité et tri : voir `allocate()`. Rien ici ne consomme de stock (mandat §7) — ce
module ne fait que calculer des nombres, jamais un UPDATE.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class MatchCandidate:
    """Une offre producteur candidate pour couvrir une occurrence — déjà filtrée sur la sous-
    catégorie et le statut publié/actif par l'appelant (`NeedMatchingService`) ; ce module réapplique
    quand même les filtres qui dépendent de l'OCCURRENCE elle-même (unité, prix, quantité)."""

    producer_id: str
    product_id: str
    unit: str
    unit_price: float
    available_quantity: float
    same_zone: bool = False


@dataclass(frozen=True)
class Allocation:
    producer_id: str
    product_id: str
    quantity: float
    unit_price: float
    unit: str


def _eligible(candidate: MatchCandidate, *, unit: str, max_price_per_unit: Optional[float]) -> bool:
    """Mandat §3 : jamais de conversion d'unité implicite — comparaison littérale, insensible à la
    casse/espaces uniquement (pas de table d'équivalence KG<->TONNE ici)."""
    if candidate.available_quantity <= 0:
        return False
    if str(candidate.unit).strip().upper() != str(unit).strip().upper():
        return False
    if max_price_per_unit is not None and candidate.unit_price > max_price_per_unit:
        return False
    return True


def _sort_key(candidate: MatchCandidate):
    """Ordre déterministe (mandat §6) : zone compatible d'abord, puis prix croissant, puis quantité
    disponible décroissante, puis (producer_id, product_id) comme tie-breaker stable — deux
    exécutions sur le même état DB produisent TOUJOURS le même ordre, donc la même allocation."""
    return (
        0 if candidate.same_zone else 1,
        candidate.unit_price,
        -candidate.available_quantity,
        candidate.producer_id,
        candidate.product_id,
    )


def allocate(
    *,
    requested_quantity: float,
    unit: str,
    candidates: Sequence[MatchCandidate],
    max_price_per_unit: Optional[float] = None,
) -> list[Allocation]:
    """Filtre, trie, puis alloue GLOUTONNEMENT jusqu'à couvrir `requested_quantity` — jamais plus
    (mandat §5). Retourne la liste des allocations retenues, dans l'ordre où elles ont été prises."""
    if requested_quantity <= 0:
        return []

    eligible = [c for c in candidates if _eligible(c, unit=unit, max_price_per_unit=max_price_per_unit)]
    eligible.sort(key=_sort_key)

    allocations: list[Allocation] = []
    remaining = requested_quantity
    for c in eligible:
        if remaining <= 0:
            break
        take = min(remaining, c.available_quantity)
        if take <= 0:
            continue
        allocations.append(
            Allocation(producer_id=c.producer_id, product_id=c.product_id, quantity=take, unit_price=c.unit_price, unit=c.unit)
        )
        remaining -= take
    return allocations


__all__ = ["MatchCandidate", "Allocation", "allocate"]
