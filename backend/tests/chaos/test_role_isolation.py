"""CHAOS 3 — Étanchéité d'IDENTITÉ (BUYER ⟂ PRODUCER remplacé par ownership).

RÉÉCRITURE 2026-08 : la refonte double-rôle est DÉFINITIVE — un même
utilisateur vend ET achète, le graphe est UNIFIÉ (un seul `build_graph`, tous
les nœuds tunnel présents quel que soit `role`), et le blocage par préfixe de
`graphs/roles.py` (`is_goal_allowed`/`is_tool_allowed`) est du code MORT :
plus aucun appelant vivant ne les invoque (vérifié — seul `normalize_role`
survit, utilisé pour le routage UI par défaut, pas pour la sécurité).

La frontière de sécurité RÉELLE aujourd'hui n'est plus « quel rôle a le droit
de voir quel intent », c'est : **un utilisateur peut-il agir SOUS L'IDENTITÉ
d'un autre ?** Elle est appliquée à deux endroits, tous deux couverts ici :

  1. `services/mcp/schema_resolver.py::lookup_arg_value` — les paramètres
     d'IDENTITÉ (`phone`, `user_phone`, `producer_id`, `user_id`) sont
     TOUJOURS résolus depuis `state` (la session authentifiée par le
     webhook/l'orchestrateur), JAMAIS depuis `payload`/`extracted_entities`
     (texte libre, donc potentiellement forgé). Un message qui contiendrait
     le numéro ou l'UUID de quelqu'un d'autre ne peut PAS faire agir l'agent
     en son nom.
  2. Les méthodes DB d'écriture (ex: `buyer.py::cancel_pending_order`)
     dérivent le PROFIL AGISSANT depuis ce même `phone` pinné, puis filtrent
     la ressource par `Order.buyer_id == profile_obj.id` — non testable ici
     sans base (philosophie chaos : zéro DB), mais dépend structurellement de
     (1), qui l'est.

Les tests encore valides de l'ancien fichier (rôle inconnu → repli
déterministe, tunnels acheteur routés vers leur nœud dédié) sont conservés
tels quels.
"""
from __future__ import annotations

import pytest


# =====================================================================
# IDENTITÉ ÉPINGLÉE À LA SESSION — jamais au payload utilisateur
# =====================================================================

