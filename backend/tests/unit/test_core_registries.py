"""Registres centraux : slots, unités, goals, réducteurs d'état.

Ces modules sont la SOURCE UNIQUE de vérité du graphe. Chaque duplication
qu'on y réintroduirait provoque des bugs « par nœud » très difficiles à tracer
(un champ canonicalisé ici mais pas là). Ces tests verrouillent l'unicité.
"""
from __future__ import annotations

import pytest

from ladini.domain.quantity_unit import (
    UNIT_SYNONYMS,
    default_unit_for_product,
    extract_unit_only_from_text,
    is_livestock_product,
    normalize_unit,
    parse_compound_quantity,
    parse_quantity_unit_from_text,
    resolve_product_unit,
    scan_number_candidates,
)
from ladini.graphs.agents.market_coach.core.slots import (
    SLOT_FILLING_INPUTS,
    SLOT_REGISTRY,
    build_alias_mirrors,
    build_canonical_field_aliases,
    build_remap_dict,
    expected_input_for_field,
    get_slot,
    is_blocking_slot,
    resolve_canonical,
)

# =====================================================================
# REGISTRE DE SLOTS — source unique alias -> canonique
# =====================================================================

class TestSlotRegistry:
    def test_canonical_names_are_unique(self):
        names = [s.canonical for s in SLOT_REGISTRY]
        assert len(names) == len(set(names)), "canoniques dupliqués dans SLOT_REGISTRY"

    def test_no_alias_collides_with_another_canonical(self):
        canon = {s.canonical for s in SLOT_REGISTRY}
        for slot in SLOT_REGISTRY:
            for alias in slot.aliases:
                assert alias not in (canon - {slot.canonical}), (
                    f"l'alias '{alias}' de '{slot.canonical}' est le canonique d'un autre slot"
                )

    def test_no_alias_shared_between_two_slots(self):
        seen: dict[str, str] = {}
        for slot in SLOT_REGISTRY:
            for alias in slot.aliases:
                assert alias not in seen, (
                    f"alias '{alias}' partagé par '{seen.get(alias)}' et '{slot.canonical}'"
                )
                seen[alias] = slot.canonical

    @pytest.mark.parametrize("alias,expected", [
        ("product_name", "product"), ("produit", "product"), ("culture", "product"),
        ("quantite", "quantity"), ("quantity_kg", "quantity"), ("original_quantity", "quantity"),
        ("prix", "price"), ("offered_price", "price"), ("montant_enchere", "price"),
        ("unite", "unit"), ("original_unit", "unit"),
        ("region", "zone"), ("target_zone", "zone"), ("localite", "zone"),
    ])
    def test_alias_resolves_to_canonical(self, alias, expected):
        assert resolve_canonical(alias) == expected

    def test_unknown_key_passes_through_unchanged(self):
        assert resolve_canonical("champ_inconnu_xyz") == "champ_inconnu_xyz"

    def test_derived_tables_stay_consistent(self):
        """Les 3 tables dérivées doivent rester cohérentes entre elles."""
        remap = build_remap_dict()
        aliases = build_canonical_field_aliases()
        assert remap == aliases, "build_remap_dict et build_canonical_field_aliases ont divergé"
        mirrors = build_alias_mirrors()
        for canonical, alias_tuple in mirrors.items():
            for alias in alias_tuple:
                assert remap[alias] == canonical

    def test_utils_alias_table_is_derived_not_duplicated(self):
        """`normalize_slot_keys` DOIT dériver du registre (régression 2026-08).

        Une table recopiée à la main dans utils.py avait divergé : certains
        champs étaient canonicalisés par memory/routing mais PAS par
        validation/response.
        """
        from ladini.graphs.agents.market_coach.utils import _CANONICAL_FIELD_ALIASES
        assert _CANONICAL_FIELD_ALIASES == build_canonical_field_aliases()

    def test_slot_filling_inputs_derived_from_expected_input_map(self):
        """L'ensemble des slots « demandables » est dérivé, jamais recopié."""
        assert "FARM_NAME" in SLOT_FILLING_INPUTS, "FARM_NAME doit être un slot demandable"
        assert "DATE" in SLOT_FILLING_INPUTS
        assert "SELECTION" not in SLOT_FILLING_INPUTS, "SELECTION n'est pas un champ métier"

    def test_tunnel_soft_inputs_include_all_slot_fields(self):
        """tunnel_manager doit dériver du registre, sinon un slot devient
        non-interruptible sans qu'on s'en aperçoive."""
        from ladini.graphs.agents.market_coach.core.tunnel_manager import (
            SOFT_EXPECTED_INPUTS,
        )
        assert SLOT_FILLING_INPUTS <= SOFT_EXPECTED_INPUTS

    def test_only_true_secrets_are_blocking(self):
        assert is_blocking_slot("CONFIRMATION") is True
        assert is_blocking_slot("otp_code") is True
        assert is_blocking_slot("product") is False
        assert is_blocking_slot("quantity") is False

    def test_expected_input_for_unknown_field_is_none_token(self):
        assert expected_input_for_field("champ_inexistant") == "NONE"


