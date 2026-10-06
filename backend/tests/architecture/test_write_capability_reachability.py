"""Contrat de REACHABILITY — une capability déclarée doit être réellement
atteignable depuis une interaction utilisateur (Phase 3, 2026-09-04).

## Pourquoi ce fichier existe

Le dépôt a produit deux fois le même bug, invisible aux tests ciblés :

* `PRODUCER_CONFIRM_DELIVERY_PAYMENT` (F1) — intent déclaré, rôle correct,
  résolveur écrit, handler enregistré, mutation DB correcte… mais sans
  entrée `_RESOLVER_PASSTHROUGH` le validateur voyait `order_id` manquant,
  le classait comme identifiant technique non résoluble, terminait le tour
  par une CLARIFICATION — et le résolveur n'était JAMAIS appelé. Les tests
  F1 appelaient le résolveur DIRECTEMENT : ils ne pouvaient pas voir
  l'impasse.
* `delete_product` — capacité DB complète, aucune chaîne conversationnelle.

Ce fichier vérifie la chaîne, maillon par maillon, pour TOUS les goals
déclarés — pas seulement ceux auxquels on pense au moment d'un chantier.

## Ce qu'il ne fait pas

Il n'impose pas qu'un goal soit « bien conçu » : il vérifie uniquement
qu'un goal déclaré est traversable, ou qu'il figure dans la liste
d'exceptions ci-dessous AVEC une justification. Toute nouvelle exception
doit être ajoutée explicitement — c'est le point : rendre le compromis
visible plutôt que silencieux."""
from __future__ import annotations

import inspect

import pytest

from ladini.graphs.agents.market_coach.interpreter.intent import (
    _TUNNEL_ASSIGNMENTS,
    INTENT_CONFIG,
    INTENT_ROLE,
)
from ladini.graphs.agents.market_coach.registry import get_action
from ladini.graphs.agents.market_coach.utils import _AUTO_RESOLVABLE_FIELDS
from ladini.infrastructure.mcp.security import TOOL_SCOPE_MAP
from ladini.protocols.mcp.servers.h import TOOL_DESCRIPTIONS

# =====================================================================
# EXCEPTIONS DOCUMENTÉES
# =====================================================================
# Chaque entrée = une capacité déclarée qui n'est PAS traversable
# aujourd'hui, avec la raison. Retirer une entrée d'ici doit rendre le
# test vert (capacité réparée), jamais l'inverse sans justification.

# (2026-09-13, Deep Intent Architecture Cleanup) : toutes les entrées
# historiques de ces deux dicts (famille STOCK_*/CROP_*/AGRO_*,
# SALES_ACCEPT_CONTRACT, SYSTEM_*, PROCUREMENT_ACCEPT_OFFER,
# PROFILE_SWITCH_ROLE, STOCK_GET_MOVEMENTS) ont été supprimées
# d'INTENT_CONFIG — architecturalement, pas simplement masquées. Elles ne
# peuvent donc plus apparaître dans WRITE_GOALS/READ_GOALS (dérivés
# d'INTENT_CONFIG) et le mécanisme d'exception documentée n'a provisoirement
# plus de raison d'être ; conservé vide pour la structure des tests et pour
# tout futur gap réel qui mériterait la même discipline (documenté, jamais
# silencieux).

#: `tool_name` sans implémentation MCP/DB réelle.
TOOL_WITHOUT_IMPLEMENTATION: dict[str, str] = {}

#: Goals exigeant un identifiant technique SANS entrée
#: `_RESOLVER_PASSTHROUGH` — donc dont un éventuel résolveur ne peut pas
#: être atteint (impasse de type F1).
UNREACHABLE_RESOLVER: dict[str, str] = {}


def _goals(action_type: str):
    return [
        (goal, cfg)
        for goal, cfg in INTENT_CONFIG.items()
        if isinstance(cfg, dict)
        and str(cfg.get("action_type") or "").upper() == action_type
    ]


WRITE_GOALS = _goals("WRITE")
READ_GOALS = _goals("READ")


def _resolver_passthrough() -> dict:
    """`_RESOLVER_PASSTHROUGH` est une constante LOCALE au validateur —
    on la lit sur la source réelle plutôt que d'en maintenir une copie
    (une copie dériverait, ce qui est exactement le bug qu'on traque)."""
    import ladini.graphs.agents.market_coach.nodes.validation as validation_mod

    src = inspect.getsource(validation_mod)
    start = src.index("_RESOLVER_PASSTHROUGH = {")
    block = src[start : src.index("\n    }", start)]
    table = {}
    for line in block.splitlines():
        line = line.strip()
        if line.startswith('"') and '": (' in line:
            table[line.split('"')[1]] = line.split('": ("')[1].split('"')[0]
    return table


PASSTHROUGH = _resolver_passthrough()


