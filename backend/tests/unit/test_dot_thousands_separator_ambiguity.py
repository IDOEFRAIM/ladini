"""`domain/quantity_unit.py::_parse_number` — un point unique suivi
d'EXACTEMENT 3 chiffres ("500.000", "12.500") est structurellement AMBIGU :
convention francophone courante du point comme séparateur de milliers
(500 000 / 12 500 FCFA) OU un vrai décimal à 3 chiffres après la virgule
(rare, ex. "0.250" kg = 250 g). Avant ce correctif, `float()` tranchait
TOUJOURS pour "décimal" — "500.000" FCFA (cinq cent mille francs, écrit avec
le point comme séparateur de milliers) était silencieusement lu comme
`500.0` — une sous-évaluation par 1000, un bug argent réel pour tout prix ou
toute quantité FCFA écrit ainsi.

(2026-09-28, audit fiabilité agent) : mandat "Safe Failure" — en cas de
doute, ne jamais deviner : `_parse_number` rejette maintenant ce cas
ambigu (`None`), forçant l'appelant à redemander, plutôt que de certifier
silencieusement une transaction sur un montant 1000x trop petit.

`scan_number_candidates` déléguait auparavant sa PROPRE copie de la même
logique défaillante (jamais partagée) — elle délègue maintenant à
`_parse_number`, donc ce fichier teste les deux points d'entrée réels."""
from __future__ import annotations

from ladini.domain.quantity_unit import _parse_number, scan_number_candidates


class TestDotAsThousandsSeparatorIsRejectedNotGuessed:
    def test_a_bare_dot_grouped_amount_is_rejected_rather_than_undervalued(self):
        """Le scénario exact de l'incident : "500.000" FCFA ne doit JAMAIS
        devenir silencieusement 500.0."""
        assert _parse_number("500.000") is None

    def test_a_smaller_dot_grouped_amount_is_also_rejected(self):
        assert _parse_number("12.500") is None

    def test_a_single_dot_grouped_amount_with_one_leading_digit_is_rejected(self):
        assert _parse_number("1.000") is None


class TestUnambiguousDecimalsStillParseNormally:
    """Non-régression : ce garde ne doit rejeter QUE le cas structurellement
    ambigu (exactement 3 chiffres après un point isolé) — tout le reste du
    comportement existant doit rester inchangé."""

    def test_a_two_digit_decimal_still_parses(self):
        assert _parse_number("12.50") == 12.50

    def test_a_one_digit_decimal_still_parses(self):
        assert _parse_number("12.5") == 12.5

    def test_a_five_digit_decimal_still_parses(self):
        assert _parse_number("3.14159") == 3.14159

    def test_a_french_comma_decimal_still_parses_unambiguously(self):
        """La virgule reste SANS AMBIGUÏTÉ décimale dans ce contexte — jamais
        un séparateur de milliers — donc jamais rejetée par ce garde."""
        assert _parse_number("12,5") == 12.5

    def test_a_multi_dot_thousands_value_still_fails_safely_as_before(self):
        """Comportement PRÉEXISTANT inchangé : plusieurs points échouent déjà
        `float()` nativement (`ValueError`) — toujours `None`, jamais une
        valeur devinée."""
        assert _parse_number("1.500.000") is None

    def test_a_plain_integer_still_parses(self):
        assert _parse_number("461000") == 461000.0

    def test_a_space_grouped_amount_still_parses(self):
        assert _parse_number("461 000") == 461000.0

    def test_none_and_empty_input_still_return_none(self):
        assert _parse_number("") is None
        assert _parse_number(None) is None  # type: ignore[arg-type]


class TestScanNumberCandidatesDelegatesTheSameGuard:
    """`scan_number_candidates` est le point d'entrée RÉEL partagé par le
    fast-path de l'interpréteur — il doit refléter EXACTEMENT le même garde,
    jamais une copie qui pourrait diverger."""

    def test_a_dot_grouped_price_is_skipped_not_undervalued(self):
        candidates = scan_number_candidates("je vends a 500.000 fcfa")
        assert not any(c.value == 500.0 for c in candidates), (
            f"500.000 FCFA ne doit jamais être lu comme 500.0 : {candidates!r}"
        )

    def test_an_unambiguous_price_is_still_captured_correctly(self):
        candidates = scan_number_candidates("je vends a 461000 fcfa")
        assert any(c.value == 461000.0 and c.near_currency for c in candidates)
