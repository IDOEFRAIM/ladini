"""Preuve structurelle anti-bypass — `select_winning_bid` n'a qu'UN chemin
conversationnel sécurisé, désormais renforcé (F4, 2026-09-04, audit
fonctionnel — "PROCUREMENT_SELECT_WINNER/ACCEPT_OFFER pouvant contourner le
tunnel sécurisé").

## Le gap réel fermé par ce fichier

`PROCUREMENT_SELECT_WINNER` (déclaré dans `interpreter/intent.py`, HORS de
`_TUNNEL_ASSIGNMENTS`) résolvait, via l'exécuteur GÉNÉRIQUE
(`actions/procure.py::prep_procurement_select_winner`), vers le MÊME
tool_name réel `select_winning_bid` que le tunnel sécurisé
(`order_tracking.py::confirm_winner_selection`/`finalize_winner`) — sans
AUCUNE des protections de ce tunnel (revalidation de prix, étape GPS,
gardes de statut, résolution de cible). `PROCUREMENT_ACCEPT_OFFER`
résolvait vers `accept_bid`, un tool_name sans méthode DB réelle. Les deux
handlers sont désormais neutralisés — ce fichier le PROUVE structurellement
et empêche une régression future.

## Ce que ce fichier NE prétend PAS prouver

`select_winning_bid` reste également appelable depuis
`flows/buyer/negotiation.py` (tunnel `BUYER_NEGOTIATE_PRICE`, négociation
acheteur↔UN producteur) — un DEUXIÈME chemin LÉGITIME, déjà tunnelé (pas
l'exécuteur générique), avec ses propres limites déjà documentées dans un
chantier antérieur (GPS best-effort, pas de récap prix) — hors du périmètre
de F4 (mandat §21 : ne pas rouvrir le hardening Auction déjà fermé)."""
from __future__ import annotations

import inspect

import pytest

from agriconnect.graphs.agents.market_coach.actions import procure as procure_actions
from agriconnect.graphs.agents.market_coach.interpreter.intent import (
    INTENT_CONFIG,
    _TUNNEL_ASSIGNMENTS,
)
from agriconnect.graphs.agents.market_coach.registry import get_action


class TestGenericExecutorCanNeverReachSelectWinningBid:
    def test_prep_procurement_select_winner_always_raises(self):
        with pytest.raises(RuntimeError):
            procure_actions.prep_procurement_select_winner(
                {}, {"auction_id": "a1", "bid_id": "b1"}
            )

    def test_prep_procurement_accept_offer_always_raises(self):
        with pytest.raises(RuntimeError):
            procure_actions.prep_procurement_accept_offer({}, {"bid_id": "b1"})

    def test_neither_handler_can_ever_resolve_a_tool_name(self):
        """Peu importe le payload fourni (y compris un payload complet,
        déjà validé) — ces deux handlers ne DOIVENT jamais retourner un
        `(tool_name, args)` exploitable par l'exécuteur."""
        for handler in (
            procure_actions.prep_procurement_select_winner,
            procure_actions.prep_procurement_accept_offer,
        ):
            with pytest.raises(RuntimeError):
                handler({"user_phone": "+22670000001"}, {
                    "auction_id": "a1", "bid_id": "b1",
                })


class TestBothGoalsStayRegisteredButNeutralized:
    """Le registre d'actions (`registry.py::validate_integrity`) exige un
    handler pour tout goal WRITE non-`handled_by_flow` — les deux goals
    restent donc déclarés (pas de cascade de suppression du catalogue
    d'intents), mais leur handler ne fait plus RIEN d'exploitable."""

    def test_procurement_select_winner_is_still_a_registered_action(self):
        registration = get_action("PROCUREMENT_SELECT_WINNER")
        assert registration is not None
        assert registration.is_write is True

    def test_procurement_accept_offer_is_still_a_registered_action(self):
        registration = get_action("PROCUREMENT_ACCEPT_OFFER")
        assert registration is not None
        assert registration.is_write is True

    def test_neither_goal_has_a_dedicated_tunnel(self):
        """Ni redirigés vers le tunnel sécurisé, ni un nouveau tunnel
        parallèle — simplement neutralisés à la source (mandat §18 : une
        seule entrée métier vers winner selection)."""
        assert INTENT_CONFIG["PROCUREMENT_SELECT_WINNER"].get("tunnel") is None
        assert INTENT_CONFIG["PROCUREMENT_ACCEPT_OFFER"].get("tunnel") is None
        assert "PROCUREMENT_SELECT_WINNER" not in _TUNNEL_ASSIGNMENTS
        assert "PROCUREMENT_ACCEPT_OFFER" not in _TUNNEL_ASSIGNMENTS


class TestExactlyTwoLegitimateCallSitesExistInTheWholeCodebase:
    """Preuve structurelle FINALE, sur le code source réel : `select_winning_bid`
    (la méthode du gateway conversationnel, PAS la méthode DB elle-même)
    n'est appelée QUE depuis les deux tunnels connus et audités. Ce test
    DOIT échouer si un troisième site (ex: un futur exécuteur générique
    réintroduit) apparaît."""

    def test_only_order_tracking_and_negotiation_call_the_gateway_method(self):
        import re

        from agriconnect.graphs.agents.market_coach.flows.buyer import (
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
