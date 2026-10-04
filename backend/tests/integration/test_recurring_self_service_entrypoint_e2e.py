"""B21.1 — « mes besoins » ouvre la liste récurrente selon la CAPACITÉ acheteur, pas selon le graphe courant.

Rejoué sur le vrai graphe compilé + le vrai orchestrateur (seuls LLM, Redis, dispatcher et MCP sont doublés) ; la preuve
contre un vrai PostgreSQL est dans `tests/schema/test_recurring_entrypoint_pg.py`. Le LLM est AVEUGLE : aucune
navigation ne lui est classable, donc si la liste s'ouvre c'est que la route est déterministe.

Cause réelle (B21.1) : l'orchestrateur n'ouvre le graphe BUYER que pour un profil BUYER ; un profil ADMIN passe par le
graphe PRODUCER, où la navigation déterministe de B20/B21 (`role_up == "BUYER"`) ne s'appliquait pas.
"""
from __future__ import annotations

import pytest

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.interpreter import context_arbitration as ca
from tests.harness import ConversationHarness, new_task

pytestmark = pytest.mark.integration

UNKNOWN = {"disposition": "UNKNOWN", "intent": None, "confidence": 0.0, "entities": {}}


def _profile(role: str, *, can_buy: bool) -> dict:
    return {
        "id": "11111111-1111-1111-1111-111111111111", "name": "Admin Ido", "role": role,
        "zone": {"id": "zone-1", "name": "Ouagadougou"},
        "profile_ids": {"producer": None, "buyer": "bbbbbbbb-0000-0000-0000-000000000001" if can_buy else None, "delivery": None},
        "permissions": {"can_sell": role in {"ADMIN", "PRODUCER"}, "can_buy": can_buy, "can_deliver": False, "is_admin": role == "ADMIN"},
    }


class _Conv:
    """Conversation dont le service récurrent est une doublure COHÉRENTE : `list_my_recurring_needs` relit ce que
    `create_recurring_need(s)` a créé (jamais l'état du graphe)."""

    def __init__(self, role: str = "ADMIN", *, can_buy: bool = True) -> None:
        self.h = ConversationHarness(role=role, profile=_profile(role, can_buy=can_buy))

    def __enter__(self):
        h = self.h.__enter__()
        server = h.server

        def _list(**_):
            items = [{
                "recurring_need_id": c["recurring_need_id"], "product": str(c.get("product_query") or "Chevre").capitalize(),
                "quantity": float(c.get("quantity") or 0), "unit": c.get("unit") or "UNITE", "recurrence_type": c.get("recurrence_type"),
                "weekly_days": None, "status": "ACTIVE", "next_occurrence_date": None, "next_occurrence_id": None,
                "requested_quantity": None, "matched_quantity": None, "next_occurrence_version": None,
                "next_occurrence_notified": False, "in_latest_digest": False, "digest_occurrence_version": None,
            } for c in server.created]
            return {"status": "success", "items": items}

        h.runtime.responses["list_my_recurring_needs"] = _list
        h.runtime.responses["get_recurring_need_detail"] = lambda **kw: {
            "status": "success", "product": "chevre", "requested_quantity": 3, "unit": "UNITE", "allocations": [],
            "occurrence_id": None, "occurrence_version": None,
        }
        h.runtime.responses["get_buyer_orders_dashboard"] = {"status": "success", "formatted_menu": "📦 SUIVI DE VOS COMMANDES", "mapping": {}}
        return h

    def __exit__(self, *exc):
        return self.h.__exit__(*exc)


def _create(h) -> None:
    h.send("chevre 3 unite chaque semaine",
           llm=new_task("CREATE_RECURRING_NEED", product="chevre", quantity=3.0, unit="UNITE", recurrence_type="WEEKLY"))
    t = h.send("oui")
    assert "C'est noté" in t.response, t.response


def _listed(turn) -> bool:
    r = turn.response.lower()
    return "mes besoins récurrents" in r and "chevre" in r and "3 unite" in r and "chaque semaine" in r


# ── le cas prod, sans PostgreSQL ────────────────────────────────────────────────────────────────────────────────────
def test_admin_with_buyer_capability_lists_the_new_need_right_after_creation_with_a_blind_llm():
    with _Conv("ADMIN") as h:
        _create(h)
        assert h.state().get("user_role") == "PRODUCER", "cause réelle : le profil ADMIN passe par le graphe PRODUCER"
        t = h.send("mes besoins", llm=UNKNOWN)
        assert t.error is None and _listed(t), t.response
        assert t.intent == "GET_MY_NEEDS" and t.llm_calls == 0
        sel = h.send("1", llm=UNKNOWN)
        assert "get_recurring_need_detail" in sel.mcp_tools() and "chevre" in sel.response.lower()


@pytest.mark.parametrize(
    "alias", ["mes besoins", "mes besoins récurrents", "voir mes besoins", "mes approvisionnements récurrents", "Mes Besoins !"]
)
def test_aliases_are_deterministic_for_the_admin(alias):
    with _Conv("ADMIN") as h:
        _create(h)
        t = h.send(alias, llm=UNKNOWN)
        assert _listed(t) and t.llm_calls == 0, t.response


def test_a_stale_menu_is_escaped_for_the_admin_too():
    """B20 avec le rôle réel : un ancien menu (sélection) ne retient pas « mes besoins »."""
    with _Conv("ADMIN") as h:
        _create(h)
        h.seed({
            "current_goal": "BUYER_LIST_ORDERS",
            **set_pending_interaction(InteractionKind.SELECTION_MENU, goal="BUYER_LIST_ORDERS", candidates=("1", "2")),
            "available_mapping": {"1": "11111111-aaaa-0000-0000-000000000001", "2": "22222222-bbbb-0000-0000-000000000002"},
        })
        t = h.send("mes besoins", llm=UNKNOWN)
        assert _listed(t), t.response
        assert "get_buyer_orders_dashboard" not in t.mcp_tools()


