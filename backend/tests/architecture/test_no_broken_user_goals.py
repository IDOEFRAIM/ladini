"""AUCUN FAUX BOUTON — tout goal réellement exposé à l'utilisateur doit
pouvoir aboutir (Phase 4, 2026-09-04).

## Le contrat

Un goal « exposé » = présent dans `allowed_intents_for_role(...)`, donc
décrit au LLM dans le prompt d'interprétation, donc **classifiable depuis
un message utilisateur**. Pour chacun, ce fichier exige la chaîne
minimale d'exécution :

```
tool_name réellement exposé côté MCP        (sinon : « tool not found »)
scope MCP présent                            (sinon : refus fail-closed)
handler enregistré                           (sinon : aucune mutation possible)
identifiant technique requis ⇒ passthrough   (sinon : impasse F1)
```

## Différence avec `test_write_capability_reachability.py`

Ce fichier-là décrit **tout le catalogue** (70 goals) et tolère des
exceptions documentées : il sert à empêcher les régressions de câblage
sur des capacités connues comme mortes ou en attente de décision.

Celui-ci ne regarde que ce que l'utilisateur peut réellement atteindre et
**n'accepte aucune exception**. Un goal cassé n'a que deux issues : le
réparer, ou le sortir du catalogue exposé (`_DEPRECATED_INTENTS` /
`_DISABLED_INTENT_PREFIXES`, avec justification). C'est le critère de
sortie de la Phase 4 : « tous les goals exposés correspondent à des
fonctionnalités réelles du produit »."""
from __future__ import annotations

import inspect

import pytest

from agriconnect.graphs.agents.market_coach.interpreter.intent import (
    INTENT_CONFIG,
    _TUNNEL_ASSIGNMENTS,
)
from agriconnect.graphs.agents.market_coach.interpreter.routing import (
    _DEPRECATED_INTENTS,
    _DISABLED_INTENT_PREFIXES,
    allowed_intents_for_role,
)
from agriconnect.graphs.agents.market_coach.registry import get_action
from agriconnect.graphs.agents.market_coach.utils import _AUTO_RESOLVABLE_FIELDS
from agriconnect.infrastructure.mcp.security import TOOL_SCOPE_MAP
from agriconnect.protocols.mcp.servers.h import TOOL_DESCRIPTIONS

EXPOSED = sorted(allowed_intents_for_role("PRODUCER"))


def _cfg(goal):
    return INTENT_CONFIG.get(goal) or {}


def _is_flow_handled(goal) -> bool:
    """Goal servi par un tunnel dédié : son `tool_name` est une étiquette
    symbolique (`add_to_cart`, `cancel_order`…), pas un outil MCP — le flow
    appelle lui-même les vraies méthodes via les gateways."""
    return bool(_cfg(goal).get("handled_by_flow")) or goal in _TUNNEL_ASSIGNMENTS


def _passthrough_table() -> dict:
    import agriconnect.graphs.agents.market_coach.nodes.validation as validation_mod

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
            "technique. Réparer le câblage, ou déprécier le goal dans "
            "`_DEPRECATED_INTENTS` avec sa justification."
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


class TestDeprecationIsHonest:
    def test_every_deprecated_goal_still_exists_in_the_catalogue(self):
        """On déprécie l'EXPOSITION, jamais par suppression silencieuse :
        `INTENT_CONFIG` doit rester la source de vérité du catalogue."""
        unknown = sorted(g for g in _DEPRECATED_INTENTS if g not in INTENT_CONFIG)
        assert not unknown, f"_DEPRECATED_INTENTS cite des goals inexistants : {unknown}"

    def test_no_deprecated_goal_is_still_exposed(self):
        leaked = sorted(g for g in _DEPRECATED_INTENTS if g in EXPOSED)
        assert not leaked, f"goals dépréciés encore exposés au LLM : {leaked}"

    def test_deprecating_a_working_goal_is_flagged(self):
        """Garde-fou inverse : si un goal déprécié redevient parfaitement
        câblé, c'est probablement qu'il a été réparé — il doit alors être
        ré-exposé, ou sa dépréciation re-justifiée explicitement."""
        product_decisions = {
            # Câblés mais volontairement hors catalogue.
            "PROCUREMENT_SELECT_WINNER",  # F4 : handler neutralisé (anti-bypass)
            # Outil réel (`get_stock_movements`), câblage manquant assumé :
            # exposer un historique de mouvements alors que l'enregistrement
            # et la correction de mouvements sont hors catalogue produirait
            # une demi-capacité. Décision produit « ledger d'inventaire »
            # (docs/PRODUCT_INTENT_SCOPE_2026-09-04.md §5).
            "STOCK_GET_MOVEMENTS",
        }
        repaired = []
        for goal in sorted(_DEPRECATED_INTENTS):
            if goal in product_decisions or _is_flow_handled(goal):
                continue
            tool = _cfg(goal).get("tool_name") or ""
            if tool in TOOL_DESCRIPTIONS:
                repaired.append(f"{goal} -> {tool}")
        assert not repaired, (
            "ces goals dépréciés pointent désormais un outil RÉEL — les "
            f"ré-exposer ou documenter pourquoi ils restent cachés : {repaired}"
        )

    def test_the_exposed_catalogue_actually_shrank(self):
        """Garde-fou du test lui-même : si le filtrage disparaît, ce fichier
        doit échouer bruyamment plutôt que de ne plus rien vérifier."""
        assert len(EXPOSED) < len(INTENT_CONFIG)
        assert _DISABLED_INTENT_PREFIXES and _DEPRECATED_INTENTS


class TestSupportedCapabilitiesStayExposed:
    """Réciproque du critère de sortie : les capacités réellement
    supportées doivent rester atteignables. Ce test échoue si une
    dépréciation trop large emporte une fonctionnalité vivante."""

    @pytest.mark.parametrize(
        "goal",
        [
            "SEARCH_PRODUCTS",
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
            "SALES_UPDATE_PRODUCTION",
            "DECLARE_CROP_CYCLE",
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
            "proposée à l'utilisateur — dépréciation trop large"
        )