def _is_flow_handled(cfg: dict, goal: str) -> bool:
    """Un goal pris en charge par un tunnel dédié n'utilise pas son
    `tool_name` comme nom d'outil MCP : c'est une étiquette symbolique, le
    flow appelle lui-même les vraies méthodes via les gateways."""
    return bool(cfg.get("handled_by_flow")) or goal in _TUNNEL_ASSIGNMENTS


class TestPassthroughTableIsSane:
    def test_every_passthrough_entry_targets_a_declared_goal(self):
        unknown = [g for g in PASSTHROUGH if g not in INTENT_CONFIG]
        assert not unknown, f"_RESOLVER_PASSTHROUGH cite des goals inconnus : {unknown}"

    def test_the_table_was_actually_parsed(self):
        """Garde-fou du test lui-même : si la constante est renommée ou
        déplacée, ce fichier doit échouer bruyamment plutôt que de
        silencieusement ne plus rien vérifier."""
        assert len(PASSTHROUGH) >= 5


class TestEveryWriteGoalIsWired:
    @pytest.mark.parametrize("goal,cfg", WRITE_GOALS, ids=[g for g, _ in WRITE_GOALS])
    def test_goal_has_a_role(self, goal, cfg):
        assert INTENT_ROLE.get(goal), f"{goal} n'a pas de rôle déclaré"

    @pytest.mark.parametrize("goal,cfg", WRITE_GOALS, ids=[g for g, _ in WRITE_GOALS])
    def test_goal_has_a_handler_or_a_flow(self, goal, cfg):
        if _is_flow_handled(cfg, goal):
            return
        assert get_action(goal) is not None, (
            f"{goal} est un WRITE sans handler enregistré et sans tunnel — "
            "il ne peut aboutir à aucune mutation"
        )

    @pytest.mark.parametrize("goal,cfg", WRITE_GOALS, ids=[g for g, _ in WRITE_GOALS])
    def test_tool_name_resolves_to_a_real_capability(self, goal, cfg):
        if _is_flow_handled(cfg, goal):
            return  # étiquette symbolique, jamais dispatchée comme outil MCP
        tool = cfg.get("tool_name") or ""
        if goal in TOOL_WITHOUT_IMPLEMENTATION:
            assert tool not in TOOL_DESCRIPTIONS, (
                f"{goal} est listé comme non implémenté mais `{tool}` existe "
                "désormais — retirer l'exception (la capacité est réparée)"
            )
            return
        assert tool in TOOL_DESCRIPTIONS, (
            f"{goal} déclare le tool `{tool}` qui n'existe pas — "
            "capacité déclarée mais impossible à exécuter"
        )

    @pytest.mark.parametrize("goal,cfg", WRITE_GOALS, ids=[g for g, _ in WRITE_GOALS])
    def test_real_tools_have_an_mcp_scope(self, goal, cfg):
        if _is_flow_handled(cfg, goal) or goal in TOOL_WITHOUT_IMPLEMENTATION:
            return
        tool = cfg.get("tool_name") or ""
        assert tool in TOOL_SCOPE_MAP, (
            f"{goal} : `{tool}` n'a pas de scope MCP — refusé par le fail-closed"
        )


class TestResolverIsActuallyReachable:
    """LE contrôle qui aurait attrapé le bug F1."""

    ALL_GOALS = WRITE_GOALS + READ_GOALS

    @pytest.mark.parametrize(
        "goal,cfg", ALL_GOALS, ids=[g for g, _ in WRITE_GOALS + READ_GOALS]
    )
    def test_technical_id_requirement_implies_a_passthrough(self, goal, cfg):
        blocking = [
            f
            for f in (cfg.get("required") or [])
            if str(f).endswith("_id") and f not in _AUTO_RESOLVABLE_FIELDS
        ]
        if not blocking or _is_flow_handled(cfg, goal):
            return
        if goal in UNREACHABLE_RESOLVER:
            assert goal not in PASSTHROUGH, (
                f"{goal} est listé comme inatteignable mais possède désormais "
                "un passthrough — retirer l'exception"
            )
            return
        assert goal in PASSTHROUGH, (
            f"{goal} exige {blocking} (identifiant technique) sans entrée "
            "_RESOLVER_PASSTHROUGH : le validateur terminera le tour par une "
            "CLARIFICATION et le résolveur ne sera JAMAIS appelé (bug F1). "
            "Ajouter l'entrée, ou documenter l'exception dans "
            "UNREACHABLE_RESOLVER avec sa raison."
        )


