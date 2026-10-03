"""B9 (2026-10-02) — la confirmation de précommande Buyer est RÉELLE, déterministe et idempotente.

Incident prod : après « 🛒 Votre panier actuel … Répondez *précommander* pour valider », « okay »
répondait « ✅ C'est confirmé, vos commandes sont en cours de préparation. » alors qu'aucune
précommande n'était confirmée. Cause : sans tunnel verrouillé, le filet « mot nu » du DIGEST
d'approvisionnement récurrent (`_bare_confirmation_for_recurring_supply_digest`) capte le « okay » de
tout acheteur ayant un besoin récurrent -> `accept_match_proposal(ACCEPT)` sur des commandes SANS
RAPPORT avec le panier, et le texte de succès du digest s'affiche — le panier reste intact.

Invariant : message de succès ⇔ confirmation métier réussie. Le contexte du panier prêt gagne sur
les filets « mot nu » (producteur / digest / réception) et tous les alias passent par le MÊME chemin
canonique (`BUYER_PREORDER_INIT` -> récap -> confirmation -> exécution certifiée).

Replays sur le VRAI graphe compilé ; seuls le LLM et le MCP sont doublés.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

import pytest
from langgraph.checkpoint.memory import MemorySaver

from ladini.graphs.agents.market_coach.core.graph_builder import build_graph
from tests.integration.test_buyer_deterministic_product_switch_e2e import (
    _PHONE,
    HostileLLM,
    _Comp,
    _offer,
    _Rt,
    run,
)

_FAKE_SUCCESS = "C'est confirmé"
_ALIASES = ["okay", "ok", "oui", "confirmer", "valider", "précommander"]
_NOISE = ("get_account_status", "get_prohibited_terms")
_RECURRING = [{"recurring_need_id": "r1", "product": "oignon", "matched_quantity": 50, "status": "ACTIVE",
               "next_occurrence_id": "occ-r1", "next_occurrence_date": "2026-10-04", "next_occurrence_notified": True}]


class CartLLM(HostileLLM):
    """Construit le panier ; pour un accord libre pendant un panier prêt, répond comme le vrai prompt
    (CONFIRM). Compte ses appels pour prouver que le chemin déterministe n'en dépend pas."""

    def __init__(self) -> None:
        super().__init__({"disposition": "DEVIATION", "confidence": 0.9})
        self.n_calls = 0

    def create(self, **kw: Any):
        self.n_calls += 1
        msgs = kw.get("messages") or []
        sysm = " ".join(m["content"] for m in msgs if m["role"] == "system")
        user = " ".join(m["content"] for m in msgs if m["role"] == "user")
        found = re.findall(r'Message utilisateur :\s*"{3}(.*?)"{3}', user, re.S)
        text = (found[-1].strip() if found else "").lower()
        if "CATALOGUE OFFICIEL" in sysm and text in {"okay", "ok", "oui", "confirmer", "valider"}:
            return _Comp(json.dumps({"disposition": "CONFIRM", "confidence": 0.95}), kw.get("model"))
        return super().create(**kw)


class _Conv:
    def __init__(self, *, recurring=None, llm: bool = True, confirm: str = "ok") -> None:
        offers = [_offer("poulets", "UNITE", "P1", "A1"), _offer("lait", "LITRE", "P2", "B1")]
        self.llm = CartLLM()
        self.rt = _PreorderRt(self.llm if llm else None, offers, recurring or [], confirm)
        self.graph = build_graph("BUYER", mc_runtime=self.rt, checkpointer=MemorySaver())
        self.cfg = {"configurable": {"thread_id": "t"}}
        self.n = 0

    def say(self, text: str) -> Dict[str, Any]:
        self.n += 1
        self.rt.tool_log.clear()
        run(self.graph.ainvoke(
            {"user_query": text, "normalized_text": text, "user_phone": _PHONE, "user_role": "BUYER",
             "message_sid": f"m{self.n}"}, self.cfg))
        return self.graph.get_state(self.cfg).values

    def tools(self) -> List[str]:
        return [t for t, _ in self.rt.tool_log if t not in _NOISE]

    def build_cart(self) -> Dict[str, Any]:
        for t in ("je veux acheter des poulets", "2", "je veux acheter du lait", "3 L"):
            st = self.say(t)
        assert [(x["name"], x["quantity"]) for x in st["active_cart"]] == [("poulets", 2.0), ("lait", 3.0)]
        assert (st.get("preorder_workflow") or {}).get("phase") == "CART"
        assert "Répondez *précommander* pour valider" in st["final_response"]
        return st