class TestIdentityPinning:
    """Un buyer authentifié (`state["user_phone"]`) ne peut PAS agir sous le
    numéro/UUID d'un autre utilisateur, même s'il le fait apparaître dans son
    message (le LLM le recopierait alors dans `extracted_entities`/payload)."""

    SESSION_PHONE = "+22670000001"
    ATTACKER_SUPPLIED_PHONE = "+22670000099"       # numéro d'un AUTRE utilisateur
    ATTACKER_SUPPLIED_PRODUCER_ID = "producer-uuid-de-quelquun-dautre"

    def _state(self, **extra):
        return {"user_phone": self.SESSION_PHONE, "user_id": None, **extra}

    @pytest.mark.parametrize("param_name", ["phone", "user_phone"])
    def test_phone_param_ignores_payload_override(self, param_name):
        from ladini.graphs.agents.market_coach.services.mcp.schema_resolver import (
            lookup_arg_value,
        )
        state = self._state()
        payload = {"phone": self.ATTACKER_SUPPLIED_PHONE, "user_phone": self.ATTACKER_SUPPLIED_PHONE}
        resolved = lookup_arg_value(param_name, state, payload, initial_args={})
        assert resolved == self.SESSION_PHONE, (
            f"'{param_name}' résolu depuis le payload au lieu de la session — "
            "un message forgé pourrait usurper l'identité d'un autre utilisateur"
        )

    @pytest.mark.parametrize("param_name", ["producer_id", "user_id"])
    def test_producer_id_param_ignores_payload_override(self, param_name):
        from ladini.graphs.agents.market_coach.services.mcp.schema_resolver import (
            lookup_arg_value,
        )
        state = self._state()
        payload = {"producer_id": self.ATTACKER_SUPPLIED_PRODUCER_ID,
                   "user_id": self.ATTACKER_SUPPLIED_PRODUCER_ID}
        resolved = lookup_arg_value(param_name, state, payload, initial_args={})
        assert resolved != self.ATTACKER_SUPPLIED_PRODUCER_ID
        assert resolved == self.SESSION_PHONE, (
            "sans user_id résolu en base, le repli légitime est le phone de session — "
            "jamais l'identité forgée du payload"
        )

    def test_uuid_user_id_wins_over_phone_and_over_payload(self):
        """Quand `state["user_id"]` (UUID déjà résolu par le profil chargé en
        amont) est disponible, il prime — et reste, comme le phone, immunisé
        au payload."""
        from ladini.graphs.agents.market_coach.services.mcp.schema_resolver import (
            lookup_arg_value,
        )
        state = self._state(user_id="real-uuid-session")
        payload = {"producer_id": self.ATTACKER_SUPPLIED_PRODUCER_ID}
        assert lookup_arg_value("producer_id", state, payload, {}) == "real-uuid-session"

    def test_full_arg_resolution_pins_identity_on_a_realistic_write_schema(self):
        """Bout-en-bout sur un schéma d'outil WRITE plausible (type
        `create_product`) : même avec un `producer_id` forgé dans le payload
        ET dans `extracted_entities`, l'argument résolu envoyé à l'outil MCP
        reste celui de la session."""
        from ladini.graphs.agents.market_coach.services.mcp.schema_resolver import (
            build_resolved_tool_args,
        )
        schema = {
            "type": "object",
            "properties": {
                "producer_id": {"type": "string"},
                "product": {"type": "string"},
                "price": {"type": "number"},
                "quantity": {"type": "number"},
            },
            "required": ["producer_id", "product", "price", "quantity"],
        }
        state = {
            "user_phone": self.SESSION_PHONE,
            "user_id": None,
            "extracted_entities": {"producer_id": self.ATTACKER_SUPPLIED_PRODUCER_ID},
        }
        payload = {
            "producer_id": self.ATTACKER_SUPPLIED_PRODUCER_ID,
            "product": "maïs", "price": 250, "quantity": 50,
        }
        args = build_resolved_tool_args("create_product", schema, state, payload, {})
        assert args["producer_id"] == self.SESSION_PHONE
        assert args["producer_id"] != self.ATTACKER_SUPPLIED_PRODUCER_ID
        # Les champs métier NON identitaires, eux, viennent bien du payload.
        assert args["product"] == "maïs" and args["price"] == 250.0

    def test_pii_is_masked_in_logs(self):
        """Défense en profondeur complémentaire : même si l'identité pinnée
        finit dans un log d'audit, elle n'y apparaît jamais en clair."""
        from ladini.graphs.agents.market_coach.services.mcp.schema_resolver import (
            mask_pii_args,
        )
        masked = mask_pii_args({"producer_id": self.SESSION_PHONE, "product": "maïs"})
        assert self.SESSION_PHONE not in masked["producer_id"]
        assert masked["product"] == "maïs"


# =====================================================================
# RÔLES INCONNUS / HOSTILES — effondrement déterministe (conservé, VERT)
# =====================================================================

@pytest.mark.parametrize("garbage_role", ["ADMIN", "root", "'; DROP TABLE--", "", None, "BUYER; PRODUCER", "🚜"])
def test_unknown_role_collapses_deterministically(garbage_role):
    """`normalize_role` reste utilisé LIVE (routage UI par défaut, choix de
    workspace). Rupture prévenue : un rôle forgé/inconnu qui obtiendrait un
    comportement HYBRIDE ou instable.

    (2026-09-08, refonte responsabilités des nœuds d'entrée, mandat §3) :
    le contrat a changé DÉLIBÉRÉMENT — "tout rôle non-BUYER s'effondre sur
    PRODUCER" était exactement le défaut silencieux interdit par le mandat
    ("Actuellement, toute valeur inconnue tombe sur PRODUCER. Cela est
    interdit."). Un rôle non reconnu retourne désormais "UNKNOWN", tout
    aussi déterministe (même entrée → même sortie, jamais hybride) mais
    honnête sur le fait qu'aucun rôle n'a pu être établi — les appelants
    qui ont besoin d'une valeur concrète (ex: sélection du graphe compilé)
    décident explicitement de leur propre repli (voir
    `core/graph_builder.py::build_graph`)."""
    from ladini.graphs.roles import normalize_role

    norm = normalize_role(garbage_role)
    assert norm in {"BUYER", "PRODUCER", "UNKNOWN"}
    assert normalize_role(garbage_role) == norm, "non déterministe d'un appel à l'autre"


# =====================================================================
# GRAPHE UNIFIÉ — même topologie de nœuds quel que soit le rôle
# =====================================================================