# =====================================================================
# UNITÉS / QUANTITÉS — source unique de parsing
# =====================================================================

class TestQuantityUnit:
    @pytest.mark.parametrize("raw,expected", [
        ("kg", "KG"), ("KG", "KG"), ("kilo", "KG"), ("kilos", "KG"), ("kilogrammes", "KG"),
        ("tonne", "TONNE"), ("tonnes", "TONNE"), ("t", "TONNE"),
        ("sac", "SAC"), ("sacs", "SAC"), ("sachet", "SAC"),
        ("panier", "PANIER"), ("tete", "TETE"), ("unite", "UNITE"),
    ])
    def test_unit_synonyms_normalize(self, raw, expected):
        assert normalize_unit(raw) == expected

    def test_unknown_unit_returns_none(self):
        assert normalize_unit("brouettes") is None

    def test_unit_synonyms_map_only_to_valid_units(self):
        valid = set(UNIT_SYNONYMS.values())
        # (2026-09-30, Étape 6 — centralisation des unités) : GRAMME/MILLILITRE/
        # CENTILITRE/DECILITRE ajoutés au registre central — voir quantity_unit.py.
        # QUINTAL ajouté lors de la clôture de l'Étape 6 (2026-09-30) : n'était
        # reconnu qu'par `actions/common.py::_UNIT_TO_KG`, invisible ici.
        assert valid == {
            "KG", "TONNE", "SAC", "PANIER", "TETE", "UNITE", "LITRE",
            "GRAMME", "MILLILITRE", "CENTILITRE", "DECILITRE", "QUINTAL",
        }

    @pytest.mark.parametrize("text,qty,unit", [
        ("200 kg", 200.0, "KG"),
        ("200kg", 200.0, "KG"),          # collé (saisie mobile)
        ("12,5 kg", 12.5, "KG"),         # virgule décimale FR
        ("1 000 kg", 1000.0, "KG"),      # espace milliers
        ("3 tonnes", 3.0, "TONNE"),
        ("50 sacs", 50.0, "SAC"),
    ])
    def test_parse_quantity_unit(self, text, qty, unit):
        r = parse_quantity_unit_from_text(text)
        assert r.quantity == qty and r.unit == unit

    def test_parse_empty_text_is_falsy(self):
        assert not parse_quantity_unit_from_text("")

    def test_compound_quantity_sums_convertible_units(self):
        r = parse_compound_quantity("2 tonnes et 375 kg")
        assert r.unit == "KG" and r.quantity == pytest.approx(2375.0)

    def test_unit_only_detection_anywhere_in_text(self):
        assert extract_unit_only_from_text("je vends 200 en sacs") == "SAC"
        assert extract_unit_only_from_text("deux cents kilos") == "KG"
        assert extract_unit_only_from_text("aucune unite ici") in (None, "UNITE")

    def test_livestock_defaults_to_head_not_kilograms(self):
        """Régression : « 300 poussins » devenait « 300 KG »."""
        for animal in ("poussins", "moutons", "boeufs", "bœufs", "chèvres", "poules"):
            assert is_livestock_product(animal) is True, animal
            assert default_unit_for_product(animal) == "TETE"

    def test_crops_keep_kilogram_default(self):
        for crop in ("tomates", "maïs", "riz", "oignons"):
            assert is_livestock_product(crop) is False
            assert default_unit_for_product(crop) == "KG"

    def test_an_elided_article_is_never_read_as_a_unit(self):
        """Incident réel (2026-09-08) : « c'est 3000 f l'unité » (= prix À LA
        PIÈCE) était lu comme `unit=LITRE`. L'apostrophe n'étant pas une
        frontière de mot pour la regex, « l'unité » produit deux tokens, et
        « l » isolé vaut LITRE dans la table de synonymes. Même piège pour
        « t' » (= TONNE)."""
        assert extract_unit_only_from_text("c'est 3000 f l'unite") != "LITRE"
        assert extract_unit_only_from_text("c'est 3000 f l'unité") != "LITRE"
        assert extract_unit_only_from_text("t'as combien") != "TONNE"
        # Apostrophe typographique aussi.
        assert extract_unit_only_from_text("3000 f l’unité") != "LITRE"

    def test_an_elided_article_missing_its_apostrophe_is_never_read_as_a_unit(self):
        """Incident réel (2026-09-15) : « je veux vendre mes 25 boeufs. L
        unite coute 425000 fcfa » (= « l'unité coûte… », apostrophe
        simplement omise — faute de frappe WhatsApp courante) était lu
        comme `unit=LITRE` pour un producteur vendant des BOEUFS — il n'y a
        ici même pas d'apostrophe à détecter par le garde précédent, juste
        un espace entre « L » et « unite ».

        Corrigé par une règle GÉNÉRALE (jamais une liste de mots comme
        « unité ») : un symbole d'une seule lettre (l/t/k) n'est retenu que
        s'il est collé à un CHIFFRE — exactement comme un vrai symbole de
        mesure s'écrit toujours ("25 L", "10T"). Ça couvre n'importe quel
        article élidé sans apostrophe, pas seulement « l'unité » : « l'année »,
        « t'inquiète »… peu importe le mot qui suit "l"/"t"."""
        assert (
            extract_unit_only_from_text(
                "je veux vendre mes 25 boeufs. L unite coute 425000 fcfa"
            )
            != "LITRE"
        )
        assert extract_unit_only_from_text("L unite coute 425000 fcfa") != "LITRE"
        assert extract_unit_only_from_text("T unite coute 500") != "TONNE"
        # Généralisation : PAS de "unité" du tout, n'importe quel mot après
        # l'article élidé sans apostrophe doit être écarté de la même façon.
        assert (
            extract_unit_only_from_text(
                "12 boeufs. L annee derniere ca coutait moins cher"
            )
            != "LITRE"
        )
        assert extract_unit_only_from_text("T inquiete pas je vends 12 moutons") != "TONNE"
        # Non-régression : un vrai symbole collé à sa quantité reste détecté.
        assert extract_unit_only_from_text("12 T de mais") == "TONNE"

    def test_a_real_litre_is_still_detected(self):
        """Non-régression : le garde d'élision ne doit pas rendre le litre
        indétectable — c'est un vrai symbole d'unité en usage (lait)."""
        assert extract_unit_only_from_text("je vends 25 l de lait") == "LITRE"
        assert extract_unit_only_from_text("des bidons de 10 litres") == "LITRE"