class _PreorderRt(_Rt):
    def __init__(self, llm, offers, recurring, confirm):
        super().__init__(llm, offers)
        self.recurring, self.confirm = recurring, confirm
        self.all_calls: List[str] = []

    async def call_db(self, tool: str, **kw: Any) -> Any:
        self.all_calls.append(tool)
        if tool == "get_user_by_phone":
            self.tool_log.append((tool, dict(kw)))
            return {"status": "success", "data": {"id": "u1", "phone": kw.get("phone"), "role": "BUYER",
                                                  "latitude": 12.37, "longitude": -1.52}}
        if tool == "list_my_recurring_needs":
            self.tool_log.append((tool, dict(kw)))
            return {"status": "success", "items": self.recurring, "data": {"items": self.recurring}}
        if tool == "accept_match_proposal":
            self.tool_log.append((tool, dict(kw)))
            return {"status": "success", "message": "ok"}
        if tool == "create_preorder_draft":
            self.tool_log.append((tool, dict(kw)))
            items = kw.get("cart_items") or []
            total = sum(float(i.get("price") or 0) * float(i.get("quantity") or 0) for i in items)
            return {"status": "success", "preorder_id": "ORD-1", "total_amount": total, "currency": "XOF",
                    "unresolved_items": [],
                    "items": [{"product_id": i.get("product_id"), "name": i.get("name") or "x",
                               "quantity": i.get("quantity"), "unit": i.get("unit") or "UNITE",
                               "price": i.get("price"),
                               "line_total": float(i.get("price") or 0) * float(i.get("quantity") or 0),
                               "producer_id": i.get("producer_id"), "tier_id": None} for i in items]}
        if tool == "confirm_preorder_draft":
            self.tool_log.append((tool, dict(kw)))
            if self.confirm == "ok":
                return {"status": "success", "order_id": "ORD-1", "total_amount": 17500, "currency": "XOF",
                        "producer_phones_notified": ["+22670000001"]}
            if self.confirm == "error":
                return {"status": "error", "message": "stock indisponible"}
            raise TimeoutError("mcp timeout")
        return await super().call_db(tool, **kw)


@pytest.fixture(autouse=True)
def _pin_cod_checkout(monkeypatch):
    """Ces tests portent sur le chemin « paiement à la livraison » (`confirm_preorder_draft`). Le réglage
    ESCROW_PAYMENT_ENABLED dépend de l'environnement (actif en CI : confirmation par lien de paiement,
    autre chemin) — épinglé comme dans `test_buyer_direct_purchase_flow.py`."""
    from ladini.core.settings import settings

    monkeypatch.setattr(settings, "ESCROW_PAYMENT_ENABLED", False)


@pytest.fixture(autouse=True)
def _role_hints(monkeypatch):
    """Le worker pose un indice de rôle PRODUCER (Redis) pour chaque producteur notifié : enregistré ici
    (aucun réseau) — c'est aussi la seule trace côté agent de la notification producteur."""
    import ladini.graphs.agents.market_coach.flows.buyer.preorder_confirmation as pc

    calls: List[tuple] = []
    monkeypatch.setattr(pc, "_set_role_hint", lambda key, role, **kw: calls.append((key, role)))
    return calls


@pytest.fixture(params=["llm", "no_llm"])
def llm_mode(request):
    return request.param == "llm"


def _confirm_calls(c: _Conv) -> int:
    return c.rt.all_calls.count("confirm_preorder_draft")


class TestTheProdBug:
    @pytest.mark.parametrize("alias", _ALIASES)
    def test_a_ready_cart_is_never_stolen_by_the_recurring_digest(self, alias, llm_mode):
        c = _Conv(recurring=_RECURRING, llm=llm_mode)
        c.build_cart()
        st = c.say(alias)
        # le flux récurrent n'est NI interrogé NI confirmé — c'était la source du faux succès
        assert not {"accept_match_proposal", "list_my_recurring_needs", "get_producer_orders"} & set(c.tools()), c.tools()
        assert _FAKE_SUCCESS not in st["final_response"]
        assert st["current_goal"] == "BUYER_PREORDER_INIT"
        assert "Récapitulatif de votre précommande" in st["final_response"]
        assert c.tools() == ["create_preorder_draft"]
        assert [(x["name"], x["quantity"]) for x in st["active_cart"]] == [("poulets", 2.0), ("lait", 3.0)]

    def test_every_alias_follows_the_exact_same_canonical_path(self, llm_mode):
        outcomes = {}
        for alias in _ALIASES:
            c = _Conv(recurring=_RECURRING, llm=llm_mode)
            c.build_cart()
            st = c.say(alias)
            outcomes[alias] = (st["current_goal"], (st.get("pending_interaction") or {}).get("kind"),
                               (st.get("preorder_workflow") or {}).get("phase"), tuple(c.tools()),
                               st["final_response"])
        assert len({v for v in outcomes.values()}) == 1, outcomes

    def test_the_deterministic_path_does_not_depend_on_the_llm(self):
        c = _Conv(recurring=_RECURRING)
        c.build_cart()
        before = c.llm.n_calls
        c.say("okay")
        assert c.llm.n_calls == before, "l'alias de confirmation d'un panier prêt ne doit consommer aucun appel LLM"

    def test_observability_logs(self, caplog):
        caplog.set_level(logging.INFO)
        c = _Conv()
        c.build_cart()
        c.say("okay")
        c.say("okay")
        c.say("okay")
        text = caplog.text
        assert "BUYER_PREORDER_CONFIRMATION_REQUESTED cart_item_count=2 source=okay" in text
        assert "preorder_phase=CART" in text
        assert "BUYER_PREORDER_CONFIRMATION_RESULT success=True preorder_created=True" in text
        assert "order_count=1" in text and "idempotent_replay=False" in text


