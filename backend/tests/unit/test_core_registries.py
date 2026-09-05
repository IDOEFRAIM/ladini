"""Registres centraux : slots, unités, goals, réducteurs d'état.

Ces modules sont la SOURCE UNIQUE de vérité du graphe. Chaque duplication
qu'on y réintroduirait provoque des bugs « par nœud » très difficiles à tracer
(un champ canonicalisé ici mais pas là). Ces tests verrouillent l'unicité.
"""
from __future__ import annotations

import pytest

from agriconnect.graphs.agents.market_coach.core.slots import (
    SLOT_REGISTRY,
    SLOT_FILLING_INPUTS,
    resolve_canonical,
    build_canonical_field_aliases,
    build_remap_dict,
    build_alias_mirrors,
    expected_input_for_field,
    is_blocking_slot,
)
from agriconnect.domain.quantity_unit import (
    UNIT_SYNONYMS,
    normalize_unit,
    parse_quantity_unit_from_text,
    parse_compound_quantity,
    extract_unit_only_from_text,
    scan_number_candidates,
    is_livestock_product,
    default_unit_for_product,
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
        from agriconnect.graphs.agents.market_coach.utils import _CANONICAL_FIELD_ALIASES
        assert _CANONICAL_FIELD_ALIASES == build_canonical_field_aliases()

    def test_slot_filling_inputs_derived_from_expected_input_map(self):
        """L'ensemble des slots « demandables » est dérivé, jamais recopié."""
        assert "FARM_NAME" in SLOT_FILLING_INPUTS, "FARM_NAME doit être un slot demandable"
        assert "DATE" in SLOT_FILLING_INPUTS
        assert "SELECTION" not in SLOT_FILLING_INPUTS, "SELECTION n'est pas un champ métier"

    def test_tunnel_soft_inputs_include_all_slot_fields(self):
        """tunnel_manager doit dériver du registre, sinon un slot devient
        non-interruptible sans qu'on s'en aperçoive."""
        from agriconnect.graphs.agents.market_coach.core.tunnel_manager import SOFT_EXPECTED_INPUTS
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
        assert valid == {"KG", "TONNE", "SAC", "PANIER", "TETE", "UNITE", "LITRE"}

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
