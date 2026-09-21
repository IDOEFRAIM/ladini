"""`domain/quantity_unit.py::all_numbers_accounted_for` — filet de sécurité
GÉNÉRIQUE contre la perte silencieuse d'un nombre par un fast-path
déterministe de quantité/prix/tarifs (2026-09-21, correctif STRUCTUREL).

Contexte : plusieurs incidents empilés sur le même thème ("600 L de lait...
le bidon de 5 L coûte 500 fcfa...", quantité globale 600 perdue par le
fast-path `pricing_tiers`) ont révélé que `scan_number_candidates` (fenêtre
glissante) et `extract_deterministic_pricing_tiers`/
`parse_packaged_compound_quantity` (découpage en clauses) sont deux analyses
INDÉPENDANTES du même texte, sans aucun recoupement — rien ne garantissait
qu'elles restent d'accord. Plutôt que de continuer à rustiner CHAQUE
nouvelle forme de message composé une par une, cette fonction ajoute un
invariant structurel : tout résultat déterministe doit expliquer CHAQUE
nombre du message, sinon il est considéré incomplet — voir son usage dans
`interpreter/routing.py::_interpret_fast_path` (fast-path
`fast_path_deterministic_pricing_tiers`)."""

from __future__ import annotations

from ladini.domain.quantity_unit import all_numbers_accounted_for


class TestCompleteExtractions:
    def test_bare_quantity_plus_two_tiers_all_explained(self):
        values = [600.0, 5.0, 500.0, 10.0, 900.0]
        entities = {
            "quantity": 600.0,
            "pricing_tiers": [
                {"quantity": 5.0, "price": 500.0},
                {"quantity": 10.0, "price": 900.0},
            ],
        }
        assert all_numbers_accounted_for(values, entities) is True

    def test_tiers_only_no_separate_bare_quantity(self):
        values = [5.0, 500.0, 10.0, 900.0]
        entities = {
            "pricing_tiers": [
                {"quantity": 5.0, "price": 500.0},
                {"quantity": 10.0, "price": 900.0},
            ],
        }
        assert all_numbers_accounted_for(values, entities) is True

    def test_simple_quantity_and_price_pair(self):
        assert all_numbers_accounted_for(
            [775.0, 175.0], {"quantity": 775.0, "price": 175.0}
        ) is True

    def test_empty_values_are_trivially_complete(self):
        assert all_numbers_accounted_for([], {}) is True

    def test_float_precision_does_not_cause_false_negatives(self):
        # Une valeur qui traverse un arrondi/une conversion (ex: 500 vs
        # 500.0000001) ne doit pas déclencher un faux "nombre manquant".
        values = [500.0000001]
        entities = {"price": 500.0}
        assert all_numbers_accounted_for(values, entities) is True


class TestIncompleteExtractionsAreDetected:
    def test_the_exact_old_bug_is_caught_quantity_dropped(self):
        # Reproduit EXACTEMENT le résultat que le fast-path
        # `fast_path_deterministic_pricing_tiers` renvoyait AVANT le
        # correctif du 2026-09-21 : `pricing_tiers` seul, la quantité
        # globale (600) absente du résultat — ce garde doit le détecter.
        values = [600.0, 5.0, 500.0, 10.0, 900.0]
        entities = {
            "pricing_tiers": [
                {"quantity": 5.0, "price": 500.0},
                {"quantity": 10.0, "price": 900.0},
            ],
        }
        assert all_numbers_accounted_for(values, entities) is False

    def test_a_missing_price_is_also_detected(self):
        values = [775.0, 175.0]
        entities = {"quantity": 775.0}  # price manquant
        assert all_numbers_accounted_for(values, entities) is False

    def test_a_missing_tier_value_is_detected(self):
        values = [5.0, 500.0, 10.0, 900.0]
        entities = {
            "pricing_tiers": [
                {"quantity": 5.0, "price": 500.0},
                # 2e palier tronqué : "price" manquant.
                {"quantity": 10.0},
            ],
        }
        assert all_numbers_accounted_for(values, entities) is False

    def test_a_future_unknown_composite_shape_is_also_caught(self):
        # Le point de CE garde : il n'a besoin de connaître AUCUNE forme
        # de message précise pour détecter une perte — un nombre totalement
        # sans rapport avec le vocabulaire actuel (quantity/price/
        # pricing_tiers) suffit à le déclencher.
        values = [42.0, 100.0, 999.0]
        entities = {"quantity": 42.0, "price": 100.0}
        assert all_numbers_accounted_for(values, entities) is False


class TestPackagingCounterToleranceIsDocumentedNotAutomatic:
    def test_a_packaging_multiplier_not_removed_by_the_caller_fails_the_guard(self):
        # "60 bidons de 5 L" -> quantité totale 300 (60×5) — ni le COMPTE de
        # paquets (60) ni le contenu par paquet (5) n'apparaissent dans le
        # contrat canonique, seul le TOTAL (300) y figure. Ce garde ne
        # connaît PAS cette règle métier de multiplication — c'est à
        # l'APPELANT (voir docstring de la fonction) de ne passer QUE les
        # nombres censés survivre tels quels dans le résultat (ici,
        # uniquement 300) avant l'appel s'il utilise ce type de parseur.
        # Verrouille explicitement cette responsabilité plutôt que de la
        # laisser implicite.
        values = [60.0, 5.0, 300.0]
        entities = {"quantity": 300.0}
        assert all_numbers_accounted_for(values, entities) is False
        # Une fois SEUL le total passé par l'appelant (responsabilité documentée) :
        assert all_numbers_accounted_for([300.0], entities) is True
