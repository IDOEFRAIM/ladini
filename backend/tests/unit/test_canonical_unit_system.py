"""Étape 6 — centralisation des unités, conversions et sémantique de quantité.

Verrouille le contrat des 4 primitives centrales ajoutées à
`domain/quantity_unit.py` (`normalize_unit` existait déjà ; `measurement_family`,
`are_units_compatible`, `convert_quantity` étendu, `base_unit_for` sont
nouveaux/étendus) et leur propagation aux consommateurs existants
(`pricing_tiers.py::unit_family/unit_factor`, `commercial_offer_flow.py::
_CONTENT_UNIT_MAP`, le moteur de packaging déterministe) — SANS dupliquer un
second jeu de facteurs nulle part.

Root cause de l'Étape 6 (voir le rapport final) : `ml`/`cl`/`dl`/`g` n'étaient
reconnus QUE par une table locale et délibérément isolée
(`commercial_offer_flow.py::_CONTENT_UNIT_MAP`, "jamais ajoutés aux registres
globaux d'unités") — absents de `UNIT_SYNONYMS`, de la table de conversion la
plus complète du dépôt (`pricing_tiers.py::_MASS_FACTORS/_VOLUME_FACTORS`, qui
ne connaissait QUE KG/G/TONNE + L, jamais ML/CL/DL), et des regex du moteur de
packaging déterministe (`_TIER_QTY_UNIT_RE`/`_PACKAGE_COUNT_UNIT_RE`/
`_SCAN_UNIT_RE`) — donc invisibles à `normalize_unit`/`convert_quantity`/au
parseur "N sachets de M ml"."""
from __future__ import annotations

import pytest

from ladini.domain.pricing_tiers import (
    unit_factor,
    unit_family,
    validate_pricing_tiers,
)
from ladini.domain.quantity_unit import (
    are_units_compatible,
    base_unit_for,
    convert_quantity,
    measurement_family,
    normalize_unit,
    parse_packaging_message,
    parse_quantity_unit_from_text,
)

# =====================================================================
# §15 — Normalisation
# =====================================================================


class TestNormalizeUnit:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("L", "LITRE"), ("l", "LITRE"), ("litre", "LITRE"), ("litres", "LITRE"),
            ("ml", "MILLILITRE"), ("millilitres", "MILLILITRE"), ("ML", "MILLILITRE"),
            ("cl", "CENTILITRE"), ("centilitre", "CENTILITRE"),
            ("dl", "DECILITRE"), ("décilitre", "DECILITRE"), ("decilitres", "DECILITRE"),
            ("kg", "KG"), ("kilos", "KG"), ("kilogramme", "KG"),
            ("g", "GRAMME"), ("grammes", "GRAMME"),
            ("tonne", "TONNE"), ("t", "TONNE"),
            ("tete", "TETE"), ("têtes", "TETE"),
            ("sac", "SAC"), ("sachet", "SAC"),
            ("unite", "UNITE"), ("unités", "UNITE"),
        ],
    )
    def test_known_aliases_map_to_their_canonical_token(self, raw, expected):
        assert normalize_unit(raw) == expected

    def test_unknown_string_returns_none(self):
        assert normalize_unit("bizarre") is None

    def test_quintal_is_recognized_since_etape_6_closure(self):
        # (2026-09-30, Étape 6 clôture) : QUINTAL, auparavant reconnu
        # UNIQUEMENT par `actions/common.py::_UNIT_TO_KG` (jamais devant le
        # registre central — voir le rapport de clôture), est maintenant sa
        # propre famille MASS ici, 100 KG. Pas d'alias "q" : aucune preuve
        # d'usage trouvée dans le dépôt.
        assert normalize_unit("quintal") == "QUINTAL"
        assert normalize_unit("QUINTAUX") == "QUINTAL"
        assert normalize_unit("q") is None

    def test_normalization_is_idempotent(self):
        # I5 du mandat : normalize(normalize(x)) == normalize(x).
        once = normalize_unit("500 ml".split()[-1])
        twice = normalize_unit(once)
        assert once == twice == "MILLILITRE"


# =====================================================================
# §3/§16 — Familles et conversion
# =====================================================================


