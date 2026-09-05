"""EXPOSITION MCP EN ALLOW-LIST — contrat de sécurité (Phase 8, 2026-09-05).

## La classe de défaut supprimée

`_compute_exposed_methods()` exposait toute méthode async publique de
`AgriDatabaseService`. Ajouter une méthode créait un outil MCP ; seule
l'absence de scope l'empêchait d'être appelée. Ce modèle **opt-out** a
produit quatre failles distinctes de la même racine (`update_order_status`,
`mark_escrow_paid`, `expire_pending_payments`,
`cancel_preorder_draft(target_status)`).

Ce fichier verrouille le modèle **opt-in** :

```
MCP_EXPOSED_TOOLS  →  TOOL_SCOPE_MAP  →  autorisation runtime
   (existe ?)           (permission ?)      (fail-closed)
```

Le test le plus important est `test_a_new_public_method_is_not_exposed` :
il démontre qu'une méthode ajoutée demain **n'est pas** un outil."""
from __future__ import annotations

import ast
import inspect
import os

import pytest

from agriconnect.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
from agriconnect.infrastructure.mcp.exposure import MCP_EXPOSED_TOOLS
from agriconnect.infrastructure.mcp.security import TOOL_SCOPE_MAP
from agriconnect.protocols.mcp.servers.h import EXPOSED_METHODS, TOOL_DESCRIPTIONS
from agriconnect.services.database.d import AgriDatabaseService

SRC = os.path.join("src", "agriconnect")
INVOCATION_HELPERS = {"_call", "call_tool", "call_db", "invoke_tool"}


def _public_async_methods() -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(AgriDatabaseService)
        if not name.startswith("_") and inspect.iscoroutinefunction(member)
    }


def _literal_tool_invocations() -> set[str]:
    """Tout nom d'outil littéral passé à un helper d'invocation, dans tout
    le paquet — la mesure des besoins RÉELS du runtime."""
    names: set[str] = set()
    for root, _, files in os.walk(SRC):
        for f in files:
            if not f.endswith(".py"):
                continue
            try:
                tree = ast.parse(open(os.path.join(root, f), encoding="utf-8").read())
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                fn = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if fn in INVOCATION_HELPERS:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        names.add(first.value)
    return names


class TestExposureIsOptIn:
    """LE contrat : l'exposition vient de la liste, pas de l'introspection."""

    def test_exposed_set_is_exactly_introspection_intersect_allowlist(self):
        assert set(EXPOSED_METHODS) == _public_async_methods() & MCP_EXPOSED_TOOLS

    def test_a_new_public_method_is_not_exposed(self):
        """Simule l'ajout d'une méthode publique async : elle ne doit
        apparaître dans AUCUN outil sans décision explicite. C'est
        exactement le défaut structurel que cette phase supprime."""
        from agriconnect.protocols.mcp.servers import h

        class _ServiceWithNewMethod(AgriDatabaseService):  # pragma: no cover
            async def newly_added_dangerous_mutation(self, order_id: str):
                ...

        introspected = {
            name
            for name, member in inspect.getmembers(_ServiceWithNewMethod)
            if not name.startswith("_") and inspect.iscoroutinefunction(member)
        }
        assert "newly_added_dangerous_mutation" in introspected, "garde-fou du test"
        # La règle d'exposition appliquée à cette classe enrichie :
        assert "newly_added_dangerous_mutation" not in (introspected & MCP_EXPOSED_TOOLS)
        # Et concrètement, l'outil n'existe pas côté MCP :
        assert "newly_added_dangerous_mutation" not in h.TOOL_DESCRIPTIONS

    def test_no_declared_tool_is_missing_from_the_service(self):
        """Dérive inverse : un nom mal orthographié ou une méthode
        supprimée ne doit pas rester dans la liste."""
        unknown = MCP_EXPOSED_TOOLS - _public_async_methods()
        assert not unknown, f"noms déclarés sans méthode correspondante : {sorted(unknown)}"

    def test_every_exposed_tool_has_a_scope(self):
        """Exposé sans scope = inutilisable ; scope sans exposition =
        trompeur. Les deux listes doivent rester cohérentes."""
        missing = [t for t in EXPOSED_METHODS if t not in TOOL_SCOPE_MAP]
        assert not missing, f"outils exposés sans scope : {missing}"

    def test_descriptors_match_the_exposed_set(self):
        assert set(TOOL_DESCRIPTIONS) == set(EXPOSED_METHODS)


class TestNoRegressionForRealJourneys:
    """Test d'équivalence (§14) : tout ce dont le runtime a réellement
    besoin doit rester exposé. Une capacité perdue fait échouer ici."""

    def test_every_goal_tool_that_exists_is_still_exposed(self):
        declared = {
            cfg.get("tool_name")
            for cfg in INTENT_CONFIG.values()
            if isinstance(cfg, dict) and cfg.get("tool_name")
        }
        real = declared & _public_async_methods()
        missing = sorted(real - set(EXPOSED_METHODS))
        assert not missing, f"outils de goals devenus invisibles : {missing}"

    def test_every_literally_invoked_tool_is_still_exposed(self):
        invoked = _literal_tool_invocations() & _public_async_methods()
        missing = sorted(invoked - set(EXPOSED_METHODS))
        assert not missing, f"outils appelés par le runtime mais masqués : {missing}"

    @pytest.mark.parametrize(
        "tool",
        [
            # parcours acheteur
            "search_products", "create_preorder_draft", "confirm_preorder_draft",
            "cancel_preorder_draft", "cancel_pending_order", "get_buyer_orders_dashboard",
            # parcours producteur
            "create_product", "update_product_price_and_qty", "delete_product",
            "get_producer_orders", "confirm_delivery_and_payment", "cancel_confirmed_order",
            "record_sale",
            # enchères
            "create_auction", "place_bid", "update_bid_price", "select_winning_bid",
            "get_auction_bids", "get_my_active_bids",
            # identité / GPS
            "identify_or_create_user", "create_user_profile", "update_geo_location",
        ],
    )
    def test_critical_journey_tool_remains_exposed(self, tool):
        assert tool in EXPOSED_METHODS


class TestDangerousAndSystemToolsAreInvisible:
    @pytest.mark.parametrize(
        "tool,reason",
        [
            ("update_order_status", "statut et paiement arbitraires, sans propriétaire"),
            ("mark_escrow_paid", "déclarerait un paiement sans le fournisseur"),
            ("expire_pending_payments", "annulation en masse, sans acteur"),
            ("ensure_performance_indexes", "DDL de maintenance, appelé par le worker"),
            ("check_and_expire_auctions", "balayage système"),
            ("get_buyer_profile", "résolution d'identité interne"),
            ("get_producer_profile", "résolution d'identité interne"),
            ("finalize_multi_order", "produirait une commande multi-producteurs"),
        ],
    )
    def test_tool_is_not_exposed(self, tool, reason):
        assert tool not in EXPOSED_METHODS, f"{tool} ne doit pas être exposé : {reason}"
        assert tool not in MCP_EXPOSED_TOOLS

    def test_the_allowlist_actually_shrank_the_surface(self):
        """Garde-fou du test lui-même : si l'intersection disparaissait,
        tout redeviendrait exposé sans que rien n'échoue."""
        assert len(EXPOSED_METHODS) < len(_public_async_methods())
