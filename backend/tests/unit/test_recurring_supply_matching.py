"""Allocation d'une occurrence (`domain/recurring_supply/matching.py`) — fonction pure, sans DB.
Les scénarios "vrai PostgreSQL" (idempotence de l'écriture, verrouillage, N+1) sont dans
`tests/schema/test_need_matching_service.py`."""
from __future__ import annotations

from ladini.domain.recurring_supply.matching import MatchCandidate, allocate


def c(producer, product, unit="KG", price=100.0, qty=10.0, same_zone=False):
    return MatchCandidate(producer_id=producer, product_id=product, unit=unit, unit_price=price, available_quantity=qty, same_zone=same_zone)


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
