"""Incident rapporté (2026-09-26) : un draft `RecurringNeed` pour "35 chèvres
chaque mois" serait resté avec `sub_category_id=null` / `status=FAILED`,
l'hypothèse avancée étant que "chevres" (saisie utilisateur, sans accent) ne
matche pas "Chèvre" (nom catalogue, avec accent) lors de la résolution
taxonomique.

Reproduction (voir le rapport donné à l'utilisateur) : `canonical_product_key`
(`domain/product_identity.py`, seule fonction de normalisation de nom de
produit du backend) et `is_livestock_product`/`default_unit_for_product`
(`domain/quantity_unit.py`, décision TETE vs KG) gèrent DÉJÀ correctement
accents, pluriel simple et casse pour "chevres"/"chèvre" et toutes les
variantes listées dans le rapport d'incident — vérifié ligne par ligne, pas
deviné. Ce fichier fige ce comportement en tests de non-régression : si le
vrai bug est ailleurs (résolution SQL trigram còté Postgres, hors de portée
sans base réelle — voir `tests/schema/`), CES fonctions Python ne doivent pas
en être la cause à l'avenir non plus.

Aucun `if` spécial "chevres" nulle part — ces tests valident la normalisation
GÉNÉRIQUE déjà en place (accent-folding NFKD + pluriel simple `-s`), jamais
une liste d'exceptions ad hoc."""
from __future__ import annotations

import pytest

from ladini.domain.product_identity import canonical_product_key, same_product
from ladini.domain.quantity_unit import default_unit_for_product, is_livestock_product
from ladini.services.database.category import _is_confident_category_match

# (raw user input, canonical catalog name) — les paires explicitement listées
# dans le rapport d'incident, plus quelques variantes de casse/ponctuation.
_ANIMAL_VARIANTS = [
    ("chevre", "Chèvre"),
    ("chevres", "Chèvre"),
    ("chèvre", "Chèvre"),
    ("chèvres", "Chèvre"),
    ("Chevres", "Chèvre"),
    ("CHEVRES", "Chèvre"),
    ("boeuf", "Bœuf"),
    ("bœuf", "Bœuf"),
    ("boeufs", "Bœuf"),
    ("bœufs", "Bœuf"),
    ("coq", "Coq"),
    ("coqs", "Coq"),
    ("poulet", "Poulet"),
    ("poulets", "Poulet"),
]


class TestCanonicalProductKeyUnifiesAccentAndPluralVariants:
    @pytest.mark.parametrize("raw,catalog_name", _ANIMAL_VARIANTS)
    def test_raw_input_and_catalog_name_share_the_same_canonical_key(
        self, raw, catalog_name
    ):
        assert canonical_product_key(raw) == canonical_product_key(catalog_name)

    @pytest.mark.parametrize("raw,catalog_name", _ANIMAL_VARIANTS)
    def test_same_product_recognizes_the_pair(self, raw, catalog_name):
        assert same_product(raw, catalog_name)

    def test_empty_or_none_never_falsely_matches(self):
        assert canonical_product_key(None) == ""
        assert canonical_product_key("") == ""
        assert not same_product("", "Chèvre")
        assert not same_product(None, "Chèvre")


class TestConfidentCategoryMatchAcceptsTheSameVariants:
    """`_is_confident_category_match` est le garde-fou anti-faux-positif
    appliqué APRÈS le fuzzy match trigram côté DB (`_resolve_sub_category`,
    `services/database/recurring_supply.py`) — doit accepter ces paires
    exactement comme `canonical_product_key`."""

    @pytest.mark.parametrize("raw,catalog_name", _ANIMAL_VARIANTS)
    def test_confident_match_accepts_the_pair(self, raw, catalog_name):
        assert _is_confident_category_match(raw, catalog_name)

    def test_unrelated_products_are_not_confidently_matched(self):
        assert not _is_confident_category_match("riz", "Chèvre")
        assert not _is_confident_category_match("tomate", "Bœuf")


class TestLivestockUnitDefaultsToTeteForAllReportedVariants:
    @pytest.mark.parametrize(
        "product",
        [
            "chevre", "chevres", "chèvre", "chèvres",
            "boeuf", "bœuf", "boeufs", "bœufs",
            "coq", "coqs",
            "poulet", "poulets",
        ],
    )
    def test_is_recognized_as_livestock(self, product):
        assert is_livestock_product(product) is True

    @pytest.mark.parametrize(
        "product",
        [
            "chevre", "chevres", "chèvre", "chèvres",
            "boeuf", "bœuf", "boeufs", "bœufs",
            "coq", "coqs",
            "poulet", "poulets",
        ],
    )
    def test_default_unit_is_tete_not_kg(self, product):
        assert default_unit_for_product(product) == "TETE"

    def test_a_quantity_prefixed_phrase_still_resolves_to_tete(self):
        # Le texte réel d'incident contient la quantité dans la même chaîne
        # ("35 chèvres") avant extraction propre du nom de produit — la
        # détection doit rester robuste même si ce nettoyage n'a pas encore
        # eu lieu (tokenisation par mot, pas une égalité stricte).
        assert is_livestock_product("35 chèvres") is True
        assert default_unit_for_product("35 chèvres") == "TETE"

    def test_a_non_livestock_product_keeps_the_fallback_unit(self):
        assert is_livestock_product("tomate") is False
        assert default_unit_for_product("tomate", fallback="KG") == "KG"