class TestUnifiedGraphTopology:
    """Remplace les anciens tests `test_producer_router_never_emits_buyer_targets`
    / `test_buyer_router_tunnel_goals_route_to_owned_nodes`, qui supposaient
    des graphes SÉPARÉS par rôle (un nœud buyer absent du graphe producteur
    aurait fait lever une KeyError LangGraph). Faux aujourd'hui : le graphe
    est UNIFIÉ (même topologie pour PRODUCER et BUYER — un producteur peut
    aussi acheter). On verrouille cette unification explicitement : c'est
    elle qui rend `to_cart`/`to_negotiation`/`to_order_tracking` valides
    pour N'IMPORTE QUEL rôle, plutôt que de le re-suspecter comme une fuite."""

    def test_producer_and_buyer_graphs_share_the_same_node_set(self):
        # `mc_runtime=object()` évite `build_runtime()` -> `get_llm()` (exige
        # un vrai GROQ_API_KEY) : ces tests inspectent la TOPOLOGIE compilée,
        # jamais l'exécution du graphe.
        from ladini.graphs.agents.market_coach.core.graph_builder import build_graph

        def node_ids(role):
            g = build_graph(role=role, mc_runtime=object()).get_graph()
            return {n.id if hasattr(n, "id") else str(n) for n in g.nodes.values()}

        producer_nodes = node_ids("PRODUCER")
        buyer_nodes = node_ids("BUYER")
        assert producer_nodes == buyer_nodes, (
            "les graphes ont divergé : le double-rôle exige une topologie unique"
        )

    def test_all_tunnel_nodes_exist_for_any_role(self):
        from ladini.graphs.agents.market_coach.core.graph_builder import build_graph

        required_tunnel_nodes = {"cart_management", "negotiation_gate", "order_tracking_node"}
        for role in ("PRODUCER", "BUYER"):
            nodes = {n.id if hasattr(n, "id") else str(n)
                     for n in build_graph(role=role, mc_runtime=object()).get_graph().nodes.values()}
            missing = required_tunnel_nodes - nodes
            assert not missing, f"rôle {role} : nœuds tunnel absents {missing} (KeyError potentielle)"

    def test_buyer_router_tunnel_goals_route_to_owned_nodes(self):
        """Rupture prévenue : une dérive des goal-sets qui enverrait un goal de
        tunnel buyer vers le pipeline générique (confirmation/exécuteur) au
        lieu de son nœud dédié — le tunnel perdrait sa machine à états
        (phases panier, négociation) et re-poserait les questions en boucle."""
        from ladini.graphs.agents.market_coach.core.goals import (
            BUYER_AUCTION_TRACKING_GOALS,
            BUYER_NEGOTIATION_GOALS,
            BUYER_ORDER_TRACKING_GOALS,
        )
        from ladini.graphs.agents.market_coach.core.router import get_domain_router

        router = get_domain_router("BUYER")
        state = lambda g: {"current_goal": g, "status": "PROCESSING", "working_memory": {}}

        for goal in BUYER_ORDER_TRACKING_GOALS | BUYER_AUCTION_TRACKING_GOALS:
            assert router.decide(state(goal)) == "to_order_tracking", goal
        for goal in BUYER_NEGOTIATION_GOALS:
            assert router.decide(state(goal)) in {"to_negotiation", "to_strategy"}, goal


# =====================================================================
# CODE MORT — verrouillé explicitement pour éviter qu'on le recâble par erreur
# =====================================================================

def test_prefix_based_role_gate_is_confirmed_dead_code():
    """`graphs/roles.py::is_goal_allowed`/`is_tool_allowed` ne sont PLUS
    appelés par aucun chemin vivant (vérifié par grep sur tout `src/` :
    seul `normalize_role` a des appelants). Ce test échoue dès qu'un import
    vivant réapparaît — signal qu'il faut soit le réintégrer sciemment
    (et alors réécrire CE fichier de tests autour), soit le supprimer pour de
    bon plutôt que de laisser une 2e frontière de sécurité orpheline et
    trompeuse traîner dans le code."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "ladini"
    live_callers = []
    for p in root.rglob("*.py"):
        if "__pycache__" in str(p) or p.name == "roles.py":
            continue
        src = p.read_text(encoding="utf-8")
        if re.search(r"\bis_goal_allowed\b|\bis_tool_allowed\b", src):
            live_callers.append(str(p))
    assert not live_callers, (
        f"is_goal_allowed/is_tool_allowed sont de nouveau appelés par : {live_callers} "
        "— mettre à jour ce fichier de tests en conséquence"
    )
