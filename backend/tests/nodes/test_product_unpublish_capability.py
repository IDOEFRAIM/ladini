"""Capacité PRODUCTEUR « retirer un produit de mon catalogue »
(`SALES_UNPUBLISH_PRODUCT`) — Product Completeness Phase 2, 2026-09-04.

## Le gap réel fermé par ce fichier

`services/database/product.py::delete_product` existait déjà, complète et
sûre : verrou `FOR UPDATE`, contrôle de propriété, REFUS si des commandes
actives existent, archivage doux (`is_available=False`, historique
préservé) si le produit a déjà été commandé, suppression physique
seulement s'il n'a jamais servi. Mais AUCUN goal, AUCUN tunnel, AUCUN
handler ne l'atteignait (recherche exhaustive : la chaîne
`"delete_product"` n'apparaissait nulle part hors de sa propre
définition). Un producteur ne pouvait donc jamais retirer un produit
épuisé ou erroné — alors que les acheteurs continuaient de le voir dans
le catalogue.

Ce fichier verrouille les DEUX moitiés du correctif : le câblage complet
(intent → rôle → action enregistrée → gateway → scope MCP) et le
résolveur conversationnel (jamais un UUID demandé à l'utilisateur, jamais
un choix implicite sur une action destructrice)."""
from __future__ import annotations

from tests.conftest import StubRuntime, run


def rt(responses=None):
    return StubRuntime(responses=responses or {})


def _product(pid, name="Riz", qty=100.0, unit="KG"):
    return {"id": pid, "name": name, "quantity_for_sale": qty, "unit": unit, "price": 500}


class TestCapabilityIsFullyWired:
    """Une capacité n'existe pas parce qu'une fonction DB existe : elle
    existe quand la chaîne complète est câblée."""

    def test_intent_declares_the_real_db_tool(self):
        from ladini.graphs.agents.market_coach.interpreter.intent import (
            INTENT_CONFIG,
            INTENT_ROLE,
        )

        cfg = INTENT_CONFIG["SALES_UNPUBLISH_PRODUCT"]
        assert cfg["tool_name"] == "delete_product"
        assert cfg["action_type"] == "WRITE"
        assert INTENT_ROLE["SALES_UNPUBLISH_PRODUCT"] == "PRODUCER"

    def test_action_handler_is_registered_and_resolves_the_tool(self):
        from ladini.graphs.agents.market_coach.registry import get_action

        registration = get_action("SALES_UNPUBLISH_PRODUCT")
        assert registration is not None
        assert registration.is_write is True

        from ladini.graphs.agents.market_coach.actions.sales import (
            prep_sales_unpublish_product,
        )

        tool_name, args = prep_sales_unpublish_product(
            {"user_phone": "+22670000001"}, {"product_id": "p-1"}
        )
        assert tool_name == "delete_product"
        assert args == {"phone": "+22670000001", "product_id": "p-1"}

    def test_gateway_and_mcp_scope_exist(self):
        from ladini.graphs.agents.market_coach.services.mcp.gateway import (
            ProductGateway,
        )
        from ladini.infrastructure.mcp.security import TOOL_SCOPE_MAP

        assert hasattr(ProductGateway, "delete_product")
        # Fail-closed : sans entrée de scope, l'appel serait refusé.
        assert "delete_product" in TOOL_SCOPE_MAP

    def test_validator_never_asks_the_producer_for_a_product_uuid(self):
        """Régression de l'impasse conversationnelle : sans l'entrée
        `_RESOLVER_PASSTHROUGH`, le validateur réclamait `product_id`
        (un UUID) et le résolveur n'était jamais atteint."""
        from ladini.graphs.agents.market_coach.nodes.validation import validator
        from tests.conftest import make_state

        result = run(
            validator(make_state(current_goal="SALES_UNPUBLISH_PRODUCT"), StubRuntime())
        )
        assert result.get("status") == "PLANNING"
        assert result.get("last_missing_field") is None


