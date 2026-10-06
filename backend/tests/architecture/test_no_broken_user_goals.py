"""AUCUN FAUX BOUTON — tout goal réellement exposé à l'utilisateur doit
pouvoir aboutir (Phase 4, 2026-09-04 ; réécrit 2026-09-13, Deep Intent
Architecture Cleanup).

## Le contrat

Un goal « exposé » = présent dans `_classifiable_intents()`, donc décrit au
LLM dans le catalogue `new_task_v2`/prompt d'interprétation, donc
**classifiable depuis un message utilisateur**. Pour chacun, ce fichier
exige la chaîne minimale d'exécution :

```
tool_name réellement exposé côté MCP        (sinon : « tool not found »)
scope MCP présent                            (sinon : refus fail-closed)
handler enregistré                           (sinon : aucune mutation possible)
identifiant technique requis ⇒ passthrough   (sinon : impasse F1)
```

## Différence avec `test_write_capability_reachability.py`

Ce fichier-là décrit **tout le catalogue** et tolère des exceptions
documentées : il sert à empêcher les régressions de câblage sur des
capacités connues comme mortes ou en attente de décision.

Celui-ci ne regarde que ce que l'utilisateur peut réellement atteindre et
**n'accepte aucune exception**.

## Ce qui a changé (2026-09-13)

Avant le Deep Intent Architecture Cleanup, `INTENT_CONFIG` contenait ~71
entrées dont une trentaine étaient du legacy masqué (`_DISABLED_INTENT_PREFIXES`
/ `_DEPRECATED_INTENTS`) : présentes dans le catalogue mais retirées de
l'exposition LLM par un filtre séparé. Ce chantier a supprimé ce mécanisme de
masquage : chaque entrée d'`INTENT_CONFIG` qui n'était pas un objectif
utilisateur réel a été **architecturalement supprimée** (code, handler,
domaine, mappings) plutôt que simplement cachée. `_classifiable_intents()`
retourne donc désormais `frozenset(INTENT_CONFIG)` sans filtrage — un goal
« exposé » et un goal « présent dans le catalogue » sont maintenant la même
chose, par construction. Les anciennes classes `TestDeprecationIsHonest`
(qui testait le mécanisme de masquage lui-même) n'ont plus d'objet et ont
été retirées ; leurs invariants utiles sont repris ci-dessous sous une forme
qui correspond à l'architecture actuelle."""
from __future__ import annotations

import inspect

import pytest

from ladini.graphs.agents.market_coach.interpreter.intent import (
    _TUNNEL_ASSIGNMENTS,
    INTENT_CONFIG,
)
from ladini.graphs.agents.market_coach.interpreter.routing import (
    _classifiable_intents,
)
from ladini.graphs.agents.market_coach.registry import get_action
from ladini.graphs.agents.market_coach.utils import _AUTO_RESOLVABLE_FIELDS
from ladini.infrastructure.mcp.security import TOOL_SCOPE_MAP
from ladini.protocols.mcp.servers.h import TOOL_DESCRIPTIONS

EXPOSED = sorted(_classifiable_intents())


def _cfg(goal):
    return INTENT_CONFIG.get(goal) or {}


def _is_flow_handled(goal) -> bool:
    """Goal servi par un tunnel dédié : son `tool_name` est une étiquette
    symbolique (`add_to_cart`, `cancel_order`…), pas un outil MCP — le flow
    appelle lui-même les vraies méthodes via les gateways."""
    return bool(_cfg(goal).get("handled_by_flow")) or goal in _TUNNEL_ASSIGNMENTS


def _passthrough_table() -> dict:
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


PASSTHROUGH = _passthrough_table()