class TestMeasurementFamily:
    @pytest.mark.parametrize(
        "unit,expected",
        [
            ("LITRE", "VOLUME"), ("MILLILITRE", "VOLUME"), ("CENTILITRE", "VOLUME"),
            ("DECILITRE", "VOLUME"), ("ml", "VOLUME"),
            ("KG", "MASS"), ("GRAMME", "MASS"), ("TONNE", "MASS"), ("g", "MASS"),
            ("SAC", None), ("TETE", None), ("UNITE", None), ("PANIER", None),
            ("", None), (None, None),
        ],
    )
    def test_family_classification(self, unit, expected):
        assert measurement_family(unit) == expected


class TestAreUnitsCompatible:
    def test_same_family_volume_is_compatible(self):
        assert are_units_compatible("LITRE", "MILLILITRE") is True
        assert are_units_compatible("ml", "cl") is True

    def test_same_family_mass_is_compatible(self):
        assert are_units_compatible("KG", "GRAMME") is True

    def test_cross_family_is_never_compatible(self):
        # I2 du mandat : conversion inter-famille toujours impossible.
        assert are_units_compatible("KG", "LITRE") is False
        assert are_units_compatible("LITRE", "KG") is False
        assert are_units_compatible("UNITE", "KG") is False
        assert are_units_compatible("TETE", "LITRE") is False

    def test_identical_singleton_unit_is_compatible_with_itself(self):
        assert are_units_compatible("TETE", "TETE") is True
        assert are_units_compatible("SAC", "SAC") is True

    def test_two_different_singleton_families_are_not_compatible(self):
        assert are_units_compatible("SAC", "TETE") is False

    def test_unknown_or_empty_is_never_compatible(self):
        assert are_units_compatible("", "LITRE") is False
        assert are_units_compatible(None, "LITRE") is False


class TestConvertQuantity:
    @pytest.mark.parametrize(
        "amount,from_unit,to_unit,expected",
        [
            (500, "ml", "LITRE", 0.5),
            (1500, "ml", "LITRE", 1.5),
            (50, "cl", "LITRE", 0.5),
            (5, "dl", "LITRE", 0.5),
            (500, "g", "KG", 0.5),
            (2500, "g", "KG", 2.5),
            (1, "LITRE", "ml", 1000.0),
            (1, "KG", "g", 1000.0),
        ],
    )
    def test_canonical_conversions(self, amount, from_unit, to_unit, expected):
        assert convert_quantity(amount, from_unit, to_unit) == pytest.approx(expected)

    def test_cross_family_conversion_is_impossible(self):
        # §6 du mandat : jamais une approximation, un résultat explicite d'échec.
        assert convert_quantity(5, "KG", "LITRE") is None
        assert convert_quantity(5, "LITRE", "KG") is None
        assert convert_quantity(5, "UNITE", "KG") is None
        assert convert_quantity(5, "TETE", "LITRE") is None

    def test_identity_conversion_is_exact(self):
        assert convert_quantity(42.0, "KG", "KG") == 42.0
        assert convert_quantity(42.0, "kg", "KG") == 42.0  # alias == canonique

    def test_no_new_floating_point_error_introduced(self):
        # §14 du mandat : "333 ml x 3 = 999 ml = 0.999 L", pas 0.9989999997.
        total_ml = 333 * 3
        assert convert_quantity(total_ml, "ml", "LITRE") == pytest.approx(0.999, abs=1e-9)


class TestBaseUnitFor:
    def test_volume_base_is_litre(self):
        assert base_unit_for("ml") == "LITRE"
        assert base_unit_for("CENTILITRE") == "LITRE"
        assert base_unit_for("LITRE") == "LITRE"

    def test_mass_base_is_kg(self):
        assert base_unit_for("g") == "KG"
        assert base_unit_for("TONNE") == "KG"

    def test_singleton_family_is_its_own_base(self):
        assert base_unit_for("TETE") == "TETE"
        assert base_unit_for("SAC") == "SAC"

    def test_empty_is_none(self):
        assert base_unit_for("") is None
        assert base_unit_for(None) is None


# =====================================================================
# §17 — Package (N conditionnement de M unité)
# =====================================================================