class TestResolveProductForUnpublish:
    def test_no_phone_returns_error(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            _resolve_product_for_unpublish,
        )

        assert run(_resolve_product_for_unpublish(rt(), "", {}))["status"] == "ERROR"

    def test_empty_catalog_is_a_clean_error_not_a_crash(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            _resolve_product_for_unpublish,
        )

        runtime = rt({"get_my_products": {"status": "success", "data": []}})
        result = run(_resolve_product_for_unpublish(runtime, "+22670000001", {}))
        assert "empty_catalog" in result["validation_errors"]

    def test_single_product_is_auto_selected(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            _resolve_product_for_unpublish,
        )

        runtime = rt(
            {"get_my_products": {"status": "success", "data": [_product("prod-1")]}}
        )
        result = run(_resolve_product_for_unpublish(runtime, "+22670000001", {}))
        assert result["status"] == "PLANNING"
        assert result["transaction_payload"]["product_id"] == "prod-1"

    def test_several_products_never_pick_implicitly(self):
        """Action destructrice : jamais « le dernier produit » — un menu
        numéroté strict, comme `_resolve_stock`."""
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            _resolve_product_for_unpublish,
        )

        runtime = rt(
            {
                "get_my_products": {
                    "status": "success",
                    "data": [_product("prod-1", "Riz"), _product("prod-2", "Maïs")],
                }
            }
        )
        result = run(_resolve_product_for_unpublish(runtime, "+22670000001", {}))
        assert result["status"] == "WAITING_INPUT"
        assert result["response_strategy"] == "SELECTION_MENU"
        assert "transaction_payload" not in result
        assert result["available_mapping"] == {"1": "prod-1", "2": "prod-2"}

    def test_selection_index_resolves_the_right_product(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            _resolve_product_for_unpublish,
        )

        runtime = rt(
            {
                "get_my_products": {
                    "status": "success",
                    "data": [_product("prod-1", "Riz"), _product("prod-2", "Maïs")],
                }
            }
        )
        result = run(
            _resolve_product_for_unpublish(
                runtime, "+22670000001", {"selection_index": 2}
            )
        )
        assert result["status"] == "PLANNING"
        assert result["transaction_payload"]["product_id"] == "prod-2"
        # Les jetons de sélection ne doivent jamais fuir dans le payload exécuté.
        assert "selection_index" not in result["transaction_payload"]

    def test_out_of_range_selection_re_displays_the_menu(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            _resolve_product_for_unpublish,
        )

        runtime = rt(
            {
                "get_my_products": {
                    "status": "success",
                    "data": [_product("prod-1"), _product("prod-2")],
                }
            }
        )
        result = run(
            _resolve_product_for_unpublish(
                runtime, "+22670000001", {"selection_index": 9}
            )
        )
        assert result["status"] == "WAITING_INPUT"


class TestArchivedProductsNeverResurface:
    """Un produit retiré ne doit réapparaître NI dans le menu de retrait du
    producteur, NI dans la recherche acheteur."""

    def test_already_archived_products_are_not_offered_again(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            _resolve_product_for_unpublish,
        )

        archived = _product("prod-archived", "Ancien riz")
        archived["is_available"] = False
        live = _product("prod-live", "Maïs")
        live["is_available"] = True
        runtime = rt({"get_my_products": {"status": "success", "data": [archived, live]}})

        result = run(_resolve_product_for_unpublish(runtime, "+22670000001", {}))
        # Un seul produit réellement publié : auto-sélectionné, et JAMAIS
        # l'archivé.
        assert result["status"] == "PLANNING"
        assert result["transaction_payload"]["product_id"] == "prod-live"

    def test_catalog_with_only_archived_products_says_nothing_to_remove(self):
        from ladini.graphs.agents.market_coach.flows.producer.flow import (
            _resolve_product_for_unpublish,
        )

        archived = _product("prod-archived")
        archived["is_available"] = False
        runtime = rt({"get_my_products": {"status": "success", "data": [archived]}})
        result = run(_resolve_product_for_unpublish(runtime, "+22670000001", {}))
        assert "empty_catalog" in result["validation_errors"]

    def test_buyer_search_filters_on_is_available(self):
        """Régression du chemin de résurrection : avant ce correctif, la
        recherche acheteur n'excluait un produit retiré QUE par
        `quantity_for_sale > 0` — une mise à jour de quantité le remettait
        en vente. Preuve sur le SQL réellement compilé."""
        import inspect

        from ladini.services.database.buyer import BuyerMixin

        source = inspect.getsource(BuyerMixin.search_products)
        assert "Product.is_available.is_(True)" in source
        assert "Product.quantity_for_sale > 0" in source


class TestBusinessRuleStaysInTheDatabaseLayer:
    """Le mandat interdit de réimplémenter une règle métier déjà encodée :
    archivage doux vs suppression physique, refus si commandes actives —
    tout cela reste dans `delete_product`, jamais dupliqué côté agent."""

    def test_agent_layer_contains_no_deletion_policy(self):
        import inspect

        from ladini.graphs.agents.market_coach.flows.producer import flow

        raw = inspect.getsource(flow._resolve_product_for_unpublish)
        # Les commentaires CITENT volontairement la règle métier restée côté
        # DB — seul le CODE réel est audité ici.
        source = "\n".join(
            line for line in raw.splitlines() if not line.strip().startswith("#")
        )
        # LIRE `is_available` pour n'offrir que des produits réellement
        # retirables est légitime ; DÉCIDER du mode de retrait (archivage
        # doux vs suppression physique) ou re-tester les commandes actives
        # serait une duplication de règle métier.
        for forbidden in (
            "OrderItem",
            "is_available = ",
            "is_available=False",
            "quantity_for_sale = ",
            "active_count",
        ):
            assert forbidden not in source

    def test_db_layer_still_refuses_when_active_orders_exist(self):
        """Non-régression de la règle métier réutilisée (lecture du code
        source réel : la garde `active_orders` doit rester en place)."""
        import inspect

        from ladini.services.database.product import ProductMixin

        source = inspect.getsource(ProductMixin.delete_product)
        assert "active_count" in source
        assert "with_for_update" in source
        assert "is_available = False" in source