class TestUnitAuthority:
    """`resolve_product_unit` — autorité UNIQUE sur « quelle unité pour ce
    produit ».

    Problème général qu'elle supprime (incident réel 2026-09-08, « Vente de
    6500 KG de poulets ») : l'unité était décidée par cinq écrivains
    successifs, chacun gardé par un `if unité est vide`. La précédence réelle
    était donc « le premier qui écrit gagne » — l'ordre d'exécution dans le
    graphe, pas la fiabilité de la source. Un défaut aveugle « KG » battait
    ainsi définitivement la nature du produit."""

    def test_a_mass_unit_on_livestock_is_corrected_not_preserved(self):
        """LE cas du bug : un KG déjà posé sur des poulets doit être CORRIGÉ.
        Un animal vivant ne se pèse pas au kilo pour être vendu à la pièce."""
        assert resolve_product_unit("poulets", current_unit="KG") == "TETE"
        assert resolve_product_unit("moutons", current_unit="TONNE") == "TETE"

    def test_a_plausible_unit_on_livestock_is_left_alone(self):
        """La correction ne vise QUE la contradiction physique (masse/volume),
        pas toute unité inattendue : « 20 sacs de poussins » reste du domaine
        du possible, ce n'est pas à cette fonction d'en juger."""
        assert resolve_product_unit("poulets", current_unit="SAC") == "SAC"

    def test_a_volume_unit_on_livestock_is_also_corrected(self):
        """Incident réel (2026-09-15) : « Vente de 49 LITRE de chèvres » —
        un LITRE posé sur un élevage (via le bug d'élision « l'unité » sans
        apostrophe, corrigé par ailleurs, ou toute autre source) restait
        collé DÉFINITIVEMENT, puisque la règle 2 ne couvrait que KG/TONNE
        (`_UNIT_TO_KG`) et LITRE n'a pas d'équivalent kg. Un animal vivant
        n'est pas plus mesurable en litres qu'en kilos — même correction
        que pour la masse."""
        assert resolve_product_unit("chèvres", current_unit="LITRE") == "TETE"
        assert resolve_product_unit("boeufs", current_unit="LITRE") == "TETE"

    def test_user_written_unit_always_wins(self):
        assert resolve_product_unit("poulets", current_unit="KG", text_unit="sacs") == "SAC"
        assert resolve_product_unit("mais", current_unit="KG", text_unit="tonnes") == "TONNE"

    def test_livestock_default_when_nothing_is_known(self):
        assert resolve_product_unit("poussins") == "TETE"

    def test_crops_are_untouched(self):
        """Non-régression : rien ne change pour une culture — ni correction,
        ni défaut inventé (c'est au validateur de demander/défaulter)."""
        assert resolve_product_unit("mais", current_unit="KG") == "KG"
        assert resolve_product_unit("tomates", current_unit="TONNE") == "TONNE"
        assert resolve_product_unit("tomates") is None

    def test_an_unknown_unit_is_preserved_never_destroyed(self):
        """Une unité hors registre est une donnée utilisateur qu'on ne sait
        pas interpréter — jamais une raison de l'effacer."""
        assert resolve_product_unit("tomates", current_unit="CAGEOT") == "CAGEOT"