class TestPackageParsing:
    def test_a_100_sachets_of_500ml_gives_50_litres(self):
        parsed = parse_packaging_message("100 sachets de 500 ml")
        assert parsed.quantity == pytest.approx(50.0)
        assert parsed.unit == "LITRE"
        assert not parsed.ambiguous

    def test_b_60_bidons_of_500ml_gives_30_litres(self):
        parsed = parse_packaging_message("60 bidons de 500 ml")
        assert parsed.quantity == pytest.approx(30.0)
        assert parsed.unit == "LITRE"

    def test_c_both_together_sum_to_55_litres(self):
        # Le mandat §19/§17C — réutilise le chemin multi-package EXISTANT
        # (jamais une somme inventée ailleurs).
        parsed = parse_packaging_message("50 sachets de 500 ml et 60 bidons de 500 ml")
        assert parsed.quantity == pytest.approx(55.0)
        assert parsed.unit == "LITRE"
        assert not parsed.ambiguous

    def test_mixed_sub_multiples_still_convert_to_the_same_base(self):
        # "50 sachets de 500 ml" + "60 bidons de 0.5 litre" : sous-multiples
        # DIFFÉRENTS, même famille — doivent quand même se sommer (55 L),
        # preuve que le collapse vers l'unité de base précède la comparaison
        # d'unités du moteur de packaging (`units = {c.unit ...}`). Point
        # décimal (pas virgule) : la virgule française est aussi le séparateur
        # de clause du moteur (`_TIER_CLAUSE_SPLIT_RE`) — une ambiguïté
        # PRÉ-EXISTANTE, sans rapport avec l'Étape 6 (reproduite à l'identique
        # avec "0,5 litre" seul, sans aucune unité ml/cl/dl en jeu), hors
        # périmètre de ce chantier (voir mandat §0/§25).
        parsed = parse_packaging_message("50 sachets de 500 ml et 60 bidons de 0.5 litre")
        assert parsed.quantity == pytest.approx(55.0)
        assert parsed.unit == "LITRE"
        assert not parsed.ambiguous


# =====================================================================
# §18 — Pricing (équivalence ml <-> L)
# =====================================================================


class TestPricingTierEquivalence:
    def test_500_ml_and_0_5_litre_tiers_are_commercially_equivalent(self):
        """« 500 FCFA le sachet de 500 ml » et « 500 FCFA le sachet de 0,5 L »
        doivent produire le MÊME `base_unit_quantity` — la vérité commerciale
        partagée — même si `unit`/`quantity` restent littéraux (mandat §9 :
        "conserve le packaging séparément", et contrat existant de
        `PricingTier.unit`, "jamais normalisé")."""
        in_ml = validate_pricing_tiers(
            [{"quantity": 500, "unit": "ml", "price": 500.0, "packaging": "sachet"}],
            base_unit="LITRE",
        )
        in_l = validate_pricing_tiers(
            [{"quantity": 0.5, "unit": "L", "price": 500.0, "packaging": "sachet"}],
            base_unit="LITRE",
        )
        assert in_ml[0].base_unit_quantity == pytest.approx(in_l[0].base_unit_quantity)
        assert in_ml[0].base_unit_quantity == pytest.approx(0.5)

    def test_50cl_and_0_5_litre_bidon_tiers_are_equivalent(self):
        in_cl = validate_pricing_tiers(
            [{"quantity": 50, "unit": "cl", "price": 600.0, "packaging": "bidon"}],
            base_unit="LITRE",
        )
        in_l = validate_pricing_tiers(
            [{"quantity": 0.5, "unit": "L", "price": 600.0, "packaging": "bidon"}],
            base_unit="LITRE",
        )
        assert in_cl[0].base_unit_quantity == pytest.approx(in_l[0].base_unit_quantity)


# =====================================================================
# §19 — Replay multi-tier réel (produit + paliers)
# =====================================================================


def test_multi_tier_replay_sachet_and_bidon_at_500ml_each():
    """Replay du scénario canonique du mandat : 50 sachets de 500 ml + 60
    bidons de 500 ml -> 55 L disponibles ; tarifs 500/600 FCFA sur des
    paliers de 0,5 L chacun. Aucun désaccord entre `availability` et
    `pricing_tiers` (I4 : la normalisation des tarifs ne mute pas
    l'inventaire — ce sont deux appels indépendants à la même primitive)."""
    availability = parse_packaging_message(
        "50 sachets de 500 ml et 60 bidons de 500 ml"
    )
    assert availability.quantity == pytest.approx(55.0)
    assert availability.unit == "LITRE"

    tiers = validate_pricing_tiers(
        [
            {"quantity": 500, "unit": "ml", "price": 500.0, "packaging": "sachet"},
            {"quantity": 500, "unit": "ml", "price": 600.0, "packaging": "bidon"},
        ],
        base_unit="LITRE",
    )
    assert len(tiers) == 2
    assert tiers[0].base_unit_quantity == pytest.approx(0.5)
    assert tiers[1].base_unit_quantity == pytest.approx(0.5)
    # I4 : l'inventaire (55 L) reste un nombre INDÉPENDANT des tarifs (0,5 L
    # chacun) — jamais fusionnés conceptuellement (mandat §10).
    assert availability.quantity != tiers[0].base_unit_quantity


