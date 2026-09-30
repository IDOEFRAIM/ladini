"""Étape 6 clôture (2026-09-30) — dernières vérités métier parallèles sur les
unités/conversions éliminées.

Avant ce correctif, 2 wrappers legacy maintenaient chacun leur PROPRE copie
partielle et divergente des facteurs de conversion de `domain/quantity_unit.py`
(la source canonique unique établie lors de la 1ère passe de l'Étape 6) :

- `market_coach/actions/common.py::normalize_quantity_to_kg` (table
  `_UNIT_TO_KG` locale) — connaissait QUINTAL (100 kg), que le registre
  central ignorait encore.
- `market_coach/utils.py::_normalize_quantity_to_kg` (if/elif locaux
  TONNE/GRAMME/KG) — ignorait QUINTAL, contrairement au wrapper ci-dessus :
  la DIVERGENCE exacte que cette clôture élimine ("2 quintaux" se convertissait
  différemment selon lequel des deux wrappers le traitait).

Les deux fonctions délèguent maintenant à `normalize_unit`/`measurement_family`/
`convert_quantity` — ce fichier verrouille leur résultat EXACT contre le calcul
canonique indépendant, pour empêcher toute divergence future silencieuse."""
from __future__ import annotations

import pytest

from ladini.domain.quantity_unit import (
    convert_quantity,
    measurement_family,
    normalize_unit,
)
from ladini.graphs.agents.market_coach.actions.common import (
    normalize_quantity_to_kg as _legacy_actions_common,
)
from ladini.graphs.agents.market_coach.utils import (
    _normalize_quantity_to_kg as _legacy_utils_payload,
)

# =====================================================================
# A/B — QUINTAL (mandat §3, statut décidé : usage réel prouvé par
# tests/unit/test_commercial_offer_price_basis_hardening.py::
# test_quintal_still_converts, préexistant — intégré au registre central,
# jamais un alias "q" isolé sans preuve d'usage)
# =====================================================================


class TestQuintalIsNowCanonical:
    def test_one_quintal_is_100_kg(self):
        assert convert_quantity(1, "QUINTAL", "KG") == 100.0

    def test_two_point_five_quintaux_is_250_kg(self):
        assert convert_quantity(2.5, "QUINTAL", "KG") == 250.0

    def test_family_is_mass(self):
        assert measurement_family("QUINTAL") == "MASS"

    def test_no_bare_q_alias_without_proof_of_usage(self):
        assert normalize_unit("q") is None


# =====================================================================
# C — ancien helper `actions/common.py` == source canonique
# =====================================================================


class TestActionsCommonWrapperMatchesCanonicalSource:
    @pytest.mark.parametrize(
        "unit,qty",
        [
            ("KG", 50), ("KILOGRAMME", 50), ("KILOGRAMMES", 50),
            ("G", 500), ("GRAMME", 500), ("GRAMMES", 500),
            ("TONNE", 2), ("TONNES", 2), ("T", 2),
            ("QUINTAL", 3), ("QUINTAUX", 3),
        ],
    )
    def test_mass_units_match_canonical_conversion(self, unit, qty):
        got_qty, got_unit = _legacy_actions_common(qty, unit)
        canonical_token = normalize_unit(unit)
        expected_qty = convert_quantity(float(qty), canonical_token, "KG")
        assert got_unit == "KG"
        assert got_qty == expected_qty

    @pytest.mark.parametrize(
        "unit", ["SAC", "PANIER", "TETE", "UNITE", "LITRE", "L", "SACHET"]
    )
    def test_non_mass_units_bypass_exactly_like_measurement_family(self, unit):
        got_qty, _got_unit = _legacy_actions_common(7, unit)
        canonical_token = normalize_unit(unit)
        assert measurement_family(canonical_token) != "MASS"
        assert got_qty == 7.0  # jamais converti


# =====================================================================
# D — ancien helper `market_coach/utils.py::_normalize_quantity_to_kg` ==
# source canonique
# =====================================================================


