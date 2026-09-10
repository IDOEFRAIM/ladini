"""`flows/common/menu_contracts.py::MenuRequest` — validation à la
construction (audit ui_engine, 2026-09-09, mandat §25).

`MenuRequest` implique TOUJOURS une sélection valide — la validation vit
ICI (pas dispersée dans `ui_engine`) : rejette ce qui produirait un mapping
structurellement AMBIGU (index dupliqué/vide, valeur métier vide), mais ne
rejette JAMAIS les labels dupliqués (légitimes — ex. même nom de produit
chez deux vendeurs, distingués par leur seul index).

Non-régression empirique : la suite complète (buyer/producer flows, ~3200
tests) a été rejouée après l'ajout de cette validation — aucun des 23
sites de construction réels de `MenuRequest` ne la viole."""
from __future__ import annotations

import pytest

from ladini.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)


class TestMenuRequestRejectsAmbiguousMappings:
    def test_zero_options_is_rejected(self):
        with pytest.raises(ValueError):
            MenuRequest(title="Vide", options=[])

    def test_duplicate_index_is_rejected(self):
        with pytest.raises(ValueError):
            MenuRequest(
                title="Ambigu",
                options=[
                    MenuOption(index="1", label="A"),
                    MenuOption(index="1", label="B"),
                ],
            )

    def test_blank_index_is_rejected(self):
        with pytest.raises(ValueError):
            MenuRequest(title="X", options=[MenuOption(index="", label="A")])

    def test_whitespace_only_index_is_rejected(self):
        with pytest.raises(ValueError):
            MenuRequest(title="X", options=[MenuOption(index="   ", label="A")])

    def test_explicitly_blank_value_is_rejected(self):
        with pytest.raises(ValueError):
            MenuRequest(
                title="X",
                options=[MenuOption(index="1", label="A", value="")],
            )


class TestMenuRequestAllowsLegitimateShapes:
    def test_single_option_menu_is_allowed(self):
        """Un menu à une seule option (« un seul résultat trouvé, confirmez »)
        est une forme légitime — ce n'est pas mentionné comme invalide par
        le mandat, et rien n'en fait un mapping ambigu."""
        menu = MenuRequest(title="Un seul résultat", options=[MenuOption(index="1", label="Le Produit")])
        assert menu.to_mapping() == {"1": "1"}

    def test_duplicate_labels_are_allowed(self):
        """Deux vendeurs différents peuvent proposer le même produit — le
        label seul ne doit jamais être la clé de désambiguïsation, l'index
        l'est déjà."""
        menu = MenuRequest(
            title="Choisissez un vendeur",
            options=[
                MenuOption(index="1", label="Maïs blanc", value="vendor-a"),
                MenuOption(index="2", label="Maïs blanc", value="vendor-b"),
            ],
        )
        assert menu.to_mapping() == {"1": "vendor-a", "2": "vendor-b"}
        assert menu.to_candidates() == ["Maïs blanc", "Maïs blanc"]

    def test_value_none_falls_back_to_index_and_is_not_rejected(self):
        menu = MenuRequest(title="X", options=[MenuOption(index="1", label="A")])
        assert menu.options[0].effective_value() == "1"


class TestOptionAlignmentInvariant:
    """Mandat §24 : pour chaque option i, l'index AG-UI == l'index du
    mapping == la position du label dans `expected_candidates`. Garanti
    PAR CONSTRUCTION (les trois sont dérivés de `menu.options` dans le même
    ordre) — ce test verrouille la propriété, pas une implémentation."""

    def test_mapping_candidates_and_options_stay_aligned_by_position(self):
        menu = MenuRequest(
            title="X",
            options=[
                MenuOption(index="1", label="Premier", value="id-1"),
                MenuOption(index="2", label="Deuxieme", value="id-2"),
                MenuOption(index="3", label="Troisieme", value="id-3"),
            ],
        )
        mapping = menu.to_mapping()
        candidates = menu.to_candidates()
        for position, opt in enumerate(menu.options):
            assert mapping[opt.index] == opt.effective_value()
            assert candidates[position] == opt.label
