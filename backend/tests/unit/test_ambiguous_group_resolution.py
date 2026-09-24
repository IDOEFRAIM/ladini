"""Résolution d'un `ambiguous_group` (`domain/recurring_supply/ambiguous_group.py`) — fonction
pure, sans DB/LLM. Mécanique GÉNÉRALE pour N candidats (mandat "lifecycle de clarification
ambiguous_groups", 2026-09-24) — aucun test ici ne doit dépendre du nom exact "mouton"/"chèvre",
sauf pour lisibilité ; `test_generic_two_products_not_livestock` le prouve avec tomate/oignon."""
from __future__ import annotations

from ladini.domain.recurring_supply.ambiguous_group import (
    AmbiguousResolutionKind,
    resolve_ambiguous_group_reply,
)

CANDIDATES = ["mouton", "chevre"]


def r(text: str, total: float = 57.0, candidates=None):
    return resolve_ambiguous_group_reply(text, total_quantity=total, candidates=candidates or CANDIDATES)


# ── mode TOTAL explicite (mandat §4 CAS A, §11 TEST 1) ────────────────────

def test_explicit_quantities_summing_to_the_total_are_accepted():
    res = r("50 moutons et 7 chevres")
    assert res.kind == AmbiguousResolutionKind.RESOLVED
    assert res.allocations == {"mouton": 50.0, "chevre": 7.0}


def test_order_of_mention_does_not_matter():
    res = r("7 chevres et 50 moutons")
    assert res.allocations == {"mouton": 50.0, "chevre": 7.0}


def test_accented_plural_forms_still_match_the_unaccented_singular_candidate():
    res = r("50 MOUTONS ET 7 CHÈVRES")
    assert res.kind == AmbiguousResolutionKind.RESOLVED


# ── mode RESTE (mandat §4 CAS B, §11 TEST 2) ──────────────────────────────

def test_one_explicit_quantity_plus_the_rest_resolves_the_other_candidate():
    res = r("20 moutons et le reste pour les chevres")
    assert res.kind == AmbiguousResolutionKind.RESOLVED
    assert res.allocations == {"mouton": 20.0, "chevre": 37.0}


def test_rest_keyword_alone_without_naming_the_remaining_candidate_still_works():
    res = r("c est 20 mouton et le reste")
    assert res.allocations == {"mouton": 20.0, "chevre": 37.0}


def test_a_negative_remainder_is_rejected_not_clamped_silently():
    res = r("70 moutons et le reste pour les chevres")
    assert res.kind == AmbiguousResolutionKind.INVALID


# ── mode EACH (mandat §6, §11 TEST 3) ─────────────────────────────────────

def test_de_chaque_assigns_the_full_total_to_every_candidate():
    res = r("57 de chaque")
    assert res.kind == AmbiguousResolutionKind.RESOLVED
    assert res.allocations == {"mouton": 57.0, "chevre": 57.0}


def test_chacun_is_recognized_as_the_each_marker_too():
    res = r("d'accord, chacun")
    assert res.allocations == {"mouton": 57.0, "chevre": 57.0}


# ── validation (mandat §5, §11 TEST 4) ────────────────────────────────────

def test_an_invalid_sum_is_rejected_with_a_targeted_correction_message():
    res = r("20 moutons et 20 chevres")
    assert res.kind == AmbiguousResolutionKind.INVALID
    assert res.allocations is None
    assert "40" in res.message and "57" in res.message


def test_a_negative_explicit_quantity_is_rejected():
    res = r("60 moutons et -3 chevres")
    assert res.kind == AmbiguousResolutionKind.INVALID


def test_only_one_candidate_specified_without_a_rest_marker_asks_for_the_other():
    res = r("50 moutons")
    assert res.kind == AmbiguousResolutionKind.INVALID


# ── non-réponse (mandat §8/§9, interruption) ──────────────────────────────

def test_an_unrelated_new_task_is_not_treated_as_a_resolution_attempt():
    res = r("je veux 14 coqs chaque semaine")
    assert res.kind == AmbiguousResolutionKind.NOT_A_RESOLUTION


def test_a_bare_abandon_phrase_is_not_a_resolution_attempt():
    res = r("laisse tomber")
    assert res.kind == AmbiguousResolutionKind.NOT_A_RESOLUTION


def test_empty_text_is_not_a_resolution_attempt():
    res = r("   ")
    assert res.kind == AmbiguousResolutionKind.NOT_A_RESOLUTION


# ── généralité : pas un patch textuel mouton/chèvre (mandat §"OBJECTIF") ──

def test_generic_two_products_not_livestock():
    res = r("30 tomates et le reste en oignons", total=100.0, candidates=["tomate", "oignon"])
    assert res.kind == AmbiguousResolutionKind.RESOLVED
    assert res.allocations == {"tomate": 30.0, "oignon": 70.0}


def test_three_candidates_all_explicit():
    res = r(
        "20 mais, 20 riz et 17 sorgho",
        total=57.0,
        candidates=["mais", "riz", "sorgho"],
    )
    assert res.kind == AmbiguousResolutionKind.RESOLVED
    assert res.allocations == {"mais": 20.0, "riz": 20.0, "sorgho": 17.0}


def test_three_candidates_two_explicit_and_a_rest_is_rejected_as_ambiguous():
    """Mandat §5 : "si plusieurs produits restent non spécifiés -> demander clarification
    ciblée" — le reste ne peut être attribué qu'à un SEUL candidat, jamais deviné entre deux."""
    res = r(
        "20 mais et le reste",
        total=57.0,
        candidates=["mais", "riz", "sorgho"],
    )
    assert res.kind == AmbiguousResolutionKind.INVALID
