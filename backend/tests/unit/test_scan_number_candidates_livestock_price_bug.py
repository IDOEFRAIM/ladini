"""Incident réel (2026-09-19) — publication d'une offre de vente de bœufs
(PROD_TEST, Bama) :

  1. "l unite coute 495000 fcfa"
     -> récap affiché : "8 UNITE de boeufs à 495 000 FCFA/LITRE" (Bug A :
        un bœuf ne se vend jamais au litre — "LITRE" est un mirage).
  2. "nonnn c est 495000 fcfca par unite" (correction du prix, faute de
     frappe sur "fcfa")
     -> récap affiché : "495 000 UNITE de boeufs à 495 000 FCFA/LITRE"
        (Bug B : la quantité, correcte à 8, est ÉCRASÉE par le montant du
        prix — la correction visait le PRIX, pas la quantité).

Root cause commune : `domain/quantity_unit.py::scan_number_candidates`
(primitive déterministe currency/unit-aware partagée par
`interpreter/routing.py::_interpret_fast_path`) :

  Bug A — `_SCAN_UNIT_RE.search(...)` renvoie le PREMIER match par POSITION,
  pas le plus fiable : sur "l unite coute...", le "l" isolé (symbole du
  litre) matche avant "unite" un peu plus loin. La garde anti-élision déjà
  ajoutée à `extract_unit_only_from_text` (incidents 2026-09-08/2026-09-15)
  n'avait JAMAIS été répliquée ici.

  Bug B — `_SCAN_CURRENCY_RE` exige une correspondance EXACTE de "fcfa"/
  "cfa"/"francs"/"balles" : "fcfca" (faute de frappe, une lettre insérée) ne
  matche pas -> `near_currency=False` -> le nombre est classé QUANTITÉ au
  lieu de PRIX par `interpreter/routing.py::_interpret_fast_path` (voir
  `elif mapped_unit: slot = "quantity"`).

Ce fichier teste la primitive RÉELLE (`scan_number_candidates`) ET, pour le
Bug B, le pipeline de classification complet (`_interpret_fast_path`) sur le
message EXACT de l'incident — pas une copie/simulation."""
from __future__ import annotations

import pytest

from ladini.domain.quantity_unit import (
    _looks_like_fcfa_typo,
    scan_number_candidates,
)
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.interpreter.routing import (
    _interpret_fast_path,
)
from tests.conftest import make_state


class TestBugALivestockNeverGetsLitreFromAnElidedUnite:
    def test_the_exact_incident_message_resolves_to_unite_not_litre(self):
        candidates = scan_number_candidates("l unite coute 495000 fcfa")
        assert len(candidates) == 1
        assert candidates[0].unit == "UNITE"
        assert candidates[0].near_currency is True

    def test_the_same_bug_with_the_apostrophe_present_is_also_fixed(self):
        """Incident sœur (2026-09-08) : "c'est 3000 f l'unité" — apostrophe
        présente cette fois. Doit rester correct après ce correctif."""
        candidates = scan_number_candidates("c'est 3000 f l'unite")
        assert len(candidates) == 1
        # "f" seul n'est pas un synonyme connu (seul "l"/"litre" le sont) —
        # ce qui compte ici est qu'on ne récupère PAS LITRE par erreur.
        assert candidates[0].unit != "LITRE"

    def test_dairy_producer_selling_by_the_litre_is_not_regressed(self):
        """Non-régression de l'incident 2026-08-29 (raison d'être de
        UNIT_SYNONYMS["l"] = "LITRE") : "25 L à 500 fcfa" doit continuer à
        reconnaître LITRE pour la quantité — un vrai symbole de litre COLLÉ
        à un chiffre reste un litre.

        §BUG DE COUVERTURE CORRIGÉ ICI (2026-09-21) : cette assertion ne
        vérifiait JAMAIS QUEL nombre recevait `unit == "LITRE"` — seulement
        que 2 candidats existaient et que le second (le prix) était bien
        `near_currency`. Le vrai bug (25, la quantité, restait `unit=None`,
        et c'est 500, le PRIX, qui récupérait `unit="LITRE"` par erreur —
        voir le correctif de `_find_scan_unit_token` dans
        `domain/quantity_unit.py`, incident réel récurrent) passait donc
        inaperçu malgré ce test "de non-régression" déjà en place, qui
        passait au vert des deux côtés du bug. `candidates[0].unit` est
        désormais vérifié explicitement."""
        candidates = scan_number_candidates("25 L a 500 fcfa")
        assert len(candidates) == 2
        assert candidates[0].value == 25.0
        assert candidates[0].unit == "LITRE", (
            f"la QUANTITÉ (25) doit porter l'unité LITRE, pas {candidates[0].unit!r}"
        )
        assert candidates[1].value == 500.0
        assert candidates[1].near_currency is True


