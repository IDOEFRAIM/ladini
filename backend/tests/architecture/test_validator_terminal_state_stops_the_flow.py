"""P1-2 (audit architectural 2026-09-08) : `validator` peut conclure le
tour lui-même — branche `blocking_technical_id`
(`nodes/validation.py:367-397`) : un identifiant technique (`*_id`)
manque sans résolveur dédié, `validator` pose déjà `final_response`,
`response_strategy="CLARIFICATION"`, `status="COMPLETED"`,
`pending_interaction=None`.

Root cause : `DomainRouter.decide` (alias `route_after_validator`) ne
traitait pas `COMPLETED` comme terminal (seuls `ERROR`/`WAITING_INPUT`
l'étaient) — le tour retombait sur le flux nominal (`to_resolver`) et
`context_resolver` s'exécutait sur un tour DÉJÀ conclu : appels MCP
possibles, écrasement du `final_response` du validateur.

Ce fichier prouve la chaîne RÉELLE `validator` -> `DomainRouter.decide`,
avec le VRAI goal reproduit par l'audit (`STOCK_UPDATE_LEVEL`,
`required=["stock_id", "quantity"]`, `stock_id` sans résolveur dédié dans
`_RESOLVER_PASSTHROUGH`)."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.core.router import DomainRouter
from ladini.graphs.agents.market_coach.nodes.validation import validator
from tests.conftest import make_state, run


class TestValidatorCompletedIsTerminalInTheRouter:
    def test_a_real_blocking_technical_id_produces_status_completed(self):
        """Verrouille le déclencheur réel avant de tester le routage —
        si `validator` change de comportement, ce test le signale
        distinctement du test de routage ci-dessous."""
        state = make_state(
            current_goal="STOCK_UPDATE_LEVEL",
            transaction_payload={"quantity": 50},
        )
        result = run(validator(state, mc_runtime=None))
        assert result["status"] == "COMPLETED"
        assert result["response_strategy"] == "CLARIFICATION"
        assert result["final_response"]
        assert result["missing_fields"] == []

    def test_domain_router_sends_a_completed_turn_straight_to_strategy(self):
        """LE test qui aurait attrapé P1-2 : un tour COMPLETED par le
        validateur ne doit JAMAIS atteindre context_resolver."""
        state = make_state(
            current_goal="STOCK_UPDATE_LEVEL",
            transaction_payload={"quantity": 50},
        )
        val_result = run(validator(state, mc_runtime=None))
        merged = {**state, **val_result}

        router = DomainRouter.build()
        target = router.decide(merged)

        assert target == "to_strategy", (
            f"un tour COMPLETED doit converger vers response_strategy, "
            f"jamais relancer un flow métier — obtenu: {target!r}"
        )

    def test_completed_wins_even_over_a_matching_route_rule(self):
        """Renforcement au-delà du cas reproduit par l'audit : COMPLETED
        doit gagner même si le goal matcherait par ailleurs une RouteRule
        (le garde est placé AVANT la boucle des règles, pas seulement dans
        le repli) — sans quoi cette propriété ne vaudrait que pour les
        goals sans règle, pas comme invariant général."""
        router = DomainRouter.build()
        for rule in router._rules:  # noqa: SLF001 — introspection de test
            for goal in rule.goals:
                state = {
                    "current_goal": goal,
                    "status": "COMPLETED",
                    "transaction_payload": {},
                }
                assert router.decide(state) == "to_strategy", (
                    f"COMPLETED doit rester terminal même pour {goal!r}, "
                    f"qui matche pourtant la règle -> {rule.target!r}"
                )

    def test_non_terminal_statuses_are_unaffected(self):
        """Non-régression : `WAITING_INPUT` routait déjà vers
        `to_strategy` avant ce correctif — seul `COMPLETED` change de
        comportement, la logique existante pour les autres statuts est
        inchangée."""
        router = DomainRouter.build()
        state = {
            "current_goal": "STOCK_UPDATE_LEVEL",
            "status": "WAITING_INPUT",
            "transaction_payload": {},
        }
        assert router.decide(state) == "to_strategy"
