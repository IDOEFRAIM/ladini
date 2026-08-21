"""Résilience DURE de l'agent — chantier 2026-08-19.

Contrairement à `test_buyer_deviation_adaptivity.py` /
`test_producer_flow_resolvers.py` (qui verrouillent le TON adaptatif), ce
fichier verrouille les mécanismes qui empêchent un utilisateur — même
espiègle ou incompris — de bloquer l'agent ou de recevoir une information
fausse :

1. La sortie de secours automatique (`abandon_tunnel_max_retries`) est
   réellement ATTEIGNABLE. Elle était du code mort.
2. Une panne technique de la recherche catalogue n'est jamais rendue comme
   « produit non disponible ».
3. Les appels réseau de la précommande ne laissent pas l'acheteur coincé
   dans une phase incohérente.
"""
from __future__ import annotations

import pytest

from agriconnect.graphs.agents.market_coach.nodes.cleanup import post_response_cleanup
from agriconnect.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from tests.conftest import make_state, run


# =====================================================================
# 1. SORTIE DE SECOURS — le compteur d'échecs doit vivre et aboutir
# =====================================================================

class TestTunnelEscapeHatchIsReachable:
    """BUG CAPITAL (2026-08-19) : `abandon_tunnel_max_retries` était
    INATTEIGNABLE — double verrou. (a) `cognitive_guard`'s branche
    `recover_active_tunnel` RAPPORTAIT `retry_count` sans jamais
    l'incrémenter ; (b) `post_response_cleanup` le remettait à 0 en fin de
    CHAQUE tour. Un utilisateur dont les messages ne sont pas classifiables
    restait donc piégé indéfiniment dans le même tunnel, sans qu'aucune
    sortie automatique ne se déclenche jamais."""

    def _stuck_state(self, retry_count=0, **extra):
        return make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="QUANTITY",
            interpreted_event="UNKNOWN",
            status="WAITING_INPUT",
            retry_count=retry_count,
            normalized_text="n'importe quoi d'incompréhensible",
            **extra,
        )

    def test_recovery_increments_the_retry_counter(self):
        result = run(cognitive_guard(self._stuck_state(retry_count=0), None))
        assert result["cognitive_decision"]["action"] == "recover_active_tunnel"
        assert result["retry_count"] == 1, "sans incrément, le seuil n'est jamais atteint"

    def test_cleanup_preserves_the_counter_while_the_tunnel_is_alive(self):
        """Le compteur doit survivre au nettoyage de fin de tour tant que le
        tunnel vit — sinon l'incrément ci-dessus est effacé immédiatement."""
        state = self._stuck_state(retry_count=1)
        patch = run(post_response_cleanup(state, None))
        assert patch.get("retry_count", 1) == 1, "le compteur ne doit pas être remis à 0"

    def test_cleanup_still_clears_the_counter_once_the_tunnel_ends(self):
        """Non-régression : une opération terminée repart bien de zéro."""
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="NONE",
            status="COMPLETED",
            retry_count=2,
        )
        patch = run(post_response_cleanup(state, None))
        assert patch["retry_count"] == 0

    def test_the_escape_hatch_actually_fires_end_to_end(self):
        """Le scénario complet : l'utilisateur reste incompris tour après
        tour, l'agent finit par lâcher prise PROPREMENT (goal effacé, panier
        de state réinitialisé) au lieu de boucler à l'infini."""
        state = self._stuck_state()
        actions = []
        for _ in range(4):
            out = run(cognitive_guard(dict(state), None))
            actions.append(out["cognitive_decision"]["action"])
            state.update({k: v for k, v in out.items() if k != "cognitive_decision"})
            if actions[-1] == "abandon_tunnel_max_retries":
                break
            state.update(run(post_response_cleanup(dict(state), None)))
            state["interpreted_event"] = "UNKNOWN"
            state["status"] = "WAITING_INPUT"

        assert "abandon_tunnel_max_retries" in actions, f"toujours piégé : {actions}"
        assert state["current_goal"] is None
        assert state["transaction_payload"] == {"__reset__": True}

    def test_a_understood_turn_resets_the_counter(self):
        """Le compteur mesure des échecs CONSÉCUTIFS. Sans ce reset, un
        utilisateur qui bute deux fois, se fait comprendre, puis bute une
        seule fois de plus se faisait éjecter de son opération alors qu'il
        progressait."""
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="QUANTITY",
            interpreted_event="ANSWER",
            retry_count=2,
        )
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] == "continue"
        assert result["retry_count"] == 0

    def test_a_progressing_user_is_never_ejected(self):
        """Scénario complet : échec, échec, succès, échec → l'opération doit
        survivre (contraste avec `test_the_escape_hatch_actually_fires`)."""
        state = self._stuck_state()
        for ev in ("UNKNOWN", "UNKNOWN", "ANSWER", "UNKNOWN"):
            state["interpreted_event"] = ev
            out = run(cognitive_guard(dict(state), None))
            action = out["cognitive_decision"]["action"]
            assert action != "abandon_tunnel_max_retries", (
                f"éjecté sur '{ev}' alors que l'utilisateur progressait"
            )
            state.update({k: v for k, v in out.items() if k != "cognitive_decision"})
            state.update(run(post_response_cleanup(dict(state), None)))
            state["status"] = "WAITING_INPUT"
        assert state["current_goal"] == "SALES_PUBLISH_PRODUCT"

    def test_a_shared_location_still_never_counts_as_a_failure(self):
        """Non-régression [[gps-delivery-burkina-faso-2026-08]] : un partage
        GPS natif arrive toujours en event=UNKNOWN — il ne doit ni
        incrémenter le compteur ni déclencher l'abandon."""
        state = self._stuck_state(retry_count=5, location_shared=True)
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] == "continue"
        # Un partage GPS est un tour LÉGITIME : il ne doit ni abandonner le
        # tunnel, ni faire monter le compteur d'échecs (le remettre à 0 est
        # au contraire le comportement voulu — c'est un tour réussi).
        assert result.get("retry_count", 0) == 0


