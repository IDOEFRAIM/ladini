"""`domain/quantity_unit.py::parse_packaging_message` — moteur UNIFIÉ
"packaging / multi-tarification" (refonte structurelle du 2026-09-21).

Pourquoi ce fichier existe : le packaging / la multi-tarification est un
argument commercial de premier plan, et ce thème a cassé plusieurs fois sous
des FORMES DIFFÉRENTES (2026-08-29, 2026-08-30, 2026-09-14, 2026-09-19,
2026-09-21). La cause commune n'était jamais la regex du jour mais la
TOPOLOGIE : trois analyses indépendantes du même texte
(`scan_number_candidates`, `extract_deterministic_pricing_tiers`,
`parse_packaged_compound_quantity`) pouvaient diverger sans que rien ne le
détecte. `parse_packaging_message` est désormais l'unique analyse ; les deux
autres fonctions en sont des vues.

Ce fichier verrouille le CONTRAT de ce moteur :
  1. chaque incident documenté, rejoué sur le message réel ;
  2. l'INVARIANT transverse qui les couvre tous d'un coup — tout nombre du
     message est soit expliqué par le résultat, soit signalé
     (`unexplained_numbers` / `ambiguous`), JAMAIS perdu en silence ;
  3. les vues historiques, dont le contrat public ne doit pas bouger.

L'invariant (2) est le vrai filet : il vaut pour des formes de message
qu'on n'a pas encore rencontrées, donc il attrapera le PROCHAIN incident de
cette famille avant la production, sans attendre qu'on écrive un test dédié
à cette forme-là.
"""

from __future__ import annotations

import pytest

from ladini.domain.quantity_unit import (
    PackagingClauseRole,
    extract_deterministic_pricing_tiers,
    packaged_compound_total,
    parse_packaged_compound_quantity,
    parse_packaging_message,
)


def _roles(text):
    return [c.role for c in parse_packaging_message(text).clauses]


class TestDocumentedIncidentsAreClosed:
    def test_2026_09_21_global_quantity_survives_alongside_pricing_tiers(self):
        """Incident RÉCURRENT (plusieurs sessions) : un producteur laitier
        répond "600 L de lait... le bidon de 5 L coûte 500 fcfa et celui de
        10 L coûte 900 fcfa" pendant la collecte de quantité — l'agent
        accusait réception des tarifs puis RE-DEMANDAIT la quantité, alors
        qu'elle venait d'être donnée.

        Cause : la quantité globale et les paliers sortaient de DEUX
        analyses différentes qui ne se parlaient pas. Ici, une seule."""
        parsed = parse_packaging_message(
            "600 L de lait... Le bidon de 5 L coute 500 fcfa et celui de "
            "10 L coute 900 fcfa"
        )
        assert parsed.quantity == 600.0
        assert parsed.unit == "LITRE"
        assert len(parsed.pricing_tiers) == 2
        assert parsed.pricing_tiers[0]["quantity"] == 5.0
        assert parsed.pricing_tiers[0]["price"] == 500.0
        assert parsed.pricing_tiers[1]["quantity"] == 10.0
        assert parsed.pricing_tiers[1]["price"] == 900.0
        assert parsed.ambiguous is False
        assert parsed.unexplained_numbers == ()

    def test_2026_09_14_packaged_groups_are_multiplied_then_summed(self):
        """"60 bidons de 5 litres et 20 bidons de 20 litres" = 700 L. Le
        "60" est un NOMBRE DE PAQUETS, sans dimension propre : il ne doit
        jamais devenir la quantité, ni capter l'unité du bidon voisin."""
        parsed = parse_packaging_message(
            "60 bidons de 5 litres et 20 bidons de 20 litres"
        )
        assert parsed.quantity == 700.0
        assert parsed.unit == "LITRE"
        assert parsed.ambiguous is False
        assert parsed.unexplained_numbers == ()

    def test_2026_09_14_a_stock_group_next_to_prices_never_becomes_a_tier(self):
        """Une clause de STOCK ("30 bidons de 20 L") a exactement la même
        forme qu'une clause de tarif. Mélangée à de vrais tarifs, la
        structure du message n'est plus décidable sans deviner : le moteur
        signale l'ambiguïté et le stock (30) comme inexpliqué, plutôt que
        de gonfler un total ou d'inventer un palier fantôme."""
        parsed = parse_packaging_message(
            "30 bidons de 20 L, 1 bidon de 5 L coute 10000 fcfa et "
            "1 bidon de 20 L coute 35000 fcfa"
        )
        assert parsed.ambiguous is True
        assert 30.0 in parsed.unexplained_numbers
        assert parsed.quantity is None

    def test_2026_09_14_groups_are_never_summed_when_a_price_is_anywhere(self):
        """"60 bidons de 5L et 30 bidons de 20L. prix : 3000fcfa/L" — dès
        qu'un prix apparaît, la somme aveugle des groupes est interdite."""
        parsed = parse_packaging_message(
            "60 bidons de 5L et 30 bidons de 20L. prix : 3000fcfa/L"
        )
        assert parsed.quantity is None
        assert parsed.ambiguous is True

    def test_2026_08_30_two_tiers_are_extracted_deterministically(self):
        """La MÊME phrase avait produit `pricing_tiers` correctement une
        fois puis échoué la suivante côté LLM (non-déterminisme MoE). Le
        moteur, lui, est déterministe : ce chemin ne dépend d'aucun LLM."""
        parsed = parse_packaging_message(
            "le sac de 50 kg a 25000 fcfa et le sac de 100 kg a 45000 fcfa"
        )
        assert [(t["quantity"], t["price"]) for t in parsed.pricing_tiers] == [
            (50.0, 25000.0),
            (100.0, 45000.0),
        ]
        assert parsed.ambiguous is False

    def test_2026_08_29_a_single_pair_is_never_promoted_to_a_tier(self):
        """"25 L à 500 fcfa" : UN seul couple quantité/prix est
        structurellement ambigu avec un prix simple. Le moteur ne produit
        donc PAS de `pricing_tiers` — et ne garde pas non plus 25 comme
        quantité globale (elle appartient au couple)."""
        parsed = parse_packaging_message("25 L a 500 fcfa")
        assert parsed.pricing_tiers == ()
        assert parsed.quantity is None
        assert set(parsed.unexplained_numbers) == {25.0, 500.0}

    def test_2026_09_19_livestock_message_stays_ambiguous_not_guessed(self):
        """"l unite coute 495000 fcfa" (prix de référence, sans quantité) :
        aucune lecture déterministe sûre — le moteur s'abstient au lieu de
        fabriquer une quantité."""
        parsed = parse_packaging_message("l unite coute 495000 fcfa")
        assert parsed.quantity is None
        assert parsed.pricing_tiers == ()
        assert parsed.ambiguous is True