# =====================================================================
# §20 — Invariants
# =====================================================================


class TestInvariants:
    def test_i1_same_physical_quantity_in_compatible_units_is_the_same_canonical_quantity(self):
        a = convert_quantity(500, "ml", "LITRE")
        b = convert_quantity(0.5, "LITRE", "LITRE")
        assert a == pytest.approx(b)

    def test_i2_conversion_across_incompatible_families_is_impossible(self):
        assert convert_quantity(1, "KG", "LITRE") is None
        assert are_units_compatible("KG", "LITRE") is False

    def test_i3_package_label_is_never_a_physical_unit(self):
        # "sachet"/"bidon" ne sont JAMAIS reconnus comme une unité par
        # normalize_unit — seul un vrai conditionnement (SAC canonique,
        # distinct du mot libre "sachet"/"bidon" tel quel) transiterait,
        # et un `package_label` ne se mélange jamais avec LITRE/KG.
        assert measurement_family("bidon") is None
        assert normalize_unit("bidon") is None

    def test_i4_pricing_tier_normalization_does_not_mutate_inventory(self):
        tiers_input = [{"quantity": 500, "unit": "ml", "price": 500.0, "packaging": "sachet"}]
        availability_before = parse_packaging_message("55 litres de lait")
        validate_pricing_tiers(tiers_input, base_unit="LITRE")
        availability_after = parse_packaging_message("55 litres de lait")
        assert availability_before.quantity == availability_after.quantity == pytest.approx(55.0)

    def test_i5_normalization_is_idempotent(self):
        once = normalize_unit("500 ml".split()[-1])
        assert normalize_unit(once) == once


# =====================================================================
# Non-régression : `parse_quantity_unit_from_text` reste inchangé pour les
# unités déjà reconnues avant l'Étape 6.
# =====================================================================


class TestNoRegressionOnAlreadyRecognizedUnits:
    @pytest.mark.parametrize(
        "text,expected_qty,expected_unit",
        [
            ("5 kg", 5.0, "KG"),
            ("25 L", 25.0, "LITRE"),
            ("2 tonnes", 2.0, "TONNE"),
            ("10 sacs", 10.0, "SAC"),
            ("3 têtes", 3.0, "TETE"),
        ],
    )
    def test_previously_supported_units_are_unaffected(self, text, expected_qty, expected_unit):
        result = parse_quantity_unit_from_text(text)
        assert result.quantity == expected_qty
        assert result.unit == expected_unit

    def test_new_sub_multiples_now_collapse_to_the_base_unit_at_the_boundary(self):
        # Mandat §11 : "normalisation au bord du système" — jamais MILLILITRE
        # qui circule dans le domaine, toujours LITRE déjà converti.
        result = parse_quantity_unit_from_text("500 ml")
        assert result.quantity == pytest.approx(0.5)
        assert result.unit == "LITRE"


# =====================================================================
# `unit_family`/`unit_factor` (pricing_tiers.py) — délégation transparente
# =====================================================================


class TestPricingTiersDelegation:
    @pytest.mark.parametrize(
        "unit,expected_family,expected_factor",
        [
            ("KG", "MASS", 1.0), ("KGS", "MASS", 1.0), ("G", "MASS", 0.001),
            ("TONNE", "MASS", 1000.0),
            ("L", "VOLUME", 1.0), ("LITRE", "VOLUME", 1.0),
            ("ML", "VOLUME", 0.001), ("CL", "VOLUME", 0.01), ("DL", "VOLUME", 0.1),
            ("SAC", "SAC", 1.0), ("TETE", "TETE", 1.0), ("XYZ", "XYZ", 1.0),
        ],
    )
    def test_matches_the_original_table_exactly_and_now_covers_ml_cl_dl(
        self, unit, expected_family, expected_factor
    ):
        assert unit_family(unit) == expected_family
        assert unit_factor(unit) == pytest.approx(expected_factor)