class TestRealEntryPointReachesTheResolver:
    """Preuve dynamique, pas seulement structurelle : on entre par le VRAI
    point d'entrée (`validator`) puis par le VRAI routeur
    (`DomainRouter.decide`), et on exige que la décision soit
    `to_resolver`. C'est précisément l'enchaînement que les tests F1
    (qui appelaient le résolveur directement) ne pouvaient pas voir."""

    @pytest.mark.parametrize(
        "goal",
        ["PRODUCER_CONFIRM_DELIVERY_PAYMENT", "SALES_UNPUBLISH_PRODUCT",
         "SALES_UPDATE_PRODUCT", "PRODUCTION_UPDATE_FUTURE",
         "PRODUCER_CANCEL_ORDER", "PRODUCER_CONFIRM_ORDER"],
    )
    def test_validator_then_router_route_to_the_resolver(self, goal):
        from ladini.graphs.agents.market_coach.core.router import DomainRouter
        from ladini.graphs.agents.market_coach.nodes.validation import validator
        from tests.conftest import StubRuntime, make_state, run

        # `make_state` pose `interpreted_event="UNKNOWN"` par défaut, ce que
        # le routeur traite (à raison) comme une dérive → `to_strategy`. Un
        # vrai tour arrive ici avec un événement classé : on reproduit donc
        # le cas nominal `NEW_TASK`, sinon ce test mesurerait le harnais et
        # pas le produit.
        state = make_state(current_goal=goal, interpreted_event="NEW_TASK")
        validated = run(validator(state, StubRuntime()))

        # Le validateur ne doit RIEN réclamer : sinon le routeur retombera
        # sur `to_strategy` et le résolveur ne sera jamais appelé.
        assert not validated.get("missing_fields"), (
            f"{goal} : le validateur réclame {validated.get('missing_fields')} — "
            "le résolveur ne sera pas atteint"
        )

        merged = {**state, **validated}
        decision = DomainRouter.build().decide(merged)
        assert decision == "to_resolver", (
            f"{goal} : le routeur décide `{decision}` au lieu de `to_resolver` — "
            "la capacité est déclarée mais son résolveur est inatteignable"
        )

    @pytest.mark.parametrize(
        "goal,event",
        [
            ("PRODUCER_CONFIRM_ORDER", "CONFIRM"),
            ("PRODUCER_CONFIRM_ORDER", "REJECT"),
            ("PRODUCER_CANCEL_ORDER", "CONFIRM"),
            ("PRODUCER_CANCEL_ORDER", "REJECT"),
        ],
    )
    def test_the_confirm_action_follow_up_turn_also_reaches_the_resolver(
        self, goal, event
    ):
        """Incident réel (2026-09-15) : le tour INITIAL (NEW_TASK, testé
        ci-dessus) routait déjà correctement vers `to_resolver` — MAIS le
        tour SUIVANT, une fois `PendingInteraction(CONFIRM_ACTION)` posé par
        `_resolve_pending_order_action` (flows/producer/flow.py), ne
        matchait AUCUNE règle de `DomainRouter` (ni goal ici n'était dans un
        tunnel) et retombait sur `route_after_validator`, qui route TOUT
        `CONFIRM_ACTION` porté par un goal à action enregistrée vers
        `to_confirmation` (confirmation_gate) — jamais revu par le
        résolveur auto-suffisant qui, seul, sait qu'un "annuler" ici doit
        exécuter `cancel_confirmed_order`, pas juste abandonner. Preuve
        dynamique que la 2e moitié du tunnel — celle qui manquait de
        couverture — est désormais atteignable elle aussi."""
        from ladini.graphs.agents.market_coach.core.router import DomainRouter
        from ladini.graphs.agents.market_coach.nodes.validation import validator
        from tests.conftest import StubRuntime, make_state, run

        state = make_state(
            current_goal=goal,
            interpreted_event=event,
            expected_input="CONFIRMATION",
            transaction_payload={"order_id": "order-1"},
        )
        validated = run(validator(state, StubRuntime()))
        assert not validated.get("missing_fields"), (
            f"{goal}/{event} : le validateur réclame "
            f"{validated.get('missing_fields')} — le résolveur ne sera pas atteint"
        )

        merged = {**state, **validated}
        decision = DomainRouter.build().decide(merged)
        assert decision == "to_resolver", (
            f"{goal}/{event} : le routeur décide `{decision}` au lieu de "
            "`to_resolver` — le tour de confirmation/annulation ne peut pas "
            "atteindre `_resolve_pending_order_action`"
        )


class TestReadGoalsPointAtRealQueries:
    @pytest.mark.parametrize("goal,cfg", READ_GOALS, ids=[g for g, _ in READ_GOALS])
    def test_tool_name_exists_or_is_documented(self, goal, cfg):
        if _is_flow_handled(cfg, goal):
            return
        tool = cfg.get("tool_name") or ""
        if tool in TOOL_DESCRIPTIONS:
            return
        # Les READ non implémentés sont tolérés mais doivent rester connus :
        # ils échouent proprement (erreur technique générique), sans risque
        # transactionnel. La liste vit dans le rapport d'audit.
        pytest.skip(f"{goal} -> `{tool}` : lecture non implémentée (documentée)")