class TestUtilsPayloadWrapperMatchesCanonicalSource:
    @pytest.mark.parametrize(
        "unit,qty",
        [
            ("KG", 50), ("TONNE", 2), ("TONNES", 2),
            ("G", 500), ("GRAMME", 500),
            ("QUINTAL", 3), ("QUINTAUX", 3),
        ],
    )
    def test_mass_units_match_canonical_conversion(self, unit, qty):
        result = _legacy_utils_payload({"quantity": qty, "unit": unit})
        canonical_token = normalize_unit(unit)
        expected_qty = convert_quantity(float(qty), canonical_token, "KG")
        assert result["unit"] == "KG"
        assert result["quantity"] == expected_qty

    @pytest.mark.parametrize("unit", ["SAC", "TETE", "UNITE", "LITRE"])
    def test_non_mass_units_are_left_untouched(self, unit):
        result = _legacy_utils_payload({"quantity": 9, "unit": unit})
        canonical_token = normalize_unit(unit)
        assert measurement_family(canonical_token) != "MASS"
        assert result["quantity"] == 9  # jamais converti, jamais retypé en float
        assert result["unit"] == unit


# =====================================================================
# E/F — sous-multiples (déjà livrés en 1ère passe, reverrouillés ici au
# niveau des deux wrappers legacy eux-mêmes, pas seulement de la source)
# =====================================================================


class TestSubMultiplesThroughTheLegacyWrappers:
    def test_500_g_is_0_5_kg_via_actions_common(self):
        assert _legacy_actions_common(500, "g") == (0.5, "KG")

    def test_500_g_is_0_5_kg_via_utils_payload(self):
        result = _legacy_utils_payload({"quantity": 500, "unit": "g"})
        assert result["quantity"] == 0.5 and result["unit"] == "KG"

    def test_500_ml_is_0_5_l_via_canonical_source(self):
        # ml/cl/dl restent hors du périmètre MASS des deux wrappers legacy
        # (ils n'ont jamais traité le VOLUME) — vérifié directement sur la
        # source canonique, qui reste la seule vérité pour ces unités.
        assert convert_quantity(500, "MILLILITRE", "LITRE") == 0.5


# =====================================================================
# G — conversion inter-famille impossible
# =====================================================================


class TestCrossFamilyConversionStaysImpossible:
    def test_litre_to_kg_is_none(self):
        assert convert_quantity(1, "LITRE", "KG") is None

    def test_actions_common_never_guesses_litre_as_kg(self):
        qty, unit = _legacy_actions_common(1, "LITRE")
        assert (qty, unit) == (1.0, "LITRE")


# =====================================================================
# H — normalisation idempotente
# =====================================================================


class TestNormalizationIsIdempotent:
    @pytest.mark.parametrize("raw", ["quintal", "QUINTAUX", "kg", "tonne", "ml"])
    def test_normalize_twice_is_the_same_as_once(self, raw):
        once = normalize_unit(raw)
        twice = normalize_unit(once)
        assert once == twice


# =====================================================================
# §9 — Test de non-divergence explicite : pour CHAQUE unité MASS supportée
# par les deux anciens wrappers, les DEUX doivent produire exactement le
# même résultat que le calcul canonique indépendant — empêche une future
# divergence silencieuse comme celle qui existait pour QUINTAL avant cette
# clôture (connu de l'un, ignoré de l'autre).
# =====================================================================


class TestNoDivergenceBetweenTheTwoLegacyWrappers:
    @pytest.mark.parametrize(
        "unit,qty",
        [
            ("KG", 10), ("G", 250), ("TONNE", 1.5), ("QUINTAL", 4),
        ],
    )
    def test_both_legacy_wrappers_agree_with_each_other_and_with_canonical(
        self, unit, qty
    ):
        canonical_token = normalize_unit(unit)
        canonical_expected = convert_quantity(float(qty), canonical_token, "KG")

        actions_common_qty, actions_common_unit = _legacy_actions_common(qty, unit)
        utils_result = _legacy_utils_payload({"quantity": qty, "unit": unit})

        assert actions_common_unit == "KG" == utils_result["unit"]
        assert actions_common_qty == canonical_expected
        assert utils_result["quantity"] == canonical_expected
        assert actions_common_qty == utils_result["quantity"]