class TestNoExposedGoalIsBroken:
    """AUCUNE exception tolérée ici — c'est le principe."""

    @pytest.mark.parametrize("goal", EXPOSED)
    def test_tool_name_exists(self, goal):
        if _is_flow_handled(goal):
            return
        tool = _cfg(goal).get("tool_name") or ""
        assert tool in TOOL_DESCRIPTIONS, (
            f"{goal} est proposé à l'utilisateur (catalogue LLM) mais son tool "
            f"`{tool}` n'existe pas : l'utilisateur récolterait une erreur "
            "technique. Réparer le câblage, ou supprimer le goal du "
            "catalogue (`INTENT_CONFIG`) avec sa justification."
        )

    @pytest.mark.parametrize("goal", EXPOSED)
    def test_tool_has_an_mcp_scope(self, goal):
        if _is_flow_handled(goal):
            return
        tool = _cfg(goal).get("tool_name") or ""
        assert tool in TOOL_SCOPE_MAP, (
            f"{goal} : `{tool}` sans scope MCP — refusé par le fail-closed"
        )

    @pytest.mark.parametrize("goal", EXPOSED)
    def test_write_goal_has_a_handler(self, goal):
        cfg = _cfg(goal)
        if str(cfg.get("action_type") or "").upper() != "WRITE" or _is_flow_handled(goal):
            return
        assert get_action(goal) is not None, f"{goal} : WRITE sans handler enregistré"

    @pytest.mark.parametrize("goal", EXPOSED)
    def test_technical_id_has_a_resolver_passthrough(self, goal):
        """L'impasse F1 : un identifiant technique requis sans entrée
        `_RESOLVER_PASSTHROUGH` fait terminer le tour par une clarification
        générique, le résolveur n'étant jamais appelé."""
        if _is_flow_handled(goal):
            return
        blocking = [
            f
            for f in (_cfg(goal).get("required") or [])
            if str(f).endswith("_id") and f not in _AUTO_RESOLVABLE_FIELDS
        ]
        if not blocking:
            return
        assert goal in PASSTHROUGH, (
            f"{goal} est exposé et exige {blocking} sans entrée "
            "_RESOLVER_PASSTHROUGH : impasse conversationnelle garantie"
        )


class TestCatalogueHasNoMaskingLeftover:
    """Garde-fous structurels post-cleanup (spec Deep Intent Architecture
    Cleanup §36) : le catalogue classifiable ne doit plus jamais réintroduire
    le mécanisme de masquage supprimé le 2026-09-13, ni classer un goal sans
    label ni consommateur."""

    def test_classifiable_intents_is_the_full_catalogue(self):
        """`_classifiable_intents()` ne filtre plus rien : toute nouvelle
        divergence avec `INTENT_CONFIG` signalerait la réintroduction d'un
        filtrage caché (role-based ou masquage legacy)."""
        assert set(EXPOSED) == set(INTENT_CONFIG)

    @pytest.mark.parametrize("goal", EXPOSED)
    def test_every_classifiable_intent_has_a_label(self, goal):
        assert _cfg(goal).get("label"), f"{goal} : classifiable sans label"

    @pytest.mark.parametrize("goal", EXPOSED)
    def test_every_classifiable_intent_has_a_valid_consumer(self, goal):
        """Un consommateur valide = un flow dédié, OU un handler
        `@register_action` enregistré (READ comme WRITE)."""
        if _is_flow_handled(goal):
            return
        assert get_action(goal) is not None, (
            f"{goal} : classifiable mais sans flow dédié ni handler enregistré "
            "— aucun code ne peut réellement l'exécuter"
        )


class TestSupportedCapabilitiesStayExposed:
    """Réciproque du critère de sortie : les capacités réellement
    supportées doivent rester atteignables. Ce test échoue si une
    suppression trop large emporte une fonctionnalité vivante."""

    @pytest.mark.parametrize(
        "goal",
        [
            "BUYER_REQUEST",
            "BUYER_ADD_TO_CART",
            "BUYER_VIEW_CART",
            "BUYER_PREORDER_INIT",
            "BUYER_PREORDER_CONFIRM",
            "BUYER_CANCEL_ORDER",
            "BUYER_LIST_ORDERS",
            "BUYER_CHECK_ORDER_STATUS",
            "BUYER_LIST_AUCTIONS",
            "BUYER_CHECK_AUCTION_STATUS",
            "BUYER_NEGOTIATE_PRICE",
            "PROCUREMENT_CREATE_REQUEST",
            "SALES_PUBLISH_PRODUCT",
            "SALES_UPDATE_PRODUCT",
            "SALES_UNPUBLISH_PRODUCT",
            "SALES_RECORD_DIRECT",
            "SALES_LIST_ORDERS",
            "SALES_PLACE_BID",
            "PRODUCTION_UPDATE_FUTURE",
            "PRODUCTION_DECLARE_FUTURE",
            "PRODUCER_CONFIRM_DELIVERY_PAYMENT",
            "PRODUCER_CONFIRM_DELIVERY_OTP",
            "PRODUCER_CANCEL_ORDER",
            "STOCK_REGISTER_HARVEST",
            "STOCK_GET_SUMMARY",
            "MARKET_SNAPSHOT",
        ],
    )
    def test_capability_is_still_reachable(self, goal):
        assert goal in EXPOSED, (
            f"{goal} est une capacité réellement supportée mais n'est plus "
            "proposée à l'utilisateur — suppression trop large"
        )
