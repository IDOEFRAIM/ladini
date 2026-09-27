"""`utils.py::canonical_unit_label` — LE normalisateur d'unité partagé (Phase 2 hardening,
commit 8, P1 audit 2026-09-24). Avant ce commit, TETE (unité canonique de l'élevage, voir
`domain/quantity_unit.py::default_unit_for_product`) était confondue avec UNITE (générique) —
`canonical_unit_label("tête")` renvoyait "UNITE", faisant perdre silencieusement la précision
"compté à la tête" dès qu'une valeur passait par ce normalisateur (voir `nodes/memory.py::
_resolve_unit_value`, qui l'appelle pour toute réponse à "quelle unité ?")."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.utils import canonical_unit_label


class TestTeteIsItsOwnCanonicalUnit:
    def test_tete_stays_tete(self):
        assert canonical_unit_label("TETE") == "TETE"
        assert canonical_unit_label("tete") == "TETE"

    def test_accented_and_plural_spellings_all_normalize_to_tete(self):
        for raw in ("tête", "TÊTE", "tetes", "TETES", "têtes", "TÊTES"):
            assert canonical_unit_label(raw) == "TETE", raw

    def test_english_head_synonyms_normalize_to_tete_not_unite(self):
        assert canonical_unit_label("head") == "TETE"
        assert canonical_unit_label("heads") == "TETE"

    def test_tete_is_never_collapsed_into_unite(self):
        """Régression directe du P1 : ce test DOIT échouer si `_CANONICAL_UNIT_MAP`
        réintroduit un jour `"TETE": "UNITE"`."""
        assert canonical_unit_label("tete") != "UNITE"


class TestUniteRemainsItsOwnDistinctCanonicalUnit:
    """UNITE reste un canonique GÉNÉRIQUE valide et DISTINCT — le correctif ne fusionne pas
    les deux dans l'autre sens (TETE et UNITE sont deux unités légitimement différentes, pas
    des synonymes l'une de l'autre)."""

    def test_unite_and_its_synonyms_stay_unite(self):
        for raw in ("unite", "UNITÉ", "piece", "pièces", "unit", "units"):
            assert canonical_unit_label(raw) == "UNITE", raw

    def test_unite_and_tete_are_distinct_canonical_values(self):
        assert canonical_unit_label("unite") != canonical_unit_label("tete")


class TestOtherCanonicalUnitsAreUnaffected:
    """Garde de non-régression large : le correctif ne touche QUE les entrées TETE/HEAD."""

    def test_weight_and_bag_units_are_unchanged(self):
        assert canonical_unit_label("kg") == "KG"
        assert canonical_unit_label("kilogrammes") == "KG"
        assert canonical_unit_label("tonnes") == "TONNE"
        assert canonical_unit_label("sacs") == "SAC"

    def test_an_empty_value_returns_the_default(self):
        assert canonical_unit_label("", default="KG") == "KG"
        assert canonical_unit_label(None, default="TETE") == "TETE"


class TestLitreIsARealCanonicalEntryNotAnAccidentalFallthrough:
    """Analytics Phase C (2026-09-27): before this fix, `_CANONICAL_UNIT_MAP`
    had NO entry for LITRE at all — `canonical_unit_label("LITRE")` only
    "worked" via the `.get(unit, unit)` fallback (the input echoed back
    unchanged), which meant "L" and "LITRES" were NOT folded to "LITRE"
    (they came back as "L"/"LITRES" verbatim). Combined with `nodes/
    memory.py::_PRIMARY_CANONICAL_UNITS` missing "LITRE" entirely, a buyer
    answering "litre" to "quelle unité ?" was silently rejected — see
    `test_resolve_unit_value_litre.py` for that half of the regression."""

    def test_all_spellings_normalize_to_litre(self):
        for raw in ("l", "L", "litre", "LITRE", "litres", "LITRES"):
            assert canonical_unit_label(raw) == "LITRE", raw

    def test_litre_family_is_volume(self):
        from ladini.domain.analytics.units import measurement_family_of

        assert measurement_family_of(canonical_unit_label("litre")) == "VOLUME"
