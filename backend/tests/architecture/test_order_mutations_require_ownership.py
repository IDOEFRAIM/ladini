"""INVARIANTS DU MODÈLE DE PRODUCTION (Phase 6B, 2026-09-05).

Deux propriétés que la production doit garantir, encodées ici en contrats
plutôt qu'en simples tests de régression :

1. **Une nouvelle commande de checkout = un producteur.** L'isolement de la
   responsabilité producteur (clôture, annulation, montant, notification)
   repose entièrement là-dessus. Si un chemin réintroduit une commande
   multi-producteurs, ces garanties retombent silencieusement.

2. **Toute mutation d'état d'une commande vérifie la propriété.** Une
   mutation qui accepte un `order_id` et écrit un statut sans contrôler qui
   appelle contourne, par construction, toutes les gardes bâties autour du
   cycle de vie des commandes."""
from __future__ import annotations

import inspect

import pytest

from ladini.infrastructure.mcp.security import TOOL_SCOPE_MAP
from ladini.services.database.buyer import BuyerMixin
from ladini.services.database.producer import ProducerMgmtMixin


def _is_prose(grep_line: str, symbol: str) -> bool:
    """`git grep -n` renvoie "chemin:numéro:contenu" — seul le CONTENU compte.

    Ces deux gardes cherchent des APPELANTS ; une simple MENTION du nom dans
    du texte n'en est pas un. Deux tests étaient rouges en permanence à cause
    de ça (audit 2026-09-10) : `infrastructure/mcp/exposure.py` documente
    dans sa docstring, en puces Markdown, les outils dangereux — dont ces
    deux-là. Un test de garde qui échoue toujours n'apprend plus rien à
    personne : il apprend juste à ignorer sa propre alerte.

    Deux critères, tous deux non ambigus :
      - la ligne commence par un marqueur de commentaire ou de puce ;
      - le symbole apparaît entre backticks. Le backtick n'est PAS une
        syntaxe Python valide : entouré de backticks, un nom est forcément
        de la documentation, jamais un appel.
    """
    content = grep_line.split(":", 2)[-1].strip()
    if content.startswith(("#", '"', "*", "-", ">")):
        return True
    return f"`{symbol}`" in content


class TestOrderStateMutationsAreOwnershipChecked:
    """Chaque mutation d'état de commande réellement atteignable doit
    résoudre un acteur ET filtrer la commande par cet acteur."""

    OWNERSHIP_CHECKED = {
        # (méthode, marqueurs prouvant la résolution d'acteur + le filtrage)
        (ProducerMgmtMixin.confirm_delivery_and_payment, ("get_producer_profile", "not_owner")),
        (ProducerMgmtMixin.cancel_confirmed_order, ("get_producer_profile", "not_owner")),
        (BuyerMixin.cancel_pending_order, ("get_buyer_profile", "Order.buyer_id ==")),
        (BuyerMixin.confirm_preorder_draft, ("get_buyer_profile", "Order.buyer_id ==")),
        (BuyerMixin.cancel_preorder_draft, ("get_buyer_profile", "Order.buyer_id ==")),
    }

    @pytest.mark.parametrize(
        "method,markers",
        sorted(OWNERSHIP_CHECKED, key=lambda pair: pair[0].__qualname__),
        ids=lambda v: v.__qualname__ if callable(v) else "",
    )
    def test_mutation_resolves_and_filters_by_actor(self, method, markers):
        source = inspect.getsource(method)
        for marker in markers:
            assert marker in source, (
                f"{method.__qualname__} ne prouve plus le contrôle de propriété "
                f"(marqueur absent : {marker!r})"
            )

    def test_update_order_status_is_not_reachable_through_mcp(self):
        """`update_order_status` écrit `status`/`payment_status` à des valeurs
        arbitraires SANS contrôle de propriété ni garde de statut. Elle n'a
        aucun appelant et doit rester hors de la carte des scopes : le
        fail-closed de `runtime.py::call_tool` la refuse alors."""
        assert "update_order_status" not in TOOL_SCOPE_MAP, (
            "update_order_status redevient appelable via MCP alors qu'elle "
            "contourne toutes les gardes du cycle de vie des commandes"
        )

    def test_update_order_status_still_has_no_caller(self):
        """Si quelqu'un la câble un jour, il devra d'abord lui donner un
        contrôle de propriété — ce test le lui rappellera."""
        import subprocess

        result = subprocess.run(
            ["git", "grep", "-n", "update_order_status", "--", "src/*.py", "src/**/*.py"],
            capture_output=True, text=True,
        )
        callers = [
            line
            for line in result.stdout.splitlines()
            if "def update_order_status" not in line
            and not _is_prose(line, "update_order_status")
            and "security.py" not in line
            and "marketplace.py" not in line
        ]
        assert not callers, f"update_order_status a désormais des appelants : {callers}"


class TestSystemOnlyMutationsAreNotExposedThroughMcp:
    """(Phase 7) Généralisation du cas `update_order_status` : une mutation
    SYSTÈME — sans acteur ni contrôle de propriété par nature — ne doit pas
    être appelable comme outil MCP. Ses vrais appelants passent
    directement par `AgriDatabaseService()`, jamais par MCP."""

    SYSTEM_ONLY = {
        # tool -> ce qu'il permettrait s'il restait exposé
        "update_order_status": "écrire status/payment_status arbitraires sur n'importe quelle commande",
        "mark_escrow_paid": "déclarer un paiement reçu sans le fournisseur de paiement",
        "expire_pending_payments": "expirer/annuler en masse les commandes en attente",
    }

    @pytest.mark.parametrize("tool,risk", sorted(SYSTEM_ONLY.items()))
    def test_tool_is_fail_closed(self, tool, risk):
        assert tool not in TOOL_SCOPE_MAP, (
            f"`{tool}` redevient appelable via MCP — cela permettrait de {risk}"
        )


