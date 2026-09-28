"""`services/database/buyer.py::_guess_display_unit` — P0 confirmé par audit
(mandat "Commercial Quantity & Pricing Domain Hardening") : l'ancienne
`_LIVESTOCK_KEYWORDS` locale testait `keyword in normalized_name` — un
SUBSTRING sans frontière de mot — donc "ane" (âne) matchait à l'intérieur
de "banane", classant une simple banane comme bétail (`unit -> TETE`).

C'était en plus une DEUXIÈME liste de mots-clés livestock, indépendante et
en désaccord potentiel avec `domain/quantity_unit.py::LIVESTOCK_PRODUCT_
KEYWORDS` (la source déjà utilisée côté producteur). Correctif : `buyer.py`
délègue maintenant à `is_livestock_product` (même module que côté
producteur) — plus de liste dupliquée, plus de faux positif par substring.

Résidu documenté (pas fermé par ce correctif, ni par le mandat de cette
session) : `is_livestock_product` reste en frontière de MOT, pas en
sémantique — "lait de vache" contient le mot ENTIER "vache" et reste donc
classé bétail des deux côtés. Voir
`docs/domain/COMMERCIAL_QUANTITY_PRICING_MODEL.md` pour la vraie fermeture
(config `SubCategory.allowed_units` déjà câblée de bout en bout, seulement
non peuplée pour les sous-catégories concernées — une donnée administrative
manquante, pas un bug de code)."""
from __future__ import annotations

from ladini.services.database.buyer import _guess_display_unit


class TestSubstringFalsePositiveIsFixed:
    def test_a_banana_is_never_classified_as_livestock(self):
        """Le scénario EXACT du bug : "ane" (âne) était un substring de
        "banane" avec l'ancienne liste locale — une banane devenait TETE."""
        assert _guess_display_unit("banane", None) == "KG"

    def test_a_banana_with_no_db_unit_stored_is_also_safe(self):
        assert _guess_display_unit("bananes plantain", "") == "KG"


class TestGenuineLivestockStillClassifiesAsTete:
    def test_boeufs_still_resolves_to_tete(self):
        assert _guess_display_unit("boeufs", None) == "TETE"

    def test_chevres_still_resolves_to_tete(self):
        assert _guess_display_unit("chèvres", None) == "TETE"

    def test_poulets_still_resolves_to_tete(self):
        assert _guess_display_unit("poulets", None) == "TETE"


class TestExplicitNonKgDbUnitAlwaysWinsOverAnyGuess:
    def test_an_explicit_non_kg_unit_is_never_overridden_by_the_livestock_guess(self):
        # Comportement PRÉ-EXISTANT inchangé par ce correctif (qui ne touche
        # que le classement livestock par mot-clé, pas cette précédence) :
        # `db_unit="KG"` est traité comme le défaut ambigu et retombe donc
        # sur le classement produit — mais toute AUTRE unité DB explicite
        # (ex: "TETE" déjà posée) reste prioritaire, jamais recalculée.
        assert _guess_display_unit("boeufs", "TETE") == "TETE"
        assert _guess_display_unit("banane", "SAC") == "SAC"


class TestResidualLaitDeVacheStillMisclassifiedDocumented:
    """Non-régression volontaire : ce test documente le résidu CONNU, pas un
    comportement désiré — voir la docstring de ce fichier et
    `docs/domain/COMMERCIAL_QUANTITY_PRICING_MODEL.md`. Il échouerait
    (dans le bon sens) le jour où la config taxonomy est peuplée pour
    "Lait", ce qui serait un progrès, pas une régression — à mettre à jour
    à ce moment-là plutôt que traité comme un test cassé."""

    def test_lait_de_vache_is_still_misclassified_as_livestock_today(self):
        assert _guess_display_unit("lait de vache", None) == "TETE"
