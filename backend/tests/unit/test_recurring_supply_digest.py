"""Rendu du digest et du détail (`domain/recurring_supply/digest.py`) — fonctions pures, sans DB.
Les scénarios "vrai PostgreSQL" (agrégation, dédup, N+1) sont dans
`tests/schema/test_recurring_supply_digest_service.py`."""
from __future__ import annotations

import uuid
from decimal import Decimal

from ladini.domain.recurring_supply.digest import (
    AllocationLine,
    NeedAvailability,
    build_detail_text,
    build_digest_text,
    dedupe_key_for_digest,
    digest_counts,
    digest_signature,
)


def n(product, requested, matched, unit="KG"):
    return NeedAvailability(product=product, requested_quantity=Decimal(requested), matched_quantity=Decimal(matched), unit=unit)


# ── digest : couverture ───────────────────────────────────────────────────

def test_full_coverage_is_marked_with_a_checkmark():
    text = build_digest_text([n("tomate", 40, 40)])
    assert "✅ Tomate : 40/40 KG disponibles" in text


def test_partial_coverage_is_marked_with_a_warning():
    text = build_digest_text([n("pomme de terre", 50, 30)])
    assert "⚠️ Pomme de terre : 30/50 KG disponibles" in text


def test_zero_coverage_is_marked_with_a_cross():
    text = build_digest_text([n("poulet", 30, 0, unit="UNITE")])
    assert "❌ Poulet : 0/30 UNITE disponibles" in text


def test_several_products_are_all_listed_in_the_given_order():
    text = build_digest_text([n("tomate", 40, 40), n("oignon", 20, 20), n("poulet", 30, 0)])
    lines = text.splitlines()
    tomate_idx = next(i for i, line in enumerate(lines) if "Tomate" in line)
    oignon_idx = next(i for i, line in enumerate(lines) if "Oignon" in line)
    poulet_idx = next(i for i, line in enumerate(lines) if "Poulet" in line)
    assert tomate_idx < oignon_idx < poulet_idx


def test_an_actionable_digest_offers_direct_confirmation():
    """VS4 pilote (confirmation depuis le digest) : dès qu'une disponibilité existe, le digest
    invite explicitement à confirmer/modifier/refuser directement — jamais un simple "voir les
    détails" qui forcerait à naviguer (mandat CAS 8, en creux : le contraire du cas sans rien)."""
    text = build_digest_text([n("tomate", 40, 40)])
    assert "confirmer" in text.lower()
    assert "modifier" in text.lower()
    assert "pas demain" in text.lower()


def test_an_actionable_digest_never_shows_the_read_only_menu():
    text = build_digest_text([n("tomate", 40, 40)])
    assert "1. Voir les détails" not in text and "2. Mes besoins" not in text


def test_a_non_actionable_digest_never_offers_a_confirmation_action():
    """CAS 8 du mandat : aucune disponibilité nulle part -> aucun bouton/action de confirmation."""
    text = build_digest_text([n("tomate", 40, 0), n("poulet", 30, 0)])
    assert "confirmer" not in text.lower()
    assert "1. Voir les détails" in text and "2. Mes besoins" in text


def test_global_availability_and_full_need_count_are_correct():
    text = build_digest_text([n("tomate", 40, 40), n("oignon", 20, 20), n("pdt", 50, 30), n("poulet", 30, 0)])
    assert "Disponibilité globale : 90/140" in text
    assert "2 de vos 4 besoins ont une disponibilité complète." in text


def test_the_wording_never_promises_a_reservation_or_a_confirmed_order():
    text = build_digest_text([n("tomate", 40, 40)])
    forbidden = ("réservé", "réservée", "garanti", "commande confirmée")
    assert not any(word in text.lower() for word in forbidden)


# ── digest : compteurs (télémétrie) ───────────────────────────────────────

def test_digest_counts_classifies_each_need_exactly_once():
    counts = digest_counts([n("tomate", 40, 40), n("oignon", 20, 10), n("poulet", 30, 0)])
    assert counts == {"occurrence_count": 3, "full_count": 1, "partial_count": 1, "unavailable_count": 1}


# ── détail : prix, unités, plusieurs producteurs ──────────────────────────

def test_detail_lists_every_producer_with_quantity_and_price():
    allocations = [
        AllocationLine(producer_label="Coopérative A", quantity=Decimal(40), unit_price=Decimal(500), unit="KG"),
        AllocationLine(producer_label="Coopérative B", quantity=Decimal(35), unit_price=Decimal(510), unit="KG"),
        AllocationLine(producer_label="Ferme C", quantity=Decimal(25), unit_price=Decimal(520), unit="KG"),
    ]
    text = build_detail_text(product="tomate", requested_quantity=Decimal(100), unit="KG", allocations=allocations)
    assert "Coopérative A" in text and "40 KG — 500 FCFA/kg" not in text  # unité littérale, voir assertion suivante
    assert "40 KG — 500 FCFA/KG" in text
    assert "35 KG — 510 FCFA/KG" in text
    assert "25 KG — 520 FCFA/KG" in text


def test_detail_computes_the_exact_total_price():
    allocations = [
        AllocationLine(producer_label="Coopérative A", quantity=Decimal(40), unit_price=Decimal(500), unit="KG"),
        AllocationLine(producer_label="Coopérative B", quantity=Decimal(35), unit_price=Decimal(510), unit="KG"),
        AllocationLine(producer_label="Ferme C", quantity=Decimal(25), unit_price=Decimal(520), unit="KG"),
    ]
    text = build_detail_text(product="tomate", requested_quantity=Decimal(100), unit="KG", allocations=allocations)
    # 40*500 + 35*510 + 25*520 = 20000 + 17850 + 13000 = 50850
    assert "Total estimé : 50850 FCFA" in text


def test_detail_with_no_allocation_says_so_without_crashing():
    text = build_detail_text(product="poulet", requested_quantity=Decimal(30), unit="UNITE", allocations=[])
    assert "Aucune disponibilité pour le moment." in text


def test_detail_never_exposes_producer_ids_or_phone_numbers():
    allocations = [AllocationLine(producer_label="Coopérative A", quantity=Decimal(1), unit_price=Decimal(1), unit="KG")]
    text = build_detail_text(product="tomate", requested_quantity=Decimal(1), unit="KG", allocations=allocations)
    assert "+226" not in text and "uuid" not in text.lower()


def test_detail_never_promises_a_reservation_before_confirmation():
    text = build_detail_text(product="tomate", requested_quantity=Decimal(1), unit="KG", allocations=[])
    assert "vérifiée lors de votre confirmation" in text
    assert "confirmer la commande" not in text.lower()


# ── signature / dedupe (mandat §9/§10) ────────────────────────────────────

def test_the_same_set_of_occurrence_versions_yields_the_same_signature_regardless_of_order():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    sig1 = digest_signature([(a, 3), (b, 1)])
    sig2 = digest_signature([(b, 1), (a, 3)])
    assert sig1 == sig2


def test_a_version_bump_on_any_occurrence_changes_the_signature():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    sig1 = digest_signature([(a, 3), (b, 1)])
    sig2 = digest_signature([(a, 4), (b, 1)])
    assert sig1 != sig2


def test_dedupe_key_embeds_buyer_date_and_signature():
    import datetime

    key = dedupe_key_for_digest("buyer-1", datetime.date(2026, 9, 22), "abc123")
    assert key == "recurring_supply_digest:buyer-1:2026-09-22:abc123"