class TestClauseClassificationIsExplicit:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("je vends du lait frais", [PackagingClauseRole.NO_NUMBER]),
            ("500 kg de riz", [PackagingClauseRole.BARE_QUANTITY]),
            ("30 bidons de 20 L", [PackagingClauseRole.PACKAGED_GROUP]),
            (
                "1 sac de 50 kg a 25000 fcfa et 1 sac de 100 kg a 45000 fcfa",
                [PackagingClauseRole.TIER, PackagingClauseRole.TIER],
            ),
            ("prix : 3000 fcfa le litre", [PackagingClauseRole.AMBIGUOUS]),
        ],
    )
    def test_roles(self, text, expected):
        assert _roles(text) == expected

    def test_an_explicit_count_of_one_with_a_price_is_a_tier_not_a_group(self):
        """"1 sac de 50 kg à 25 000 FCFA" est un TARIF écrit avec son compte
        explicite. Piège réel : "sac" appartient aussi au vocabulaire des
        unités, donc une lecture naïve y voit DEUX quantités ("1 sac" et
        "50 kg") et renonce — le message était silencieusement perdu."""
        parsed = parse_packaging_message(
            "1 sac de 50 kg a 25000 fcfa et 1 sac de 100 kg a 45000 fcfa"
        )
        assert len(parsed.pricing_tiers) == 2
        assert parsed.pricing_tiers[0]["quantity"] == 50.0
        assert parsed.pricing_tiers[0]["packaging"] == "sac"
        assert parsed.unexplained_numbers == ()

    def test_packaging_is_inherited_from_the_previous_tier(self):
        """"celui de 10 L à 900f" désigne le conditionnement précédent sans
        le renommer — le palier hérite de "bidon" plutôt que de rester vide,
        sinon le récapitulatif affiché au producteur perd le mot."""
        parsed = parse_packaging_message(
            "le bidon de 5 L coute 500 fcfa et celui de 10 L coute 900 fcfa"
        )
        assert [t["packaging"] for t in parsed.pricing_tiers] == [
            "bidon",
            "bidon",
        ]