class TestBugCUnitNotAttachedWhenItImmediatelyFollowsTheNumber:
    """Incident réel RÉCURRENT (2026-09-21) — signalé lors d'une session
    précédente, jamais réellement fermé (voir le test de couverture corrigé
    ci-dessus) : un producteur laitier répond "j'ai 600 L de lait... Le
    bidon de 5 L coûte 500 fcfa et celui de 10 L coûte 900 fcfa" pendant la
    collecte de quantité — l'agent accuse réception des tarifs mais
    re-demande la quantité, alors que "600 L" venait d'être donnée.

    Root cause : `_find_scan_unit_token`, appelée sur la fenêtre `after`
    (texte APRÈS le nombre scanné), rejetait TOUJOURS un symbole mono-lettre
    ("L") collé au nombre, car sa garde anti-faux-positif ne regardait QUE
    l'intérieur de cette fenêtre — qui, par construction, exclut le chiffre
    qui vient d'être scanné. "600 L" (nombre puis unité, l'ordre naturel en
    français) ne pouvait donc JAMAIS obtenir `unit="LITRE"` ; l'unité
    "flottait" alors jusqu'au nombre suivant portant une devise à proximité
    (500), un PRIX pris à tort pour un litre."""

    def test_bare_quantity_immediately_followed_by_l_gets_the_unit(self):
        candidates = scan_number_candidates("600 L de lait")
        assert len(candidates) == 1
        assert candidates[0].value == 600.0
        assert candidates[0].unit == "LITRE"
        assert candidates[0].near_currency is False

    def test_the_exact_incident_message_attaches_litre_to_the_bare_quantity_only(self):
        text = (
            "600 L de lait... Le bidon de 5 L coute 500 fcfa et celui de "
            "10 L coute 900 fcfa"
        )
        candidates = scan_number_candidates(text)
        by_value = {c.value: c for c in candidates}

        # La quantité globale (600) : unité reconnue, jamais confondue avec
        # un prix.
        assert by_value[600.0].unit == "LITRE"
        assert by_value[600.0].near_currency is False

        # Les prix (500, 900) restent correctement marqués near_currency —
        # inchangé par ce correctif.
        assert by_value[500.0].near_currency is True
        assert by_value[900.0].near_currency is True

    def test_livestock_single_letter_false_positive_guard_is_not_reopened(self):
        """Garde-fou : ce correctif ne doit PAS réouvrir Bug A (2026-09-19,
        "l unite coute 495000 fcfa" ne doit jamais capter LITRE depuis un
        "l" isolé sans rapport avec un chiffre)."""
        candidates = scan_number_candidates("l unite coute 495000 fcfa")
        assert len(candidates) == 1
        assert candidates[0].unit == "UNITE"


class TestBugBTypoOnFcfaNoLongerMisclassifiesPriceAsQuantity:
    @pytest.mark.parametrize(
        "typo", ["fcfca", "fcaf", "fcfaa", "fcf a".replace(" ", "")]
    )
    def test_common_fcfa_typos_are_still_recognized_as_currency(self, typo):
        candidates = scan_number_candidates(f"495000 {typo} par unite")
        assert candidates[0].near_currency is True

    def test_exact_fcfa_spelling_still_works(self):
        """Non-régression : l'orthographe correcte doit toujours marcher."""
        candidates = scan_number_candidates("495000 fcfa par unite")
        assert candidates[0].near_currency is True

    def test_ca_the_common_french_word_is_not_a_false_positive(self):
        """Garde anti-faux-positif (trouvé PENDANT ce correctif) : "ça" est
        à distance 1 de "cfa" — un mot français très courant ("ça coûte
        cher", "25 sacs, ça part demain") ne doit JAMAIS être pris pour une
        devise."""
        assert _looks_like_fcfa_typo("ca") is False
        candidates = scan_number_candidates("25 sacs ca coute cher")
        prices = [c for c in candidates if c.near_currency]
        assert prices == []

    def test_the_full_interpreter_pipeline_keeps_the_correct_quantity(self):
        """Bout en bout, message EXACT de l'incident (tour 4) : une
        correction de PRIX pendant une CONFIRMATION ne doit jamais écraser
        la quantité déjà connue (8) par le montant du prix (495000)."""
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            normalized_text="nonnn c est 495000 fcfca par unite",
            confirmation_summary=(
                "Vente de 8 UNITE de boeufs a 495 000 FCFA/LITRE."
            ),
            transaction_payload={
                "product": "boeufs",
                "quantity": 8,
                "unit": "UNITE",
                "price": 495000,
                "price_unit": "LITRE",
            },
            **set_pending_interaction(
                InteractionKind.CONFIRM_ACTION, context_ref="confirmation"
            ),
        )
        result = _interpret_fast_path(
            state, "nonnn c est 495000 fcfca par unite"
        )
        assert result is not None, (
            "le message aurait dû être résolu par le fast-path déterministe "
            "de correction chiffrée pendant la confirmation"
        )
        entities = result["extracted_entities"]
        assert entities.get("price") == 495000.0, entities
        assert "quantity" not in entities or entities.get("quantity") != 495000.0, (
            f"la quantité (8) a été écrasée par le montant du prix : {entities!r}"
        )