class TestRealConfirmationEndToEnd:
    def test_success_message_is_the_consequence_of_the_real_confirmation(self):
        c = _Conv()
        c.build_cart()
        st = c.say("okay")  # récap
        assert (st.get("pending_interaction") or {}).get("kind") == "CONFIRM_ACTION"
        assert _confirm_calls(c) == 0 and "confirmée" not in st["final_response"].lower()
        st = c.say("okay")  # accord -> livraison au point GPS habituel ?
        assert _confirm_calls(c) == 0 and "confirmée" not in st["final_response"].lower()
        st = c.say("oui")  # GPS validé -> exécution RÉELLE
        assert _confirm_calls(c) == 1
        assert "Précommande confirmée" in st["final_response"] and "ORD-1" in st["final_response"]
        # état après succès : panier consommé, pending consommé, confirmation non rejouable
        assert st["active_cart"] == [] and not (st.get("pending_interaction") or {}).get("kind")
        assert (st.get("preorder_workflow") or {}).get("phase") == "CONFIRMED"
        assert st["status"] == "COMPLETED"

    def test_confirm_service_failure_never_shows_success_and_keeps_the_cart_for_retry(self):
        c = _Conv(confirm="error")
        c.build_cart()
        c.say("okay")
        c.say("okay")
        st = c.say("oui")
        assert _confirm_calls(c) == 1
        text = st["final_response"]
        assert "Précommande confirmée" not in text and _FAKE_SUCCESS not in text
        assert "Impossible de confirmer" in text and "aucun stock n'a été débité" in text
        assert [(x["name"], x["quantity"]) for x in st["active_cart"]] == [("poulets", 2.0), ("lait", 3.0)]
        assert (st.get("preorder_workflow") or {}).get("phase") == "CART"
        # …état cohérent pour un retry : le même « okay » relance proprement la confirmation
        st = c.say("okay")
        assert "Récapitulatif de votre précommande" in st["final_response"]

    def test_mcp_exception_is_reported_honestly_never_as_a_success(self):
        c = _Conv(confirm="raise")
        c.build_cart()
        c.say("okay")
        c.say("okay")
        st = c.say("oui")
        text = st["final_response"]
        assert "Précommande confirmée" not in text and _FAKE_SUCCESS not in text
        assert "en cours de vérification" in text  # issue inconnue annoncée comme telle (réconciliation)
        assert _confirm_calls(c) == 1

    def test_after_a_failed_confirmation_a_retry_can_succeed_exactly_once(self):
        c = _Conv(confirm="error")
        c.build_cart()
        for t in ("okay", "okay", "oui"):
            c.say(t)
        c.rt.confirm = "ok"
        c.say("okay")
        c.say("okay")
        st = c.say("oui")
        assert "Précommande confirmée" in st["final_response"] and st["active_cart"] == []


class TestIdempotence:
    def test_repeated_okay_never_duplicates_the_preorder_or_the_order(self):
        c = _Conv()
        c.build_cart()
        for t in ("okay", "okay", "okay", "okay", "okay"):
            c.say(t)
        assert c.rt.all_calls.count("create_preorder_draft") == 1
        assert _confirm_calls(c) == 1

    def test_precommander_then_okay_is_a_single_confirmation(self):
        c = _Conv()
        c.build_cart()
        for t in ("précommander", "okay", "okay"):
            c.say(t)
        assert c.rt.all_calls.count("create_preorder_draft") == 1 and _confirm_calls(c) == 1

    def test_after_the_confirmation_the_same_cart_cannot_be_confirmed_again(self):
        c = _Conv()
        c.build_cart()
        for t in ("okay", "okay", "oui"):
            st = c.say(t)
        assert st["active_cart"] == []
        for t in ("okay", "précommander", "valider"):
            st = c.say(t)
            assert "Précommande confirmée" not in st["final_response"] and _FAKE_SUCCESS not in st["final_response"]
        assert _confirm_calls(c) == 1 and c.rt.all_calls.count("create_preorder_draft") == 1


