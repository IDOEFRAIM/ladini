"""Allocation d'une occurrence (`domain/recurring_supply/matching.py`) — fonction pure, sans DB.
Les scénarios "vrai PostgreSQL" (idempotence de l'écriture, verrouillage, N+1) sont dans
`tests/schema/test_need_matching_service.py`.

`allocate` (glouton, multi-source automatique) reste testé tel quel ci-dessous — primitive
CONSERVÉE, simplement plus appelée par le chemin nominal du pilote. `allocate_single_source`
(source unique, chemin nominal recurring_supply — mandat matching pilote 2026-09-23) est testée
dans sa propre section plus bas, avec les 6 cas exacts du mandat."""
from __future__ import annotations

from ladini.domain.recurring_supply.matching import (
    MatchCandidate,
    allocate,
    allocate_single_source,
)


def c(producer, product, unit="KG", price=100.0, qty=10.0, same_zone=False, reliability=None):
    return MatchCandidate(
        producer_id=producer, product_id=product, unit=unit, unit_price=price,
        available_quantity=qty, same_zone=same_zone, reliability=reliability,
    )


# ── matching simple ──────────────────────────────────────────────────────

def test_a_single_sufficient_candidate_covers_the_full_request():
    result = allocate(requested_quantity=40, unit="KG", candidates=[c("A", "p1", qty=60)])
    assert [(a.producer_id, a.quantity) for a in result] == [("A", 40)]


# ── multi-fournisseurs ───────────────────────────────────────────────────

def test_multiple_candidates_are_combined_up_to_exactly_the_requested_quantity():
    """Même prix pour les 3 : le tri retient la règle du mandat §6 (quantité disponible
    décroissante après le prix) — C (50) puis A (40) puis B, plafonné à ce qu'il reste (10)."""
    candidates = [c("A", "p1", price=100, qty=40), c("B", "p2", price=100, qty=35), c("C", "p3", price=100, qty=50)]
    result = allocate(requested_quantity=100, unit="KG", candidates=candidates)
    total = sum(a.quantity for a in result)
    assert total == 100
    assert [(a.producer_id, a.quantity) for a in result] == [("C", 50), ("A", 40), ("B", 10)]


def test_allocation_never_exceeds_the_requested_quantity():
    candidates = [c("A", "p1", qty=1000)]
    result = allocate(requested_quantity=40, unit="KG", candidates=candidates)
    assert sum(a.quantity for a in result) == 40


# ── stock insuffisant ────────────────────────────────────────────────────

def test_insufficient_total_stock_allocates_everything_available_without_overreaching():
    candidates = [c("A", "p1", qty=30), c("B", "p2", qty=30)]
    result = allocate(requested_quantity=100, unit="KG", candidates=candidates)
    assert sum(a.quantity for a in result) == 60


# ── catégorie / unité / prix ─────────────────────────────────────────────

def test_a_zero_or_empty_stock_candidate_is_excluded():
    result = allocate(requested_quantity=10, unit="KG", candidates=[c("A", "p1", qty=0)])
    assert result == []


def test_an_incompatible_unit_is_excluded_never_converted():
    result = allocate(requested_quantity=10, unit="KG", candidates=[c("A", "p1", unit="UNITE", qty=100)])
    assert result == []


def test_unit_comparison_is_case_and_whitespace_insensitive_only():
    result = allocate(requested_quantity=10, unit="KG", candidates=[c("A", "p1", unit=" kg ", qty=100)])
    assert [a.producer_id for a in result] == ["A"]


def test_a_price_above_the_cap_is_excluded():
    result = allocate(requested_quantity=10, unit="KG", max_price_per_unit=500, candidates=[c("A", "p1", price=600, qty=100)])
    assert result == []


def test_a_price_at_or_below_the_cap_is_kept():
    result = allocate(requested_quantity=10, unit="KG", max_price_per_unit=500, candidates=[c("A", "p1", price=500, qty=100)])
    assert len(result) == 1