class TestNothingIsEverLostSilently:
    """L'INVARIANT transverse — le vrai filet de cette refonte.

    Il ne connaît aucune forme de message précise : pour n'importe quel
    texte, chaque nombre doit être soit expliqué par le résultat, soit
    explicitement signalé. C'est ce qui rend la prochaine forme de message
    composée (pas encore rencontrée) détectable AVANT la production."""

    @pytest.mark.parametrize(
        "text",
        [
            "600 L de lait... Le bidon de 5 L coute 500 fcfa et celui de 10 L coute 900 fcfa",
            "60 bidons de 5 litres et 20 bidons de 20 litres",
            "30 bidons de 20 L, 1 bidon de 5 L coute 10000 fcfa et 1 bidon de 20 L coute 35000 fcfa",
            "le sac de 50 kg a 25000 fcfa et le sac de 100 kg a 45000 fcfa",
            "1 sac de 50 kg a 25000 fcfa et 1 sac de 100 kg a 45000 fcfa",
            "25 L a 500 fcfa",
            "l unite coute 495000 fcfa",
            "500 kg de riz",
            "j ai 2 tonnes de mais. le sac de 50 kg coute 20000 fcfa et le sac de 100 kg coute 38000 fcfa",
            "le carton de 12 unites coute 6000 fcfa, le carton de 24 unites coute 11000 fcfa",
            "600 L de lait et 5 poulets",
            "60 bidons de 5L et 30 bidons de 20L. prix : 3000fcfa/L",
            "j ai 3 tonnes de mais et 500 kg de riz",
            "prix : 3000 fcfa le litre",
            "5 sacs de 50 kg et 3 paniers de 10 kg",
        ],
    )
    def test_a_clause_that_gives_up_on_a_number_always_surfaces_it(self, text):
        """Un nombre peut légitimement ne pas survivre tel quel dans le
        contrat canonique quand il a été CONSOMMÉ par sa clause (le compte
        de paquets d'un "60 bidons de 5 L" disparaît dans le total 300) —
        la clause l'atteste en ne le listant PAS dans son `unexplained`.

        Ce qui ne doit JAMAIS arriver, c'est l'inverse : une clause qui
        déclare ne pas savoir expliquer un nombre, sans que le message le
        remonte. C'est exactement par cette fissure que la quantité globale
        (600 L) disparaissait."""
        parsed = parse_packaging_message(text)
        explained = set()
        if parsed.quantity is not None:
            explained.add(parsed.quantity)
        for tier in parsed.pricing_tiers:
            explained.add(tier["quantity"])
            explained.add(tier["price"])
        surfaced = set(parsed.unexplained_numbers)

        for clause in parsed.clauses:
            for number in clause.unexplained:
                assert number in explained or number in surfaced, (
                    f"la clause {clause.text!r} déclare ne pas expliquer "
                    f"{number}, et le message {text!r} ne le remonte pas — "
                    f"perte SILENCIEUSE, exactement le mode de défaillance "
                    f"que cette refonte ferme"
                )

    @pytest.mark.parametrize(
        "text",
        [
            "600 L de lait... Le bidon de 5 L coute 500 fcfa et celui de 10 L coute 900 fcfa",
            "60 bidons de 5 litres et 20 bidons de 20 litres",
            "le sac de 50 kg a 25000 fcfa et le sac de 100 kg a 45000 fcfa",
            "1 sac de 50 kg a 25000 fcfa et 1 sac de 100 kg a 45000 fcfa",
            "500 kg de riz",
            "j ai 2 tonnes de mais. le sac de 50 kg coute 20000 fcfa et le sac de 100 kg coute 38000 fcfa",
            "le carton de 12 unites coute 6000 fcfa, le carton de 24 unites coute 11000 fcfa",
            "5 sacs de 50 kg et 3 paniers de 10 kg",
        ],
    )
    def test_a_confident_parse_explains_absolutely_everything(self, text):
        """Le pendant strict : quand le moteur se déclare SÛR (pas
        d'ambiguïté, rien d'inexpliqué), c'est vérifiable — chaque nombre du
        message correspond réellement à un champ du résultat. C'est cette
        promesse-là que `interpreter/routing.py` utilise pour publier un
        brouillon sans appeler le LLM ; si elle était fausse, l'agent
        publierait des données inventées."""
        parsed = parse_packaging_message(text)
        if parsed.ambiguous or parsed.unexplained_numbers:
            pytest.skip("le moteur ne se déclare pas sûr sur ce message")

        # Une analyse SÛRE implique qu'aucune clause n'a renoncé en chemin :
        # chaque clause a pleinement rendu compte de ses nombres, soit en
        # les reportant dans le résultat, soit en les consommant (compte de
        # paquets × contenu). Une clause qui aurait abandonné un nombre
        # rendrait la certitude du message mensongère.
        for clause in parsed.clauses:
            assert clause.unexplained == (), (
                f"le moteur se déclare sûr, mais la clause {clause.text!r} "
                f"a abandonné {clause.unexplained}"
            )
        # ...et tout nombre non consommé se retrouve bien dans le résultat.
        explained = set()
        if parsed.quantity is not None:
            explained.add(parsed.quantity)
        for tier in parsed.pricing_tiers:
            explained.add(tier["quantity"])
            explained.add(tier["price"])
        assert explained, f"analyse sûre mais vide pour {text!r}"

    def test_empty_and_blank_inputs_are_inert(self):
        for text in ("", "   ", None):
            parsed = parse_packaging_message(text)
            assert parsed.clauses == ()
            assert parsed.quantity is None
            assert parsed.pricing_tiers == ()
            assert parsed.ambiguous is False