class TestNoCart:
    @pytest.mark.parametrize("alias", ["okay", "ok", "oui", "confirmer", "valider", "précommander"])
    def test_nothing_is_confirmed_without_a_cart(self, alias, llm_mode):
        c = _Conv(llm=llm_mode)
        st = c.say(alias)
        assert not {"create_preorder_draft", "confirm_preorder_draft", "accept_match_proposal"} & set(c.rt.all_calls)
        assert _FAKE_SUCCESS not in st["final_response"] and "préparation" not in st["final_response"]
        assert "Précommande confirmée" not in st["final_response"]

    def test_the_recurring_digest_reply_still_works_when_there_is_no_cart(self):
        # non-régression : sans panier prêt, « okay » reste la réponse au digest récurrent.
        c = _Conv(recurring=_RECURRING)
        st = c.say("okay")
        assert "accept_match_proposal" in c.rt.all_calls
        assert "C'est confirmé, vos commandes sont en cours de préparation" in st["final_response"]


class TestUnit:
    @staticmethod
    def _state(phase="CART", cart=True):
        from tests.conftest import make_state

        return make_state(
            preorder_workflow={"phase": phase} if phase else {},
            active_cart=[{"name": "lait", "quantity": 1}] if cart else [],
        )

    @pytest.mark.parametrize("text", ["okay", "OK", "Oui !", "confirmer", "valider", "précommander",
                                      "precommander", "je précommande", "confimer"])
    def test_aliases_when_the_cart_is_ready(self, text):
        from ladini.graphs.agents.market_coach.interpreter.routing import (
            _cart_ready_confirmation_alias,
        )

        assert _cart_ready_confirmation_alias(self._state(), text) is not None, text

    @pytest.mark.parametrize("text", ["je veux acheter du lait", "non", "annuler", "okay je veux aussi des tomates", "2", ""])
    def test_free_text_and_refusals_are_not_aliases(self, text):
        from ladini.graphs.agents.market_coach.interpreter.routing import (
            _cart_ready_confirmation_alias,
        )

        assert _cart_ready_confirmation_alias(self._state(), text) is None, text

    @pytest.mark.parametrize("phase,cart", [("CART", False), ("PREORDER_DRAFTED", True), ("CONFIRMED", True), (None, True)])
    def test_only_a_ready_cart_counts(self, phase, cart):
        from ladini.graphs.agents.market_coach.interpreter.routing import (
            _cart_ready_confirmation_alias,
        )

        assert _cart_ready_confirmation_alias(self._state(phase, cart), "okay") is None

    def test_recurring_digest_reports_a_partial_success_honestly(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.buyer.recurring_need as rn
        from ladini.graphs.agents.market_coach.services.mcp.gateway import MCPCallError

        class _GW:
            def __init__(self, *_a, **_k):
                pass

            async def list_my_recurring_needs(self, phone):
                return {"items": [{"recurring_need_id": "a", "product": "oignon", "matched_quantity": 5, "status": "ACTIVE", "next_occurrence_id": "occ-a", "next_occurrence_date": "2026-10-04", "next_occurrence_notified": True},
                                  {"recurring_need_id": "b", "product": "tomate", "matched_quantity": 5, "status": "ACTIVE", "next_occurrence_id": "occ-b", "next_occurrence_date": "2026-10-04", "next_occurrence_notified": True}]}

            async def accept_match_proposal(self, phone, recurring_need_id, action, occurrence_id=None):
                if recurring_need_id == "b":
                    raise MCPCallError("accept_match_proposal", "déjà traité", "ALREADY_DONE", "r-1")
                return {"status": "success"}

        monkeypatch.setattr(rn, "RecurringSupplyGateway", _GW)
        out = run(rn._respond_to_digest_flow({"user_phone": _PHONE}, object(), action="CONFIRM"))
        text = out["final_response"]
        assert "oignon" in text and "tomate" in text
        assert "vos commandes sont en cours de préparation" not in text
        assert out["result"] == {"confirmed": ["oignon"], "skipped": ["tomate"]}