class TestNoExposedToolAcceptsAnArbitraryStatus:
    """(Phase 7) Un outil exposé qui écrit un statut reçu en paramètre doit
    valider ce qu'il accepte : sinon un appelant MCP peut placer une entité
    dans n'importe quel état, en contournant toutes les transitions."""

    def test_cancel_preorder_draft_whitelists_its_target_status(self):
        source = inspect.getsource(BuyerMixin.cancel_preorder_draft)
        assert 'target_status not in ("CANCELLED", "SUPERSEDED")' in source, (
            "cancel_preorder_draft écrit `target_status` sans liste blanche : "
            "un appelant MCP pourrait faire passer son brouillon à COMPLETED"
        )
        assert "invalid_target_status" in source

    def test_no_other_exposed_write_tool_takes_a_status_parameter(self):
        """Inventaire verrouillé : si un nouvel outil d'écriture accepte un
        statut, il doit être ajouté ici APRÈS avoir été validé."""
        import inspect as _inspect

        from ladini.protocols.mcp.servers.h import TOOL_DESCRIPTIONS
        from ladini.services.database.d import AgriDatabaseService

        offenders = []
        for tool in sorted(TOOL_DESCRIPTIONS):
            scope = TOOL_SCOPE_MAP.get(tool)
            if scope is None or scope.name != "DB_DATA_WRITE":
                continue
            method = getattr(AgriDatabaseService, tool, None)
            if not callable(method):
                continue
            try:
                params = list(_inspect.signature(method).parameters)
            except (TypeError, ValueError):
                continue
            if any("status" in p.lower() for p in params):
                offenders.append(tool)
        assert offenders == ["cancel_preorder_draft"], (
            f"Nouvel outil d'écriture acceptant un statut : {offenders}. "
            "Vérifier qu'il valide les valeurs acceptées."
        )


class TestOneNewOrderOneProducer:
    """Invariant métier du modèle de production (Phase 6A)."""

    def test_checkout_creates_one_order_per_producer(self):
        """Le regroupement par producteur doit rester la structure même du
        checkout — pas un filtrage ajouté après coup."""
        source = inspect.getsource(BuyerMixin.create_preorder_draft)
        assert "_order_for_producer" in source
        assert "checkout_group_id" in source
        # La commande est créée À PARTIR du producteur de la ligne, jamais
        # une commande unique remplie ensuite.
        assert "await _order_for_producer(product.producer_id)" in source

    def test_no_live_path_puts_two_producers_in_one_order(self):
        """Les seuls autres chemins créant des `OrderItem` sont soit morts,
        soit mono-producteur par construction. Ce test échoue si l'un d'eux
        est câblé sans traiter le regroupement."""
        import subprocess

        result = subprocess.run(
            ["git", "grep", "-n", "OrderItem(", "--", "src/*.py", "src/**/*.py"],
            capture_output=True, text=True,
        )
        sites = {
            line.split(":")[0]
            for line in result.stdout.splitlines()
            if "class OrderItem" not in line
        }
        expected = {
            # le checkout groupé (Phase 6A) — regroupe par producteur
            "src/ladini/services/database/buyer.py",
            # record_sale : UN produit du producteur lui-même
            "src/ladini/services/database/marketplace.py",
            # OrderService : code mort confirmé (zéro appelant)
            "src/ladini/services/database/order_service.py",
            # accept_match_proposal (VS4, approvisionnement récurrent) —
            # même structure que le checkout groupé : `by_producer` regroupe
            # les allocations PAR producteur AVANT toute création de commande,
            # une commande naît de CE regroupement (jamais l'inverse), voir
            # `test_recurring_need_confirmation_service.py::
            # test_accepting_a_multi_producer_occurrence_creates_one_order_per_producer`
            # (PostgreSQL réel) qui prouve 2 producteurs -> 2 commandes.
            "src/ladini/services/database/recurring_supply.py",
        }
        assert sites <= expected, (
            f"Nouveau site de création d'`OrderItem` : {sites - expected}. "
            "Vérifier qu'il ne peut pas produire une commande multi-producteurs."
        )

    def test_finalize_multi_order_stays_unwired(self):
        """`finalize_multi_order` crée UNE commande avec les articles de TOUS
        les producteurs (docstring : « commande ferme multi-produits »). Elle
        n'a jamais été câblée au checkout ; la câbler telle quelle
        réintroduirait exactement le P1 fermé en Phase 6A."""
        import subprocess

        result = subprocess.run(
            ["git", "grep", "-n", "finalize_multi_order", "--", "src/*.py", "src/**/*.py"],
            capture_output=True, text=True,
        )
        real_callers = [
            line
            for line in result.stdout.splitlines()
            if "async def finalize_multi_order" not in line
            and not _is_prose(line, "finalize_multi_order")
        ]
        assert not real_callers, (
            "finalize_multi_order est désormais appelée — elle produit une "
            f"commande multi-producteurs : {real_callers}"
        )


class TestAuctionOrdersRemainMonoProducer:
    def test_auction_path_creates_no_order_items_and_one_winning_bid(self):
        from ladini.services.database.auction import AuctionMixin

        source = inspect.getsource(AuctionMixin.select_winning_bid)
        assert "OrderItem(" not in source
        assert "winning_bid_id=bid.id" in source
        assert "checkout_group_id" not in source