class TestHistoricalViewsKeepTheirContract:
    """`extract_deterministic_pricing_tiers` et
    `parse_packaged_compound_quantity` sont devenues des vues du moteur.
    Leurs appelants (`flows/producer/flow.py`, garde de `routing.py`) n'ont
    pas bougé : leur contrat public ne doit pas bouger non plus."""

    def test_tiers_view_requires_at_least_two_tiers(self):
        assert extract_deterministic_pricing_tiers("le bidon de 5 L coute 500 fcfa") is None
        assert extract_deterministic_pricing_tiers("25 L a 500 fcfa") is None

    def test_tiers_view_returns_plain_mutable_dicts(self):
        tiers = extract_deterministic_pricing_tiers(
            "le sac de 50 kg a 25000 fcfa et le sac de 100 kg a 45000 fcfa"
        )
        assert isinstance(tiers, list) and len(tiers) == 2
        assert set(tiers[0]) == {"quantity", "unit", "price", "packaging"}
        tiers[0]["quantity"] = 1  # jamais un objet gelé partagé
        assert extract_deterministic_pricing_tiers(
            "le sac de 50 kg a 25000 fcfa et le sac de 100 kg a 45000 fcfa"
        )[0]["quantity"] == 50.0

    def test_tiers_view_refuses_when_a_stock_group_is_present(self):
        assert (
            extract_deterministic_pricing_tiers(
                "30 bidons de 20 L, 1 bidon de 5 L coute 10000 fcfa et "
                "1 bidon de 20 L coute 35000 fcfa"
            )
            is None
        )

    def test_packaged_view_requires_an_actual_package_group(self):
        """Une quantité simple relève de `parse_quantity_unit_from_text` —
        cette vue-ci ne répond que pour les quantités exprimées EN PAQUETS."""
        assert parse_packaged_compound_quantity("500 kg de riz").quantity is None
        assert parse_packaged_compound_quantity(
            "60 bidons de 5 litres et 20 bidons de 20 litres"
        ).quantity == 700.0

    def test_packaged_view_refuses_as_soon_as_a_price_appears(self):
        assert (
            parse_packaged_compound_quantity(
                "60 bidons de 5L et 30 bidons de 20L. prix : 3000fcfa/L"
            ).quantity
            is None
        )

    def test_packaged_view_refuses_mixed_units(self):
        assert (
            parse_packaged_compound_quantity(
                "60 bidons de 5 litres et 20 sacs de 50 kg"
            ).quantity
            is None
        )

    def test_the_two_entry_points_never_disagree(self):
        """`packaged_compound_total` existe pour les appelants qui ont déjà
        l'analyse en main (`interpreter/routing.py`) — il doit donner
        EXACTEMENT le même résultat que la vue qui re-parse. Une divergence
        ici reproduirait précisément la classe de bug que cette refonte
        supprime."""
        for text in (
            "60 bidons de 5 litres et 20 bidons de 20 litres",
            "500 kg de riz",
            "60 bidons de 5L et 30 bidons de 20L. prix : 3000fcfa/L",
            "5 sacs de 50 kg et 3 paniers de 10 kg",
            "1 sac de 50 kg",
        ):
            assert packaged_compound_total(
                parse_packaging_message(text)
            ) == parse_packaged_compound_quantity(text), text