def test_creation_leaves_no_living_tunnel_state_for_the_admin():
    with _Conv("ADMIN") as h:
        _create(h)
        st = h.state()
        assert not st.get("current_goal") and not st.get("locked_goal") and not st.get("pending_interaction")
        assert not st.get("recurring_need_draft") and not st.get("missing_fields") and not st.get("expected_candidates")
        assert not st.get("available_mapping") and not (st.get("working_memory") or {}).get("active_goal")


def test_the_capability_is_read_for_the_closed_vocabulary_only(monkeypatch):
    from ladini.graphs.agents.market_coach.interpreter import routing

    seen = []
    real = routing._buyer_capability

    async def spy(mc_runtime, state):
        seen.append(1)
        return await real(mc_runtime, state)

    monkeypatch.setattr(routing, "_buyer_capability", spy)
    with _Conv("ADMIN") as h:
        h.send("bonjour", llm=UNKNOWN)
        assert seen == [], "aucune lecture de capacité hors navigation « mes besoins »"
        h.send("mes besoins", llm=UNKNOWN)
        assert seen == [1]


# ── pas de passe-droit : sans capacité acheteur, le contrat existant s'applique ──────────────────────────────────────
def test_a_user_without_buyer_capability_is_not_given_the_list():
    with _Conv("PRODUCER", can_buy=False) as h:
        t = h.send("mes besoins", llm=UNKNOWN)
        assert "list_my_recurring_needs" not in t.mcp_tools() and t.llm_calls >= 1


def test_an_unreadable_capability_fails_closed(monkeypatch):
    with _Conv("ADMIN") as h:
        original = h.runtime.call_db
        state = {"broken": False}

        async def call_db(tool_name, **kw):
            if tool_name == "get_user_by_phone" and state["broken"]:
                raise RuntimeError("profil illisible")
            return await original(tool_name, **kw)

        h.runtime.call_db = call_db
        _create(h)
        state["broken"] = True
        t = h.send("mes besoins", llm=UNKNOWN)
        assert "list_my_recurring_needs" not in t.mcp_tools()


def test_mes_commandes_stays_a_buyer_graph_navigation_for_the_admin():
    """« mes commandes » est ambigu côté producteur : la capacité acheteur ne l'ouvre pas hors graphe BUYER."""
    with _Conv("ADMIN") as h:
        t = h.send("mes commandes", llm=UNKNOWN)
        assert "get_buyer_orders_dashboard" not in t.mcp_tools()


def test_buyer_graph_still_lists_without_any_capability_lookup():
    """(9) Régression : profil BUYER, graphe BUYER — la décision ne dépend d'aucune lecture de profil supplémentaire."""
    with _Conv("BUYER") as h:
        _create(h)
        t = h.send("mes besoins", llm=UNKNOWN)
        assert _listed(t) and t.llm_calls == 0
        assert t.mcp_tools().count("get_user_by_phone") == 0


# ── la primitive pure : matrice rôle × capacité × contexte ───────────────────────────────────────────────────────────
def _menu_state():
    return {"current_goal": "BUYER_LIST_ORDERS", **set_pending_interaction(InteractionKind.SELECTION_MENU, goal="BUYER_LIST_ORDERS", candidates=("1",)),
            "available_mapping": {"1": "o1"}}


def _slot_state():
    return {"current_goal": "UPDATE_PRICE", **set_pending_interaction(InteractionKind.ENTER_FIELD, goal="UPDATE_PRICE", field_name="price")}


@pytest.mark.parametrize("state_factory", [lambda: {}, _menu_state])
def test_pure_capability_opens_the_list_in_a_producer_graph(state_factory):
    d = ca.resolve_conversation_context(state_factory(), "mes besoins", role="PRODUCER", buyer_capable=True)
    assert d.kind == ca.ArbitrationKind.INTERRUPT_WITH_NEW_GOAL and d.raw["detected_intent"] == "GET_MY_NEEDS"


def test_pure_no_capability_no_navigation():
    d = ca.resolve_conversation_context({}, "mes besoins", role="PRODUCER", buyer_capable=False)
    assert d.kind == ca.ArbitrationKind.GENERIC_CLASSIFICATION and d.raw is None


def test_pure_a_producer_data_slot_is_never_interrupted_by_the_capability():
    d = ca.resolve_conversation_context(_slot_state(), "mes besoins", role="PRODUCER", buyer_capable=True)
    assert d.kind == ca.ArbitrationKind.ACTIVE_SLOT and d.raw is None


def test_pure_orders_navigation_needs_the_buyer_graph():
    assert ca.resolve_conversation_context({}, "mes commandes", role="PRODUCER", buyer_capable=True).raw is None
    assert ca.resolve_conversation_context({}, "mes commandes", role="BUYER").raw["detected_intent"] == "BUYER_LIST_ORDERS"


def test_pure_other_text_is_untouched_by_the_capability():
    for text in ("je veux vendre du maïs", "bonjour", "mes besoins de la semaine prochaine"):
        d = ca.resolve_conversation_context({}, text, role="PRODUCER", buyer_capable=True)
        assert d.raw is None and d.kind == ca.ArbitrationKind.GENERIC_CLASSIFICATION