def test_no_price_cap_accepts_any_price():
    result = allocate(requested_quantity=10, unit="KG", max_price_per_unit=None, candidates=[c("A", "p1", price=999999, qty=100)])
    assert len(result) == 1


# ── ordre déterministe (mandat §6) ────────────────────────────────────────

def test_same_zone_candidates_are_preferred_over_cheaper_out_of_zone_ones():
    candidates = [c("FAR", "p1", price=50, qty=100, same_zone=False), c("NEAR", "p2", price=80, qty=100, same_zone=True)]
    result = allocate(requested_quantity=10, unit="KG", candidates=candidates)
    assert result[0].producer_id == "NEAR"


def test_cheaper_candidates_are_preferred_within_the_same_zone_tier():
    candidates = [c("EXPENSIVE", "p1", price=200, qty=100), c("CHEAP", "p2", price=100, qty=100)]
    result = allocate(requested_quantity=10, unit="KG", candidates=candidates)
    assert result[0].producer_id == "CHEAP"


def test_at_equal_price_the_larger_available_quantity_is_preferred():
    candidates = [c("SMALL", "p1", price=100, qty=5), c("BIG", "p2", price=100, qty=50)]
    result = allocate(requested_quantity=10, unit="KG", candidates=candidates)
    assert result[0].producer_id == "BIG"


def test_the_tie_breaker_is_the_stable_producer_and_product_identifier():
    candidates = [c("B", "p1", price=100, qty=50), c("A", "p2", price=100, qty=50)]
    result = allocate(requested_quantity=10, unit="KG", candidates=candidates)
    assert result[0].producer_id == "A"


def test_two_runs_on_the_same_candidate_set_produce_the_exact_same_allocation():
    candidates = [c("C", "p3", price=100, qty=40), c("A", "p1", price=100, qty=35), c("B", "p2", price=100, qty=50)]
    r1 = allocate(requested_quantity=100, unit="KG", candidates=candidates)
    r2 = allocate(requested_quantity=100, unit="KG", candidates=list(reversed(candidates)))
    assert r1 == r2


# =====================================================================
# allocate_single_source — chemin nominal pilote (mandat matching, 2026-09-23)
# Fournisseur unique > fiabilité > prix > zone > partiel individuel, JAMAIS de combinaison.
# =====================================================================

# CAS 1 — fournisseur unique : B et C couvrent seuls, A (30) ne couvre pas — jamais de combinaison.
def test_cas1_a_single_full_coverage_candidate_is_chosen_never_a_combination():
    candidates = [c("A", "p1", qty=30), c("B", "p2", qty=50), c("C", "p3", qty=100)]
    result = allocate_single_source(requested_quantity=40, unit="KG", candidates=candidates)
    assert len(result) == 1
    assert result[0].quantity == 40  # exactement le besoin, jamais tout le stock du fournisseur choisi


# CAS 2 — deux fournisseurs couvrent 100% : le plus fiable gagne même s'il est plus cher.
def test_cas2_the_more_reliable_full_coverage_candidate_wins_over_a_cheaper_one():
    candidates = [
        c("CHEAP_UNRELIABLE", "p1", price=90, qty=100, reliability=0.4),
        c("RELIABLE", "p2", price=110, qty=100, reliability=0.95),
    ]
    result = allocate_single_source(requested_quantity=40, unit="KG", candidates=candidates)
    assert result[0].producer_id == "RELIABLE"


# CAS 3 — fiabilité équivalente ou inconnue (aucun signal réel) : le prix départage.
def test_cas3_unknown_or_equal_reliability_falls_through_to_price():
    candidates = [
        c("EXPENSIVE", "p1", price=150, qty=100, reliability=None),
        c("CHEAP", "p2", price=90, qty=100, reliability=None),
    ]
    result = allocate_single_source(requested_quantity=40, unit="KG", candidates=candidates)
    assert result[0].producer_id == "CHEAP"


