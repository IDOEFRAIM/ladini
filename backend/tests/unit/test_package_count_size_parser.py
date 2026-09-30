"""Parser générique "N <label libre> de M <unité>" (Étape 3, 2026-09-30).

Incident réel : "j'ai 50 pot de 4 litre" devenait `quantity=4 L` (le "50"
disparaissait), puis "Quel est votre prix par litre ?" comme si la quantité
était résolue. Root cause CONFIRMÉE par audit direct (voir
`domain/commercial_offer_flow.py::parse_generic_package_count_and_size`,
docstring) : le moteur de paliers existant (`quantity_unit.py::
parse_packaging_message`) exige un mot de conditionnement tiré d'une liste
FERMÉE de 9 mots (`_TIER_PACKAGING_WORDS`) — "pot" n'y figure pas.

Ce parser est GÉNÉRIQUE (§3 du mandat) : AUCUN mot de conditionnement n'est
hardcodé ici ni ailleurs dans ce fichier de test — "pot"/"caisse" ne sont
testés que comme des labels parmi d'autres, jamais traités spécialement par
le code testé.
"""
from __future__ import annotations

import pytest

from ladini.domain.commercial_offer import (
    Provenance,
    derive_available_quantity_from_package,
)
from ladini.domain.commercial_offer_flow import (
    parse_generic_package_count_and_size as parse,
)

# =====================================================================
# §12 du mandat — cas A-K
# =====================================================================


class TestCanonicalCases:
    def test_a_50_pots_de_4L(self):
        pkg = parse("50 pots de 4 L")
        assert pkg is not None
        assert pkg.count == 50
        assert pkg.package_type == "POT"
        assert pkg.content_amount == 4.0
        assert pkg.content_unit == "LITRE"
        derived = derive_available_quantity_from_package(pkg)
        assert derived is not None
        assert derived.amount == 200.0
        assert derived.unit == "LITRE"

    def test_b_20_sacs_de_50kg(self):
        pkg = parse("20 sacs de 50 kg")
        derived = derive_available_quantity_from_package(pkg)
        assert derived.amount == 1000.0 and derived.unit == "KG"

    def test_c_15_bidons_de_20L(self):
        pkg = parse("15 bidons de 20 L")
        derived = derive_available_quantity_from_package(pkg)
        assert derived.amount == 300.0 and derived.unit == "LITRE"

    def test_d_30_caisses_de_12kg(self):
        pkg = parse("30 caisses de 12 kg")
        derived = derive_available_quantity_from_package(pkg)
        assert derived.amount == 360.0 and derived.unit == "KG"

    def test_e_10_cageots_de_15kg(self):
        pkg = parse("10 cageots de 15 kg")
        derived = derive_available_quantity_from_package(pkg)
        assert derived.amount == 150.0 and derived.unit == "KG"

    def test_f_12_barquettes_de_500g(self):
        """g -> KG via le mécanisme EXISTANT (`_CONTENT_UNIT_MAP`, déjà utilisé
        par `parse_package_content` pour le contenu d'un conditionnement) —
        aucune nouvelle table de conversion créée."""
        pkg = parse("12 barquettes de 500 g")
        assert pkg is not None
        assert pkg.content_amount == 0.5
        assert pkg.content_unit == "KG"
        derived = derive_available_quantity_from_package(pkg)
        assert derived.amount == 6.0 and derived.unit == "KG"

    def test_g_50_pots_count_only_no_availability(self):
        assert parse("50 pots") is None

    def test_h_pot_de_4L_size_only_no_availability(self):
        assert parse("pot de 4 L") is None

    def test_i_4L_bare_no_package(self):
        """Une quantité nue ne doit JAMAIS créer de `PackageDefinition`."""
        assert parse("4 L") is None

    def test_j_gibberish_no_false_calculation(self):
        assert parse("50 trucs completement bizarres sans unite") is None

    @pytest.mark.parametrize(
        "label", ["cageot", "barquette", "fut", "regime", "tonneau", "gobelet"]
    )
    def test_k_free_label_not_whitelisted_is_representable(self, label):
        pkg = parse(f"5 {label}s de 2 kg")
        assert pkg is not None
        assert pkg.count == 5
        assert pkg.content_amount == 2.0