# =====================================================================
# 2. PANNE CATALOGUE ≠ PRODUIT INEXISTANT
# =====================================================================

class TestCatalogOutageIsNeverReportedAsMissingProduct:
    """`resolve_product_vendors` renvoyait `([], False)` AUSSI BIEN pour un
    catalogue réellement vide que pour une panne MCP — les deux appelants
    rendent une liste vide par « 📭 Le produit X n'est pas disponible dans
    notre catalogue ». Une simple panne backend affirmait donc à l'acheteur
    que le produit n'existe pas, et l'orientait vers un appel d'offres
    inutile pour un produit pourtant en stock."""

    def _boom_service(self, monkeypatch, module):
        from agriconnect.graphs.agents.market_coach.services.domain import cart_service as cs

        async def _boom(self, phone, product_name):
            raise cs.ProductLookupUnavailable("mcp down")

        monkeypatch.setattr(cs.CartDomainService, "resolve_product_vendors", _boom)

    def test_cart_reports_a_technical_outage_not_an_empty_catalog(self, monkeypatch):
        from agriconnect.graphs.agents.market_coach.flows.buyer import cart as mod

        self._boom_service(monkeypatch, mod)
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            transaction_payload={"product": "tomates", "quantity": 50},
        )
        result = run(mod.cart_management(state, None))
        text = result["final_response"]
        assert "technique" in text.lower()
        assert "n'est pas disponible dans notre catalogue" not in text

    def test_procurement_reports_a_technical_outage_not_an_empty_catalog(self, monkeypatch):
        from agriconnect.graphs.agents.market_coach.flows.buyer import procurement as mod

        self._boom_service(monkeypatch, mod)
        state = make_state(
            current_goal="BUYER_REQUEST",
            transaction_payload={"product": "oignons"},
        )
        result = run(mod.buyer_request_resolver(state, None))
        text = result["final_response"]
        assert "technique" in text.lower()
        assert "Aucun produit disponible" not in text

    def test_a_genuinely_empty_catalog_still_says_so(self, monkeypatch):
        """Non-régression : le vrai « aucun vendeur » garde son message
        d'origine — on ne masque pas les catalogues réellement vides."""
        from agriconnect.graphs.agents.market_coach.flows.buyer import cart as mod
        from agriconnect.graphs.agents.market_coach.services.domain import cart_service as cs

        async def _empty(self, phone, product_name):
            return [], False

        monkeypatch.setattr(cs.CartDomainService, "resolve_product_vendors", _empty)
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            transaction_payload={"product": "tomates", "quantity": 50},
        )
        result = run(mod.cart_management(state, None))
        assert "n'est pas disponible dans notre catalogue" in result["final_response"]


# =====================================================================
# 3. PRÉCOMMANDE — une panne réseau ne doit pas coincer l'acheteur
# =====================================================================

class TestPreorderNetworkFailuresKeepTheBuyerUnstuck:
    CART = [{
        "product_id": "p1", "name": "tomates", "quantity": 10,
        "unit": "KG", "price": 225, "producer_id": "prod1", "status": "ACTIVE",
    }]

    def test_draft_creation_failure_returns_the_buyer_to_the_cart(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.flows.buyer.preorder as mod

        class _Boom:
            def __init__(self, rt):
                pass

            async def create_draft(self, **kwargs):
                raise RuntimeError("mcp timeout")

        monkeypatch.setattr(mod, "PreorderGateway", _Boom)
        state = make_state(
            current_goal="BUYER_PREORDER_INIT",
            active_cart=self.CART,
            preorder_workflow={"phase": "CART"},
        )
        result = run(mod.create_preorder(state, None))
        assert result["response_strategy"] == "ERROR"
        assert result["preorder_workflow"]["phase"] == "CART"
        assert "panier est conservé" in result["final_response"]

    def test_confirmation_failure_keeps_the_draft_for_a_retry(self, monkeypatch):
        """Le brouillon reste intact (aucun stock débité) — l'acheteur peut
        simplement redire « oui » au tour suivant."""
        import agriconnect.graphs.agents.market_coach.flows.buyer.preorder as mod
        from agriconnect.core.settings import settings

        monkeypatch.setattr(settings, "ESCROW_PAYMENT_ENABLED", False)

        class _Boom:
            def __init__(self, rt):
                pass

            async def confirm_draft(self, **kwargs):
                raise RuntimeError("mcp timeout")

        monkeypatch.setattr(mod, "PreorderGateway", _Boom)
        state = make_state(
            current_goal="BUYER_PREORDER_CONFIRM",
            active_cart=self.CART,
            preorder_workflow={
                "phase": "PREORDER_DRAFTED", "preorder_id": "abc123",
                "gps_stage": True, "gps_default": {"lat": 12.35, "lon": -1.5},
            },
            transaction_payload={"resolved_id": "PREORDER_CONFIRM"},
        )
        result = run(mod.create_preorder(state, None))
        assert result["response_strategy"] == "ERROR"
        assert result["preorder_workflow"]["phase"] == "PREORDER_DRAFTED"
        assert result["preorder_workflow"]["preorder_id"] == "abc123"
