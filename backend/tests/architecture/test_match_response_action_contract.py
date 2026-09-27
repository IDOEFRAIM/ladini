"""Test de garde (mandat "2e mismatch CONFIRM vs ACCEPT", 2026-09-26, §7/§11) : AUCUN call-site
production ne doit jamais transmettre une action conversationnelle brute ("CONFIRM"/"REJECT") à
`RecurringSupplyGateway.accept_match_proposal` — chaque appelant doit d'abord la faire passer par
la table canonique partagée `flows/buyer/recurring_need.py::_MATCH_RESPONSE_TO_SERVICE_ACTION`
(via `_to_match_service_action`), elle-même bornée par le contrat réel du service
(`services/database/recurring_supply.py::MATCH_RESPONSE_ACTIONS = ("ACCEPT", "REJECT")`).

Deux occurrences de ce bug ont déjà été trouvées et corrigées séparément :
  - `_respond_to_digest_flow` (réponse au digest récurrent) — PR #12.
  - `_respond_to_match` (menu détail `GET_MY_NEEDS`) — ce mandat.

Ce test empêche structurellement qu'un futur 3e call-site (ou une régression sur les deux
existants) réintroduise le même bug — inspection de SOURCE plutôt que de comportement, même
discipline que `test_no_legacy_pending_signal_shim.py` : un comportement correct par coïncidence
(ex: un futur call-site qui ne teste jamais le cas CONFIRM) ne protège de rien, seule une garantie
structurelle empêche la régression silencieuse."""
from __future__ import annotations

import inspect
import re

import pytest


def _source(fn) -> str:
    return inspect.getsource(fn)


class TestNoRawConversationalActionReachesTheGateway:
    """Chaque appelant de `accept_match_proposal` doit passer par `_to_match_service_action`
    AVANT l'appel gateway, jamais transmettre son propre paramètre `action` tel quel."""

    def test_respond_to_match_maps_through_the_canonical_function_before_calling_the_gateway(self):
        from ladini.graphs.agents.market_coach.flows.buyer.recurring_need import (
            _respond_to_match,
        )

        src = _source(_respond_to_match)
        assert "_to_match_service_action(action)" in src
        assert re.search(r"accept_match_proposal\([^)]*action=action\b", src) is None, (
            "_respond_to_match ne doit plus jamais passer `action=action` (le mot conversationnel "
            "brut) à accept_match_proposal — seulement `action=service_action` (mappé)."
        )
        assert "action=service_action" in src

    def test_respond_to_digest_flow_maps_through_the_canonical_function_before_calling_the_gateway(self):
        from ladini.graphs.agents.market_coach.flows.buyer.recurring_need import (
            _respond_to_digest_flow,
        )

        src = _source(_respond_to_digest_flow)
        assert "_to_match_service_action(action)" in src
        assert re.search(r"accept_match_proposal\([^)]*action=action\b", src) is None
        assert "action=service_action" in src

    def test_there_are_exactly_two_production_call_sites_of_accept_match_proposal(self):
        """Verrou explicite du périmètre audité (mandat §2/§11) : si un 3e call-site apparaît un
        jour, ce test doit le forcer à passer par cette même revue de contrat plutôt que de
        glisser silencieusement à côté."""
        import ladini.graphs.agents.market_coach.flows.buyer.recurring_need as mod

        src = inspect.getsource(mod)
        assert src.count("gw.accept_match_proposal(") == 2


class TestSharedMappingIsTheSingleSourceOfTruth:
    def test_the_shared_mapping_covers_confirm_and_reject_only(self):
        from ladini.graphs.agents.market_coach.flows.buyer.recurring_need import (
            _MATCH_RESPONSE_TO_SERVICE_ACTION,
        )

        assert _MATCH_RESPONSE_TO_SERVICE_ACTION == {"CONFIRM": "ACCEPT", "REJECT": "REJECT"}

    def test_every_mapped_value_belongs_to_the_canonical_service_contract(self):
        from ladini.graphs.agents.market_coach.flows.buyer.recurring_need import (
            _MATCH_RESPONSE_TO_SERVICE_ACTION,
        )
        from ladini.services.database.recurring_supply import MATCH_RESPONSE_ACTIONS

        for service_action in _MATCH_RESPONSE_TO_SERVICE_ACTION.values():
            assert service_action in MATCH_RESPONSE_ACTIONS

    def test_to_match_service_action_resolves_both_directions_correctly(self):
        from ladini.graphs.agents.market_coach.flows.buyer.recurring_need import (
            _to_match_service_action,
        )

        assert _to_match_service_action("CONFIRM") == "ACCEPT"
        assert _to_match_service_action("REJECT") == "REJECT"

    def test_an_action_outside_the_closed_conversational_vocabulary_fails_fast(self):
        from ladini.graphs.agents.market_coach.flows.buyer.recurring_need import (
            _to_match_service_action,
        )

        with pytest.raises(AssertionError):
            _to_match_service_action("CONFIRM_MATCH")  # le sentinel `transaction_payload`, jamais l'action résolue
        with pytest.raises(AssertionError):
            _to_match_service_action("ACCEPT")  # déjà le mot service : ne doit jamais être redonné en entrée
        with pytest.raises(AssertionError):
            _to_match_service_action("")