def test_cas3b_a_proven_reliable_candidate_still_outranks_an_unproven_cheaper_one():
    """Un inconnu (`reliability=None`) est neutre, pas gagnant par défaut : un candidat PROUVÉ
    fiable continue de l'emporter sur un inconnu moins cher (même logique que CAS 2, ici avec un
    seul candidat connu au lieu de deux)."""
    candidates = [
        c("KNOWN_RELIABLE", "p1", price=150, qty=100, reliability=0.9),
        c("UNKNOWN_CHEAP", "p2", price=90, qty=100, reliability=None),
    ]
    result = allocate_single_source(requested_quantity=40, unit="KG", candidates=candidates)
    assert result[0].producer_id == "KNOWN_RELIABLE"


def test_cas3c_an_unproven_candidate_still_outranks_a_proven_unreliable_cheaper_one():
    """Symétrique du cas précédent : un inconnu n'est jamais pire qu'un candidat PROUVÉ peu fiable
    (ex : annulations tardives répétées), même si ce dernier est moins cher."""
    candidates = [
        c("KNOWN_UNRELIABLE", "p1", price=90, qty=100, reliability=0.1),
        c("UNKNOWN", "p2", price=150, qty=100, reliability=None),
    ]
    result = allocate_single_source(requested_quantity=40, unit="KG", candidates=candidates)
    assert result[0].producer_id == "UNKNOWN"


# CAS 4 — prix équivalent : la zone départage seulement si l'info existe (same_zone).
def test_cas4_equal_price_falls_through_to_zone_when_available():
    candidates = [c("FAR", "p1", price=100, qty=100, same_zone=False), c("NEAR", "p2", price=100, qty=100, same_zone=True)]
    result = allocate_single_source(requested_quantity=40, unit="KG", candidates=candidates)
    assert result[0].producer_id == "NEAR"


def test_cas4b_equal_price_and_zone_falls_through_to_the_deterministic_tie_break():
    candidates = [c("B", "p1", price=100, qty=100), c("A", "p2", price=100, qty=100)]
    result = allocate_single_source(requested_quantity=40, unit="KG", candidates=candidates)
    assert result[0].producer_id == "A"


# CAS 5 — aucun fournisseur ne couvre entièrement : la meilleure offre PARTIELLE individuelle.
def test_cas5_no_full_coverage_picks_the_largest_individual_partial_offer():
    candidates = [c("A", "p1", qty=30), c("B", "p2", qty=20), c("C", "p3", qty=10)]
    result = allocate_single_source(requested_quantity=40, unit="KG", candidates=candidates)
    assert len(result) == 1
    assert result[0].producer_id == "A"
    assert result[0].quantity == 30  # jamais complété par B/C — l'écart (30/40) reste explicite en amont


# CAS 6 — A=20 + B=20 pour un besoin de 40 : PAS de combinaison automatique.
def test_cas6_two_partial_offers_that_would_sum_exactly_are_never_combined():
    candidates = [c("A", "p1", qty=20), c("B", "p2", qty=20)]
    result = allocate_single_source(requested_quantity=40, unit="KG", candidates=candidates)
    assert len(result) == 1  # jamais les deux lignes
    assert result[0].quantity == 20  # jamais 40


# CAS 7 — aucun stock : liste vide, l'occurrence reste OPEN côté service (test schema dédié).
def test_cas7_no_eligible_candidate_returns_an_empty_list():
    assert allocate_single_source(requested_quantity=40, unit="KG", candidates=[]) == []
    assert allocate_single_source(requested_quantity=40, unit="KG", candidates=[c("A", "p1", qty=0)]) == []


def test_never_allocates_more_than_requested_even_with_a_much_larger_single_offer():
    result = allocate_single_source(requested_quantity=40, unit="KG", candidates=[c("A", "p1", qty=1000)])
    assert result[0].quantity == 40


def test_an_incompatible_unit_or_price_above_cap_is_excluded_same_as_allocate():
    assert allocate_single_source(requested_quantity=10, unit="KG", candidates=[c("A", "p1", unit="UNITE", qty=100)]) == []
    assert (
        allocate_single_source(
            requested_quantity=10, unit="KG", max_price_per_unit=500, candidates=[c("A", "p1", price=600, qty=100)]
        )
        == []
    )
