"""Incident réel (2026-09-07, log de production) : un producteur répondant
"ok" / "j'accepte" / "je suis d'accord" à une confirmation
`STOCK_REGISTER_HARVEST` déjà posée voyait le MÊME récapitulatif renvoyé en
boucle, indéfiniment — jamais d'exécution réelle (`add_stock`).

Root cause (chaîne complète reproduite ici) : `validator` tourne AUSSI sur
le tour où l'utilisateur RÉPOND à la confirmation (`event="CONFIRM"`) — à ce
stade tous les champs requis sont par construction déjà remplis, donc
`validator` prend sa branche "validated_complete" comme pour n'importe quel
autre tour prêt à avancer, et effaçait inconditionnellement
`pending_interaction` (`clear_pending_interaction("validated_complete")`)
AVANT que `confirmation_gate`, plus loin dans le MÊME tour, ne puisse le
relire. `confirmation_gate` ne trouvait alors plus de `CONFIRM_ACTION`
actif (`get_pending_interaction(state).kind != CONFIRM_ACTION` malgré
`event=="CONFIRM"`), retombait sur sa branche générique et reconstruisait
un récapitulatif FRAIS — posant une NOUVELLE `CONFIRM_ACTION` à la place de
résoudre l'ancienne. Boucle infinie : chaque "j'accepte" ré-affichait la
question au lieu de déclencher l'exécution.

Ce test enchaîne les VRAIES fonctions `validator` puis `confirmation_gate`,
dans l'ordre réel du graphe, pour prouver que `pending_interaction` survit
intact jusqu'à `confirmation_gate` et que la confirmation aboutit bien à
`EXECUTING`."""
from __future__ import annotations

from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
)
from agriconnect.graphs.agents.market_coach.nodes.confirmation_gate import (
    confirmation_gate,
)
from agriconnect.graphs.agents.market_coach.nodes.validation import validator
from tests.conftest import make_state, run


def _confirm_action_state(**overrides):
    base = make_state(
        current_goal="STOCK_REGISTER_HARVEST",
        interpreted_event="CONFIRM",
        goal_status="WAITING_CONFIRMATION",
        status="WAITING_CONFIRMATION",
        transaction_payload={"product": "poulets", "quantity": 6000, "unit": "UNITE"},
        confirmation_summary=(
            "Enregistrement d'une récolte : 6000 UNITE de poulets en stock."
        ),
        pending_interaction={
            "kind": "CONFIRM_ACTION",
            "goal": "STOCK_REGISTER_HARVEST",
            "field": None,
            "context_ref": "confirmation",
            "candidates": [],
            "created_at": 0.0,
            "status": "ACTIVE",
            "target": None,
        },
    )
    base.update(overrides)
    return base


class TestValidatorDoesNotStealAnActiveConfirmation:
    def test_validator_leaves_confirm_action_intact_on_a_complete_payload(self):
        """The exact bug: validator's 'validated_complete' branch must not
        clear a pending_interaction that IS the confirmation this turn is
        answering."""
        state = _confirm_action_state()

        result = run(validator(state, mc_runtime=None))

        merged = {**state, **result}
        pending = get_pending_interaction(merged)
        assert pending.kind == InteractionKind.CONFIRM_ACTION, (
            f"validator must not clear an active CONFIRM_ACTION — got patch keys "
            f"{list(result.keys())}, pending_interaction={merged.get('pending_interaction')}"
        )

    def test_full_chain_validator_then_confirmation_gate_executes_not_reasks(self):
        """End-to-end reproduction: after validator runs (as it does for
        real, right before confirmation_gate in the graph), the CONFIRM
        reply must actually execute — never re-show the same recap."""
        state = _confirm_action_state()

        val_patch = run(validator(state, mc_runtime=None))
        state_after_validator = {**state, **val_patch}

        gate_patch = run(confirmation_gate(state_after_validator, mc_runtime=None))

        assert gate_patch["status"] == "EXECUTING", (
            f"confirmation_gate must execute the confirmed action, not "
            f"re-ask — got status={gate_patch.get('status')!r}, "
            f"response_strategy={gate_patch.get('response_strategy')!r}"
        )
        assert gate_patch["is_certified"] is True
        assert gate_patch["execution_authorized"] is True
        final_state = {**state_after_validator, **gate_patch}
        assert get_pending_interaction(final_state).kind == InteractionKind.NONE, (
            "a resolved confirmation must never leave CONFIRM_ACTION active "
            "for the next turn"
        )

    def test_a_non_confirm_answer_still_clears_pending_interaction_as_before(self):
        """Non-regression: the normal slot-filling path (no confirmation in
        flight) must keep clearing pending_interaction exactly as before —
        this guard only protects an ALREADY-ACTIVE CONFIRM_ACTION."""
        state = make_state(
            current_goal="STOCK_REGISTER_HARVEST",
            interpreted_event="ANSWER",
            transaction_payload={"product": "poulets", "quantity": 6000, "unit": "UNITE"},
            pending_interaction={
                "kind": "ENTER_FIELD",
                "goal": "STOCK_REGISTER_HARVEST",
                "field": "quantity",
                "context_ref": None,
                "candidates": [],
                "created_at": 0.0,
                "status": "ACTIVE",
                "target": None,
            },
        )

        result = run(validator(state, mc_runtime=None))

        merged = {**state, **result}
        assert get_pending_interaction(merged).kind == InteractionKind.NONE
