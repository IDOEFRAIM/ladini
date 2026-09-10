"""`graphs/roles.py::normalize_role` — normalisation stricte du rôle
(2026-09-08, refonte responsabilités des nœuds d'entrée, mandat §3/§15).

Contrat exigé par le mandat :
    BUYER -> BUYER
    ACHETEUR -> BUYER
    PRODUCER -> PRODUCER
    valeur inconnue -> UNKNOWN
    None -> UNKNOWN

Avant ce correctif, toute valeur non reconnue (y compris une absence)
retombait silencieusement sur "PRODUCER" — interdit explicitement par le
mandat : le rôle de profil ne doit jamais être présumé, et ne doit de
toute façon plus décider du domaine métier courant (voir
`core/router.py::_goal_domain`, résolu PAR GOAL)."""
from __future__ import annotations

import pytest

from ladini.graphs.roles import normalize_role


class TestNormalizeRole:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("BUYER", "BUYER"),
            ("buyer", "BUYER"),
            ("  Buyer  ", "BUYER"),
            ("ACHETEUR", "BUYER"),
            ("ACHETEUSE", "BUYER"),
            ("PRODUCER", "PRODUCER"),
            ("producer", "PRODUCER"),
            ("PRODUCTEUR", "PRODUCER"),
            ("PRODUCTRICE", "PRODUCER"),
        ],
    )
    def test_recognized_values_are_normalized(self, raw, expected):
        assert normalize_role(raw) == expected

    def test_none_is_unknown_not_producer(self):
        assert normalize_role(None) == "UNKNOWN"

    def test_empty_string_is_unknown_not_producer(self):
        assert normalize_role("") == "UNKNOWN"

    @pytest.mark.parametrize("garbage", ["ADMIN", "root", "toto", "🚜", "'; DROP TABLE--"])
    def test_unrecognized_value_is_unknown_not_producer(self, garbage):
        assert normalize_role(garbage) == "UNKNOWN"

    def test_is_deterministic(self):
        assert normalize_role("garbage") == normalize_role("garbage") == "UNKNOWN"
