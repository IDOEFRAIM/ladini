"""Bug réel (2026-09-12) : "j'ai 2 tonnes et 375 kg....Je veux vendre le kg
a 225 fcfa" — le récapitulatif affichait "2 TONNE" en ignorant totalement
les 375 kg. Cause : `_QUANTITY_UNIT_RE` incluait `.` dans la classe de
caractères de l'unité, donc un point de suspension COLLÉ au mot suivant
sans espace ("kg....Je") faisait capturer "kg....Je" comme unité — un
token qui ne correspond à aucun synonyme connu, donc `375` se retrouvait
sans unité reconnue et `parse_compound_quantity` ne voyait qu'UNE seule
paire quantité+unité valide ("2 tonnes"), pas deux, et n'additionnait
jamais rien (voir `src/ladini/domain/quantity_unit.py`)."""
from __future__ import annotations

from ladini.domain.quantity_unit import (
    parse_compound_quantity,
    parse_quantity_unit_from_text,
)


class TestEllipsisGluedToUnitNoLongerSwallowsTheNextWord:
    def test_real_incident_message_sums_both_units_correctly(self):
        text = "j'ai 2 tonnes et 375 kg....Je veux vendre le kg a 225 fcfa"
        result = parse_compound_quantity(text)
        assert result.unit == "KG"
        assert result.quantity == 2375.0

    def test_ellipsis_with_no_space_does_not_merge_into_the_unit_token(self):
        result = parse_quantity_unit_from_text("375 kg....Je veux vendre")
        assert result.unit == "KG"
        assert result.quantity == 375.0

    def test_compound_quantity_still_works_with_a_normal_space(self):
        text = "2 tonnes et 375 kg, prix 225 fcfa le kg"
        result = parse_compound_quantity(text)
        assert result.unit == "KG"
        assert result.quantity == 2375.0

    def test_trailing_period_directly_after_unit_still_resolves(self):
        result = parse_quantity_unit_from_text("j'ai 50 kg.")
        assert result.unit == "KG"
        assert result.quantity == 50.0