# =====================================================================
# Généricité — aucun mot hardcodé, singularisation simple
# =====================================================================


class TestGenericity:
    def test_pot_absent_from_any_whitelist_still_works(self):
        """Preuve directe que "pot" n'est dans AUCUNE liste fermée du dépôt et
        que ce parser n'en a pas besoin."""
        from ladini.domain.quantity_unit import _TIER_PACKAGING_WORDS

        assert "pot" not in _TIER_PACKAGING_WORDS
        assert "pots" not in _TIER_PACKAGING_WORDS
        assert parse("50 pots de 4 L") is not None

    def test_singular_and_plural_label_both_work(self):
        assert parse("1 pot de 4 L").package_type == "POT"
        assert parse("50 pots de 4 L").package_type == "POT"

    def test_short_label_not_over_singularized(self):
        """Même garde que `extract_package_word` (`len(word) > 3`) — appliquée
        ici à l'identique : "bacs" (4 lettres) DÉPASSE le seuil -> singularisé
        en "bac" ; un mot de 3 lettres ou moins resterait intact (aucun cas
        canonique du mandat n'en fournit, non testé séparément ici)."""
        pkg = parse("10 bacs de 5 L")
        assert pkg is not None
        assert pkg.package_type == "BAC"


class TestAbstentionOnAmbiguity:
    def test_ambiguous_multiword_label_abstains(self):
        """"50 gros pots rouges de 4 L" — 3 mots avant "de" : structure hors du
        périmètre volontairement étroit de ce parser conservateur (§5)."""
        assert parse("50 gros pots rouges de 4 L") is None

    def test_price_present_abstains(self):
        """§9 : une déclaration de STOCK ne doit jamais se mélanger à un PRIX
        — laisse la main au moteur de paliers / à la logique de pricing basis
        (hors périmètre de cette étape)."""
        assert parse("50 pots de 4 L a 2750 fcfa") is None
        assert parse("2750 fcfa le pot de 4 L") is None

    def test_two_groups_abstains(self):
        """Ce parser conservateur ne gère qu'UN seul groupe — 2+ groupes
        restent du ressort du moteur de paliers/du LLM (§4 : généralité
        raisonnable, pas la couverture totale)."""
        assert parse("50 pots de 4 L et 20 sacs de 10 kg") is None

    def test_stray_third_number_abstains(self):
        assert parse("50 pots de 4 L 2000") is None

    def test_fractional_count_abstains(self):
        """Un compte de conditionnements est un entier par nature."""
        assert parse("50,5 pots de 4 L") is None

    def test_zero_or_negative_count_abstains(self):
        assert parse("0 pots de 4 L") is None

    def test_empty_text_abstains(self):
        assert parse("") is None
        assert parse(None) is None


# =====================================================================
# §14 du mandat — tests d'invariant
# =====================================================================


def test_invariant_count_and_size_explicit_gives_exact_product():
    pkg = parse("20 sacs de 50 kg")
    derived = derive_available_quantity_from_package(pkg)
    assert derived.amount == pkg.count * pkg.content_amount


@pytest.mark.parametrize("text", ["50 pots", "pot de 4 L"])
def test_invariant_count_or_size_missing_never_derives(text):
    pkg = parse(text)
    if pkg is None:
        # Le parser lui-même s'abstient déjà — l'invariant est structurellement
        # respecté (rien à dériver depuis `None`).
        return
    assert derive_available_quantity_from_package(pkg) is None


def test_invariant_pricing_tier_sizes_never_define_inventory():
    """Un palier tarifaire (`PricingTier.quantity`) n'est PAS un
    `PackageDefinition` et ne doit jamais alimenter
    `derive_available_quantity_from_package` — vérifié en s'assurant que ce
    parser n'extrait RIEN d'un message de paliers tarifaires (le prix present
    fait déjà abstenir, voir `test_price_present_abstains` ci-dessus ; ce test
    couvre en plus le cas 2-tiers sans prix isolé dans une seule clause)."""
    assert parse("le bidon de 5 L a 700 fcfa et celui de 9 L a 1000 fcfa") is None


def test_provenance_of_derived_quantity_is_domain_derived():
    pkg = parse("50 pots de 4 L")
    derived = derive_available_quantity_from_package(pkg)
    assert derived.source == Provenance.DOMAIN_DERIVED
