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
from functools import cmp_to_key
from typing import Optional, Sequence


@dataclass(frozen=True)
class MatchCandidate:
    """Une offre producteur candidate pour couvrir une occurrence — déjà filtrée sur la sous-
    catégorie et le statut publié/actif par l'appelant (`NeedMatchingService`) ; ce module réapplique
    quand même les filtres qui dépendent de l'OCCURRENCE elle-même (unité, prix, quantité).

    `reliability` (pilote, mandat §matching-fiabilite) : ratio [0,1] de commandes honorées sur les
    dernières occurrences RÉSOLUES de ce producteur (voir `need_matching_service.py::
    _load_reliability` pour le calcul et le seuil d'échantillon minimum), ou `None` si l'historique
    est encore insuffisant pour dire quoi que ce soit — jamais une valeur inventée. `_compare`
    (ci-dessous) traite `None` comme un point NEUTRE (`_NEUTRAL_RELIABILITY`), jamais comme un score
    réel : un candidat inconnu reste départageable face à un candidat CONNU (un fournisseur prouvé
    fiable l'emporte, un fournisseur prouvé peu fiable perd face à un inconnu) ; deux candidats
    inconnus, eux, se valent exactement (même valeur neutre) et tombent sur le prix."""

    producer_id: str
    product_id: str
    unit: str
    unit_price: float
    available_quantity: float
    same_zone: bool = False
    reliability: Optional[float] = None


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


# =====================================================================
# ALLOCATION SOURCE UNIQUE — chemin nominal du pilote recurring_supply
# (mandat matching pilote, 2026-09-23). `allocate()` ci-dessus (glouton,
# multi-source automatique) N'EST PAS supprimée — elle reste utilisable
# ailleurs/plus tard (mandat : « ne supprime pas les primitives multi-
# source ») ; ce module en ajoute une SECONDE, stricte source unique, seule
# appelée par `NeedMatchingService` pour ce domaine (voir son docstring).
# =====================================================================


# Un candidat SANS historique suffisant (`reliability is None`) n'est ni bon ni mauvais — mandat :
# "considère les fournisseurs comme équivalents [...] passe au critère suivant" quand personne n'a de
# signal (CAS 3 : deux inconnus se départagent au prix, la valeur neutre s'annule). Mais un candidat
# CONNU reste comparable à un inconnu : un fournisseur PROUVÉ fiable doit continuer à l'emporter sur
# un nouvel entrant (CAS 2, mandat §6), et un nouvel entrant doit continuer à l'emporter sur un
# fournisseur PROUVÉ peu fiable (annulations tardives) — un inconnu n'est jamais pire qu'un mauvais
# historique prouvé. `0.5` est ce point neutre : ni un bonus, ni une pénalité, juste "pas encore
# d'avis" — jamais présenté comme un score réel (aucune donnée n'est affichée à l'utilisateur).
_NEUTRAL_RELIABILITY = 0.5


def _effective_reliability(candidate: MatchCandidate) -> float:
    return candidate.reliability if candidate.reliability is not None else _NEUTRAL_RELIABILITY


def _compare(a: MatchCandidate, b: MatchCandidate) -> int:
    """Cascade de départage déterministe (Règles 2/3/4 + tie-break) — jamais un score composite
    unique : chaque critère ne parle QUE si le précédent n'a rien tranché, exactement l'ordre validé
    (fiabilité > prix > zone > tie-break)."""
    # Règle 2 — fiabilité (voir `_effective_reliability` ci-dessus pour le traitement de l'inconnu).
    rel_a, rel_b = _effective_reliability(a), _effective_reliability(b)
    if rel_a != rel_b:
        return -1 if rel_a > rel_b else 1
    # Règle 3 — prix, le moins cher gagne.
    if a.unit_price != b.unit_price:
        return -1 if a.unit_price < b.unit_price else 1
    # Règle 4 — zone : signal booléen déjà exploité ailleurs (`same_zone`), jamais de distance
    # géospatiale calculée ici (mandat : "ne crée pas de moteur géospatial pour ce pilote").
    if a.same_zone != b.same_zone:
        return -1 if a.same_zone else 1
    # Tie-break déterministe final — même paire (producteur, produit) → même résultat à chaque appel.
    key_a, key_b = (a.producer_id, a.product_id), (b.producer_id, b.product_id)
    if key_a != key_b:
        return -1 if key_a < key_b else 1
    return 0


def _compare_partial(a: MatchCandidate, b: MatchCandidate) -> int:
    """Règle 5 (aucune couverture totale) : la quantité disponible est le critère PRINCIPAL —
    « meilleure offre partielle » signifie maximiser la couverture, pas minimiser le prix. Les
    critères de `_compare` ne servent qu'à départager une ÉGALITÉ de quantité."""
    if a.available_quantity != b.available_quantity:
        return -1 if a.available_quantity > b.available_quantity else 1
    return _compare(a, b)


def allocate_single_source(
    *,
    requested_quantity: float,
    unit: str,
    candidates: Sequence[MatchCandidate],
    max_price_per_unit: Optional[float] = None,
) -> list[Allocation]:
    """Alloue une occurrence à AU PLUS UN fournisseur — jamais une combinaison automatique
    (Règle 6). Retourne une liste de 0 ou 1 `Allocation`, jamais plus :

    - Règle 1 : s'il existe au moins un candidat dont `available_quantity` couvre `requested_
      quantity` en entier, on choisit UNIQUEMENT parmi ceux-là (`_compare`) et on prélève EXACTEMENT
      `requested_quantity` — jamais tout son stock disponible.
    - Règle 5 : sinon, on choisit la meilleure offre PARTIELLE individuelle (`_compare_partial`) et
      on prélève la totalité de sa disponibilité (elle ne couvre pas le besoin par définition ici).
    - Aucun candidat éligible : liste vide — l'appelant (`NeedMatchingService`) laisse l'occurrence
      `OPEN` avec `quantity_matched=0`, état de rupture déjà prévu par le domaine existant."""
    if requested_quantity <= 0:
        return []

    eligible = [c for c in candidates if _eligible(c, unit=unit, max_price_per_unit=max_price_per_unit)]
    if not eligible:
        return []

    full_coverage = [c for c in eligible if c.available_quantity >= requested_quantity]
    if full_coverage:
        best = sorted(full_coverage, key=cmp_to_key(_compare))[0]
        return [
            Allocation(
                producer_id=best.producer_id,
                product_id=best.product_id,
                quantity=requested_quantity,
                unit_price=best.unit_price,
                unit=best.unit,
            )
        ]

    best = sorted(eligible, key=cmp_to_key(_compare_partial))[0]
    return [
        Allocation(
            producer_id=best.producer_id,
            product_id=best.product_id,
            quantity=best.available_quantity,
            unit_price=best.unit_price,
            unit=best.unit,
        )
    ]


__all__ = ["MatchCandidate", "Allocation", "allocate", "allocate_single_source"]