class TestUnitAuthorityCategoryConfig:
    """(2026-09-19, retour produit) — `category_config` : l'admin a
    configuré un ENSEMBLE d'unités autorisées + une unité PRIORITAIRE pour
    standardiser (ex: lait -> LITRE seul ; maïs -> G/KG/TONNE/SAC avec
    priorité). Quand elle est fournie, elle prime sur TOUT le reste — c'est
    la définition même de « standardiser » ; quand elle est absente/None
    (comportement réel aujourd'hui, tant que le site n'a rien configuré),
    `resolve_product_unit` retombe EXACTEMENT sur les règles 1-4
    historiques (`TestUnitAuthority` ci-dessus), zéro régression."""

    def test_text_unit_outside_allowed_set_is_overridden_by_priority(self):
        """Le producteur écrit une unité qui n'est PAS dans l'ensemble admin
        -> on standardise sur `priority_unit`, on ne suit pas le texte."""
        config = {"priority_unit": "LITRE", "allowed_units": ["LITRE"]}
        assert (
            resolve_product_unit(
                "lait", current_unit=None, text_unit="kg", category_config=config
            )
            == "LITRE"
        )

    def test_text_unit_inside_allowed_set_is_honored(self):
        config = {
            "priority_unit": "TONNE",
            "allowed_units": ["G", "KG", "TONNE", "SAC"],
        }
        assert (
            resolve_product_unit(
                "mais", current_unit=None, text_unit="sacs", category_config=config
            )
            == "SAC"
        )

    def test_current_unit_outside_allowed_set_is_corrected_to_priority(self):
        """Une unité déjà posée (tour précédent) hors de l'ensemble admin
        doit être corrigée, pas simplement conservée."""
        config = {"priority_unit": "TETE", "allowed_units": ["TETE", "UNITE"]}
        assert (
            resolve_product_unit(
                "boeufs", current_unit="LITRE", category_config=config
            )
            == "TETE"
        )

    def test_current_unit_inside_allowed_set_is_kept_over_priority(self):
        config = {"priority_unit": "TETE", "allowed_units": ["TETE", "UNITE"]}
        assert (
            resolve_product_unit(
                "boeufs", current_unit="UNITE", category_config=config
            )
            == "UNITE"
        )

    def test_nothing_known_falls_back_to_priority_unit(self):
        config = {"priority_unit": "LITRE", "allowed_units": ["LITRE"]}
        assert (
            resolve_product_unit("lait", category_config=config) == "LITRE"
        )

    def test_none_config_is_the_default_and_changes_nothing(self):
        """Non-régression explicite : `category_config=None` (valeur par
        défaut du paramètre) doit produire EXACTEMENT le même résultat que
        sans le paramètre du tout."""
        assert resolve_product_unit(
            "poulets", current_unit="KG", category_config=None
        ) == resolve_product_unit("poulets", current_unit="KG")

    def test_malformed_config_degrades_to_historical_rules_never_raises(self):
        """Une config mal formée (venant d'un futur outil MCP, donc pas sous
        notre contrôle) ne doit jamais faire planter la résolution d'unité —
        seulement se dégrader au comportement historique."""
        assert resolve_product_unit(
            "poulets", current_unit="KG", category_config={}
        ) == "TETE"
        assert resolve_product_unit(
            "poulets", current_unit="KG", category_config={"allowed_units": []}
        ) == "TETE"
        assert resolve_product_unit(
            "poulets",
            current_unit="KG",
            category_config={"priority_unit": "LITRE", "allowed_units": ["LITRE"]},
        ) == "LITRE"
        # unité prioritaire incohérente (absente de l'ensemble autorisé) :
        # ignorée, mais l'ensemble reste exploitable si non-ambigu (1 seule
        # unité) ; sinon repli complet sur les règles 1-4.
        assert resolve_product_unit(
            "poulets",
            current_unit="KG",
            category_config={"priority_unit": "SAC", "allowed_units": ["TETE"]},
        ) == "TETE"


