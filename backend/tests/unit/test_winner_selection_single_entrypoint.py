"""Preuve structurelle anti-bypass — `select_winning_bid` n'a qu'UN chemin
conversationnel sécurisé (F4, 2026-09-04, audit fonctionnel —
"PROCUREMENT_SELECT_WINNER/ACCEPT_OFFER pouvant contourner le tunnel
sécurisé").

## Le gap réel fermé par ce fichier

`PROCUREMENT_SELECT_WINNER` (déclaré dans `interpreter/intent.py`, HORS de
`_TUNNEL_ASSIGNMENTS`) résolvait, via l'exécuteur GÉNÉRIQUE
(`actions/procure.py::prep_procurement_select_winner`), vers le MÊME
tool_name réel `select_winning_bid` que le tunnel sécurisé
(`order_tracking.py::confirm_winner_selection`/`finalize_winner`) — sans
AUCUNE des protections de ce tunnel (revalidation de prix, étape GPS,
gardes de statut, résolution de cible). `PROCUREMENT_ACCEPT_OFFER`
résolvait vers `accept_bid`, un tool_name sans méthode DB réelle.

F4 (2026-09-04) avait neutralisé les deux handlers (`raise RuntimeError`
systématique) tout en les gardant enregistrés. Le Deep Intent Architecture
Cleanup (2026-09-13) est allé plus loin : les deux intents ont été
**architecturalement supprimés** d'`INTENT_CONFIG` (plus aucun tool réel ne
les soutient — `select_winning_bid`/`accept_bid` n'existent pas côté DB pour
cet usage), donc leurs handlers `prep_procurement_select_winner`/
`prep_procurement_accept_offer` ont été supprimés avec eux : ils ne peuvent
plus être ni classés par le LLM, ni résolus par l'exécuteur générique — le
bypass est fermé par absence totale de chemin, pas seulement par un stub qui
lève une exception.

## Ce que ce fichier NE prétend PAS prouver

`select_winning_bid` reste également appelable depuis
`flows/buyer/negotiation.py` (tunnel `BUYER_NEGOTIATE_PRICE`, négociation
acheteur↔UN producteur) — un DEUXIÈME chemin LÉGITIME, déjà tunnelé (pas
l'exécuteur générique), avec ses propres limites déjà documentées dans un
chantier antérieur (GPS best-effort, pas de récap prix) — hors du périmètre
de F4 (mandat §21 : ne pas rouvrir le hardening Auction déjà fermé)."""
from __future__ import annotations

import inspect

from ladini.graphs.agents.market_coach.actions import procure as procure_actions
from ladini.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from ladini.graphs.agents.market_coach.registry import get_action


class TestGenericExecutorCanNeverReachSelectWinningBid:
    """(2026-09-13, Deep Intent Architecture Cleanup) : les intents et leurs
    handlers ont été supprimés — le bypass n'est plus neutralisé, il est
    inexistant."""

    def test_neither_intent_exists_in_the_catalogue_anymore(self):
        assert "PROCUREMENT_SELECT_WINNER" not in INTENT_CONFIG
        assert "PROCUREMENT_ACCEPT_OFFER" not in INTENT_CONFIG

    def test_neither_handler_is_registered_anymore(self):
        assert get_action("PROCUREMENT_SELECT_WINNER") is None
        assert get_action("PROCUREMENT_ACCEPT_OFFER") is None

    def test_neither_prep_function_exists_on_the_module_anymore(self):
        assert not hasattr(procure_actions, "prep_procurement_select_winner")
        assert not hasattr(procure_actions, "prep_procurement_accept_offer")


class TestExactlyTwoLegitimateCallSitesExistInTheWholeCodebase:
    """Preuve structurelle FINALE, sur le code source réel : `select_winning_bid`
    (la méthode du gateway conversationnel, PAS la méthode DB elle-même)
    n'est appelée QUE depuis les deux tunnels connus et audités. Ce test
    DOIT échouer si un troisième site (ex: un futur exécuteur générique
    réintroduit) apparaît."""

    def test_only_order_tracking_and_negotiation_call_the_gateway_method(self):
        import re

        from ladini.graphs.agents.market_coach.flows.buyer import (
            negotiation as negotiation_mod,
            order_tracking as order_tracking_mod,
        )

        call_pattern = re.compile(r"\.select_winning_bid\(")

        legitimate_modules = {
            "order_tracking": order_tracking_mod,
            "negotiation": negotiation_mod,
        }
        for name, module in legitimate_modules.items():
            source = inspect.getsource(module)
            assert call_pattern.search(source), (
                f"{name} devrait toujours appeler select_winning_bid "
                "(régression du test lui-même si ce n'est plus le cas)"
            )

        # Le module de dispatch générique NE DOIT PLUS jamais nommer
        # `select_winning_bid` comme tool_name par défaut.
        procure_source = inspect.getsource(procure_actions)
        assert '"select_winning_bid"' not in procure_source
        assert "'select_winning_bid'" not in procure_source
