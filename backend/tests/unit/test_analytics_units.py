"""domain/analytics/units.py — canonical unit / measurement-family model.

Covers the mission's mandatory unit-compatibility tests (section 19.A/D/E):
KG+KG ok, KG+G ok after conversion, KG+L rejected, KG+TETE rejected, and
alias resolution stays consistent with the existing single-source-of-truth
registry.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ladini.domain.analytics.units import (
    convert_to_canonical,
    is_recognized_unit,
    measurement_family_of,
    resolve_subcategory_canonical_unit,
    units_are_aggregation_compatible,
)


class TestMeasurementFamilyOf:
    def test_mass_units(self):
        assert measurement_family_of("KG") == "MASS"
        assert measurement_family_of("TONNE") == "MASS"

    def test_volume_units(self):
        assert measurement_family_of("LITRE") == "VOLUME"

    def test_count_units_are_not_merged_with_package(self):
        assert measurement_family_of("TETE") == "COUNT"
        assert measurement_family_of("UNITE") == "COUNT"

    def test_package_units_stay_out_of_count(self):
        # Mission: "Ne force pas SAC/PANIER dans COUNT si leur taille varie."
        assert measurement_family_of("SAC") == "PACKAGE"
        assert measurement_family_of("PANIER") == "PACKAGE"

    def test_unknown_or_missing_unit_is_other_never_a_guess(self):
        assert measurement_family_of(None) == "OTHER"
        assert measurement_family_of("") == "OTHER"
        assert measurement_family_of("BIDON") == "OTHER"


class TestAggregationCompatibility:
    def test_identical_units_are_compatible(self):
        assert units_are_aggregation_compatible("KG", "KG") is True

    def test_mass_family_is_compatible_kg_and_gram(self):
        # Mission test A: "KG + G -> OK après conversion".
        assert units_are_aggregation_compatible("KG", "G") is True

    def test_mass_and_volume_are_incompatible(self):
        # Mission: "KG + L -> jamais additionnés."
        assert units_are_aggregation_compatible("KG", "LITRE") is False

    def test_mass_and_count_are_incompatible(self):
        # Mission test A: "KG + TETE -> rejet."
        assert units_are_aggregation_compatible("KG", "TETE") is False

    def test_two_distinct_count_units_are_incompatible(self):
        # Same coarse family (COUNT) but different literal units — a head
        # of cattle is not a generic "unite" of anything else.
        assert units_are_aggregation_compatible("TETE", "UNITE") is False

    def test_two_distinct_package_units_are_incompatible(self):
        assert units_are_aggregation_compatible("SAC", "PANIER") is False

    def test_missing_unit_is_never_compatible(self):
        assert units_are_aggregation_compatible(None, "KG") is False
        assert units_are_aggregation_compatible("KG", None) is False


class TestConvertToCanonical:
    def test_gram_to_kg(self):
        # Mission worked example: "1000 G tomate + 2 KG tomate -> 3 KG".
        assert convert_to_canonical(1000, "G", "KG") == pytest.approx(1.0)

    def test_tonne_to_kg(self):
        assert convert_to_canonical(2, "TONNE", "KG") == pytest.approx(2000.0)

    def test_incompatible_pair_returns_none_never_a_wrong_number(self):
        assert convert_to_canonical(5, "LITRE", "KG") is None
        assert convert_to_canonical(5, "TETE", "KG") is None


class TestResolveSubcategoryCanonicalUnit:
    def test_uses_priority_unit_verbatim(self):
        sub_category = SimpleNamespace(priority_unit="KG")
        assert resolve_subcategory_canonical_unit(sub_category) == "KG"

    def test_normalizes_a_raw_alias(self):
        sub_category = SimpleNamespace(priority_unit="kilogramme")
        assert resolve_subcategory_canonical_unit(sub_category) == "KG"

    def test_unconfigured_subcategory_is_none_not_a_guessed_default(self):
        sub_category = SimpleNamespace(priority_unit=None)
        assert resolve_subcategory_canonical_unit(sub_category) is None


class TestIsRecognizedUnit:
    def test_valid_units_registry_members_are_recognized(self):
        for unit in ("KG", "TONNE", "SAC", "PANIER", "TETE", "UNITE", "LITRE"):
            assert is_recognized_unit(unit) is True

    def test_gram_is_not_in_the_admin_configurable_registry(self):
        # G is a valid pricing-tier granularity, never a SubCategory
        # priority_unit — the two registries are deliberately different
        # scopes (see units.py module docstring).
        assert is_recognized_unit("G") is False

    def test_unknown_unit_is_not_recognized(self):
        assert is_recognized_unit("BIDON") is False
        assert is_recognized_unit(None) is False