class TestUnitSlotDefaultIsProductAware:
    """Le défaut du slot `unit` était la constante « KG », posée sans aucune
    preuve — c'est elle qui gagnait ensuite contre la nature du produit."""

    def test_livestock_defaults_to_head(self):
        slot = get_slot("unit")
        assert slot.default_value is None, "plus de constante aveugle"
        assert slot.default_factory({"product": "poulets"}) == "TETE"

    def test_crop_default_is_unchanged(self):
        slot = get_slot("unit")
        assert slot.default_factory({"product": "maïs"}) == "KG"
        assert slot.default_factory({}) == "KG"


class TestNumberScanner:
    """`scan_number_candidates` : primitive PARTAGÉE (interpréteur fast-path).

    Elle classe CHAQUE nombre par son voisinage — c'est ce qui permet de
    désambiguïser « 775 kg ... 175 fcfa » en quantité + prix.
    """

    def test_tags_quantity_and_price_independently(self):
        cands = scan_number_candidates("j ai 775 kg d oignon et le kg coute 175 fcfa")
        assert len(cands) == 2
        qty = [c for c in cands if c.unit and not c.near_currency]
        price = [c for c in cands if c.near_currency]
        assert qty and qty[0].value == 775.0 and qty[0].unit == "KG"
        assert price and price[0].value == 175.0

    def test_keeps_unit_even_next_to_currency(self):
        """« 10000fcfa/kg » doit conserver KG pour renseigner price_unit."""
        cands = scan_number_candidates("10000fcfa/kg")
        assert len(cands) == 1
        assert cands[0].near_currency is True
        assert cands[0].unit == "KG"

    def test_bare_number_has_no_unit_no_currency(self):
        cands = scan_number_candidates("environ 300")
        assert len(cands) == 1
        assert cands[0].unit is None and cands[0].near_currency is False

    def test_no_number_returns_empty(self):
        assert scan_number_candidates("je ne sais pas") == []
