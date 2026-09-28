"""Buyer direct-purchase tunnel — offer identity, photo transparency, shortage → TAKE_AVAILABLE.

Reproduit l'incident réel WhatsApp (2026-09-28) sur le VRAI graphe compilé
(`tests/harness/conversation.py` : orchestrateur, graphe, checkpointer et persistance JSON
réels ; seuls LLM / MCP / Redis / envoi sont doublés) : trois OFFRES distinctes dont deux
appartiennent au MÊME producteur.

    1. TEST Producteur — 425000 FCFA/UNITE — Dispo: 1          (A, producteur P1)
    2. Gilbert-prod    — 450000 FCFA/UNITE — Dispo: 20         (B, producteur P2)
    3. Gilbert-prod    — 461000 FCFA/UNITE — Dispo: 461000     (C, producteur P2)

Quatre défauts distincts, diagnostiqués séparément puis testés ensemble :

  A. identité d'offre   — « 3 » retombait sur la 1ʳᵉ offre du producteur P2 (B, pas C) ;
  B. `photo N`          — innocent : commande hors graphe, lecture seule (prouvé ici) ;
  C. quantité           — « 20 » ne remplaçait pas 461000 (récap périmé) ;
  D. branche            — un « oui » après « 20 » lançait un appel d'offres.

Portée honnête : `create_preorder_draft` / `confirm_preorder_draft` sont des DOUBLES écrits
par ce test (comme `test_cart_to_checkout_full_journey.py`) — ils reproduisent ce que le
serveur MCP renvoie ; la logique serveur (débit de stock, ordre) est verrouillée ailleurs.
Ce que ce fichier prouve : le `product_id`, le producteur, la quantité et le prix envoyés au
serveur sont EXACTEMENT ceux de l'offre choisie et de la quantité décidée, et qu'aucun
appel d'offres n'est émis.
"""
from __future__ import annotations

import contextlib
import copy
import json
from typing import Any, Dict, Iterator, List, Optional

import pytest

from ladini.core.settings import settings
from ladini.graphs.agents.market_coach.domain.preorder_draft import PreorderDraftStatus
from ladini.graphs.agents.market_coach.flows.buyer.preorder import create_preorder
from ladini.services import search_results_cache
from ladini.services.database import preorder_draft_store
from tests.architecture.test_preorder_draft_persistence import (
    _install_fake_db as _install_fake_preorder_db,
)
from tests.conftest import run
from tests.evals.runners.harness import RecordingRuntime
from tests.harness import ConversationHarness, new_task
from tests.harness.conversation import RecordingDispatcher

pytestmark = pytest.mark.integration

P1 = "aaaaaaaa-0000-0000-0000-000000000001"
P2 = "bbbbbbbb-0000-0000-0000-000000000002"
PROD_A = "a0000000-0000-0000-0000-00000000000a"
PROD_B = "b0000000-0000-0000-0000-00000000000b"
PROD_C = "c0000000-0000-0000-0000-00000000000c"

AUCTION_TOOLS = {"create_auction", "create_procurement_request", "update_auction"}
_INFRA = {
    "get_user_by_phone", "get_account_status", "get_prohibited_terms",
    "get_product_category_unit_config",
}
PROFILE = {
    "id": "11111111-1111-1111-1111-111111111111", "name": "Awa", "role": "BUYER",
    "zone": {"id": "zone-1", "name": "Ouagadougou"},
}


def _offers(stock_b: float = 20, stock_c: float = 461000) -> List[Dict[str, Any]]:
    return [
        {
            "id": PROD_A, "name": "boeufs", "price": 425000.0, "unit": "UNITE",
            "producer_id": P1, "vendor_name": "TEST Producteur",
            "available_quantity": 1, "images": ["https://img/a1.jpg"],
        },
        {
            "id": PROD_B, "name": "boeufs", "price": 450000.0, "unit": "UNITE",
            "producer_id": P2, "vendor_name": "Gilbert-prod",
            "available_quantity": stock_b, "images": ["https://img/b1.jpg"],
        },
        {
            "id": PROD_C, "name": "boeufs", "price": 461000.0, "unit": "UNITE",
            "producer_id": P2, "vendor_name": "Gilbert-prod",
            "available_quantity": stock_c,
            "images": ["https://img/c1.jpg", "https://img/c2.jpg"],
        },
    ]


class _FakeCache:
    """Sous-ensemble Redis de `services/search_results_cache.py` (setex/get)."""

    def __init__(self) -> None:
        self.data: Dict[str, str] = {}

    def setex(self, key, _ttl, value):
        self.data[key] = value

    def get(self, key):
        return self.data.get(key)


class Shop:
    """Catalogue + stock vivants : `stock` peut être modifié entre deux tours."""

    def __init__(self, offers: List[Dict[str, Any]]) -> None:
        self.offers = offers
        self.by_id = {o["id"]: o for o in offers}
        self.stock = {o["id"]: float(o["available_quantity"]) for o in offers}

    def stock_check(self, **kw: Any) -> Dict[str, Any]:
        pid = str(kw.get("product_id"))
        wanted = float(kw.get("quantity") or 0)
        offer = self.by_id.get(pid)
        if offer is None:
            return {"status": "error", "reason": "product_not_found", "message": "introuvable"}
        if wanted > self.stock[pid]:
            return {
                "status": "error", "reason": "insufficient_stock",
                "available_quantity": self.stock[pid], "unit": offer["unit"],
                "message": "Stock insuffisant",
            }
        return {
            "status": "success", "available_quantity": self.stock[pid], "unit": offer["unit"],
            "unit_price": offer["price"], "producer_id": offer["producer_id"],
        }


@contextlib.contextmanager
def buyer_conversation(
    shop: Optional[Shop] = None, *, gps: bool = False
) -> Iterator[ConversationHarness]:
    shop = shop or Shop(_offers())
    profile = dict(PROFILE)
    if gps:
        profile.update(latitude=12.37, longitude=-1.52)
    with ConversationHarness(role="BUYER", profile=profile) as c:
        c.shop = shop  # type: ignore[attr-defined]
        c.runtime.responses["search_products"] = lambda **_: {
            "status": "success", "data": {"results": [copy.deepcopy(o) for o in shop.offers]},
        }
        c.runtime.responses["validate_stock_availability_atomic"] = shop.stock_check
        cache = _FakeCache()
        previous = search_results_cache._redis_client
        search_results_cache._redis_client = cache  # type: ignore[assignment]
        c.photo_cache = cache  # type: ignore[attr-defined]
        try:
            yield c
        finally:
            search_results_cache._redis_client = previous


@pytest.fixture
def conv():
    with buyer_conversation() as c:
        yield c


# ── helpers ────────────────────────────────────────────────────────────────


def _search(conv: ConversationHarness):
    return conv.send("boeufs", llm=new_task("BUYER_REQUEST", product="boeufs"))


def _ctx(state: Dict[str, Any]) -> Dict[str, Any]:
    return dict(state.get("vendor_selection_context") or {})


def _chosen(state: Dict[str, Any]) -> Dict[str, Any]:
    return dict(_ctx(state).get("chosen_vendor") or {})


def _cart(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(state.get("active_cart") or [])


def _business_calls(conv: ConversationHarness) -> List[str]:
    return [tool for tool, _ in conv.runtime.calls if tool not in _INFRA]


def _shortage(state: Dict[str, Any]) -> Dict[str, Any]:
    return dict((state.get("working_memory") or {}).get("stock_shortage") or {})


def _set_quantity(quantity: float) -> Dict[str, Any]:
    """Sortie du micro-prompt STRUCTURED_ACTION quand le tunnel attend SET_QUANTITY."""
    return {"disposition": "ACTION", "action": "SET_QUANTITY", "quantity": quantity, "confidence": 0.95}


def _to_shortage(conv: ConversationHarness, index: str = "2", requested: float = 461000.0):
    _search(conv)
    conv.send(index)
    return conv.send(f"Je veux {int(requested)}", llm=_set_quantity(requested))


def _photo(conv: ConversationHarness, index: str) -> RecordingDispatcher:
    """Exécute la commande `photos <index>` EXACTEMENT comme la tâche Celery le fait
    (`workers/media/product_photo_task.py::_send_search_result_photos`) — hors graphe."""
    from unittest import mock

    from ladini.api import response_dispatch
    from ladini.workers.media.product_photo_task import _send_search_result_photos

    dispatcher = RecordingDispatcher()
    with mock.patch.object(response_dispatch, "get_dispatcher", lambda: dispatcher):
        conv._run(_send_search_result_photos(conv.phone, index, message_sid=f"wamid.photo{index}"))
    return dispatcher


def _photo_urls(dispatcher: RecordingDispatcher) -> List[str]:
    urls: List[str] = []
    for _phone, plan in dispatcher.sent:
        for item in plan.items:
            url = getattr(item, "url", None)
            if url:
                urls.append(url)
    return urls


def _photo_reminder(dispatcher: RecordingDispatcher) -> str:
    texts = [getattr(i, "text", "") for _p, plan in dispatcher.sent for i in plan.items]
    return " ".join(t for t in texts if t)


def _stable(state: Dict[str, Any]) -> str:
    """Empreinte des champs de sélection PRINCIPALE — jamais modifiés par `photo N`."""
    keys = (
        "vendor_selection_context", "pending_interaction", "current_goal", "transaction_payload",
        "active_cart", "tier_selection_context", "working_memory",
    )
    return json.dumps({k: state.get(k) for k in keys}, sort_keys=True, default=str)


# =====================================================================
# A. Identité d'offre
# =====================================================================


class TestOfferIdentity:
    def test_menu_lists_three_distinct_offers_in_order_with_stable_ids(self, conv):
        t = _search(conv)
        ctx = _ctx(t.after)
        vendors = ctx["vendors"]
        assert [v["product_id"] for v in vendors] == [PROD_A, PROD_B, PROD_C]
        assert [v["offer_id"] for v in vendors] == [PROD_A, PROD_B, PROD_C]
        assert [v["display_index"] for v in vendors] == [1, 2, 3]
        assert ctx.get("menu_id"), "un menu_id figé doit accompagner le snapshot"

    def test_selecting_3_resolves_the_exact_offer_C(self, conv):
        _search(conv)
        t = conv.send("3")
        chosen = _chosen(t.after)
        assert chosen.get("product_id") == PROD_C, (
            f"'3' doit désigner l'offre C, obtenu {chosen.get('product_id')!r} "
            f"(prix {chosen.get('price')}) — réponse : {t.response!r}"
        )
        assert chosen.get("price") == 461000.0
        assert chosen.get("available_qty") == 461000
        assert "461000" in t.response and "450000" not in t.response

    def test_same_producer_two_offers_index_2_and_3_stay_distinct(self, conv):
        """Régression critique : B et C ont le MÊME producer_id."""
        _search(conv)
        t2 = conv.send("2")
        assert _chosen(t2.after)["product_id"] == PROD_B
        assert _chosen(t2.after)["price"] == 450000.0

    def test_selecting_3_needs_no_llm_and_uses_the_deterministic_path(self, conv):
        _search(conv)
        t = conv.send("3")  # aucun script LLM : un appel ferait échouer le tour
        assert t.llm_calls == 0
        assert t.error is None

    def test_legacy_producer_id_only_action_is_rejected_when_ambiguous(self):
        """Contrat historique (producer_id seul) : jamais deviné quand le producteur a
        plusieurs offres dans le menu."""
        from ladini.graphs.agents.market_coach.domain.selection_actions import (
            ActionType,
            build_selection_context,
            validate_action,
        )

        state = {
            "vendor_selection_context": {
                "vendors": [
                    {"product_id": PROD_A, "producer_id": P1, "vendor_name": "a", "price": 1, "unit": "U"},
                    {"product_id": PROD_B, "producer_id": P2, "vendor_name": "g", "price": 2, "unit": "U"},
                    {"product_id": PROD_C, "producer_id": P2, "vendor_name": "g", "price": 3, "unit": "U"},
                ]
            }
        }
        ctx = build_selection_context(state)
        ambiguous = validate_action({"action": ActionType.SELECT_PRODUCER, "producer_id": P2}, ctx)
        assert ambiguous is None
        unique = validate_action({"action": ActionType.SELECT_PRODUCER, "producer_id": P1}, ctx)
        assert unique is not None and unique.offer_id == PROD_A
        exact = validate_action({"action": ActionType.SELECT_PRODUCER, "offer_id": PROD_C}, ctx)
        assert exact is not None and exact.offer_id == PROD_C and exact.producer_id == P2

    def test_lines_without_producer_id_do_not_shift_the_displayed_indexes(self):
        from ladini.graphs.agents.market_coach.domain.selection_actions import (
            ActionType,
            build_selection_context,
            fast_path_action,
        )

        vendors = [
            {"product_id": "x1", "producer_id": "", "vendor_name": "n1", "price": 1, "unit": "U"},
            {"product_id": "x2", "producer_id": P2, "vendor_name": "n2", "price": 2, "unit": "U"},
            {"product_id": "x3", "producer_id": P2, "vendor_name": "n3", "price": 3, "unit": "U"},
        ]
        ctx = build_selection_context({"vendor_selection_context": {"vendors": vendors}})
        assert [o.display_index for o in ctx.producer_options] == [1, 2, 3]
        raw = fast_path_action("3", ctx)
        assert raw and raw["action"] == ActionType.SELECT_PRODUCER and raw["offer_id"] == "x3"


# =====================================================================
# B. photo N — action secondaire en lecture seule
# =====================================================================


class TestPhotoPreviewIsStateTransparent:
    def test_photo_3_shows_offer_C_photos_and_changes_no_graph_state(self, conv):
        t = _search(conv)
        before = _stable(conv.state())
        cache_before = dict(conv.photo_cache.data)

        dispatcher = _photo(conv, "3")

        assert _photo_urls(dispatcher) == ["https://img/c1.jpg", "https://img/c2.jpg"]
        assert "répondre *3*" in _photo_reminder(dispatcher)
        assert _stable(conv.state()) == before, "photo N a modifié l'état de sélection principal"
        assert conv.photo_cache.data == cache_before, "photo N a réécrit le snapshot"
        assert t.after["pending_interaction"]["kind"] == "SELECTION_MENU"

    def test_photo_3_then_3_selects_offer_C(self, conv):
        _search(conv)
        _photo(conv, "3")
        t = conv.send("3")
        assert _chosen(t.after)["product_id"] == PROD_C
        assert _chosen(t.after)["price"] == 461000.0

    def test_photo_3_twice_then_3_selects_offer_C(self, conv):
        _search(conv)
        _photo(conv, "3")
        _photo(conv, "3")
        t = conv.send("3")
        assert _chosen(t.after)["product_id"] == PROD_C

    def test_photo_2_then_3_selects_3_not_2(self, conv):
        _search(conv)
        _photo(conv, "2")
        t = conv.send("3")
        assert _chosen(t.after)["product_id"] == PROD_C

    def test_photo_cache_carries_the_same_offer_ids_and_menu_id_as_the_snapshot(self, conv):
        t = _search(conv)
        cached = json.loads(conv.photo_cache.data[search_results_cache.key_for(conv.phone)])
        ctx = _ctx(t.after)
        assert {k: v["offer_id"] for k, v in cached.items()} == {"1": PROD_A, "2": PROD_B, "3": PROD_C}
        assert {v["menu_id"] for v in cached.values()} == {ctx["menu_id"]}

    def test_photo_of_unknown_index_is_read_only_too(self, conv):
        _search(conv)
        before = _stable(conv.state())
        # index hors menu : message d'erreur, aucune mutation d'état
        from unittest import mock

        from ladini.api import tasks as api_tasks
        from ladini.workers.media.product_photo_task import _send_search_result_photos

        sent: List[str] = []

        async def _fake_text(phone, text, message_sid=None):
            sent.append(text)

        with mock.patch.object(api_tasks, "send_confirmation_text", _fake_text):
            conv._run(_send_search_result_photos(conv.phone, "9", message_sid="wamid.x"))
        assert sent and "invalide" in sent[0].lower()
        assert _stable(conv.state()) == before


# =====================================================================
# Pagination (affichage) vs index (identité)
# =====================================================================


class TestPaginationNeverChangesIndexes:
    def _many(self, n: int) -> List[Dict[str, Any]]:
        return [
            {
                "id": f"prod-{i:03d}", "name": "boeufs", "price": 1000.0 + i, "unit": "UNITE",
                "producer_id": P2 if i % 2 else P1, "vendor_name": f"Vendeur {i}",
                "available_quantity": 10 + i, "images": [],
            }
            for i in range(1, n + 1)
        ]

    def test_three_offers_render_as_one_page_with_no_footer_only_page(self, conv):
        t = _search(conv)
        assert "Page 2/2" not in t.response
        assert t.response.count("Producteurs disponibles") == 1
        assert all(f"*{i}.*" in t.response for i in (1, 2, 3))

    def test_many_offers_paginate_but_each_index_appears_once_and_in_order(self):
        shop = Shop(self._many(8))
        with buyer_conversation(shop) as conv:
            t = _search(conv)
            for i in range(1, 9):
                assert t.response.count(f"*{i}.*") == 1, f"index {i} dupliqué ou absent"
            positions = [t.response.index(f"*{i}.*") for i in range(1, 9)]
            assert positions == sorted(positions)
            # aucune page ne se limite à des pieds de page : chaque page a au moins une offre
            pages = [p for p in t.response.split("") if p.strip()]
            assert len(pages) == 2
            import re

            assert all(re.search(r"\*\d+\.\*", p) for p in pages), "page sans aucune option"
            # l'option 8 désigne bien la 8ᵉ offre, même après pagination
            sel = conv.send("8")
            assert _chosen(sel.after)["product_id"] == "prod-008"

    def test_options_count_equals_candidates_count(self):
        shop = Shop(self._many(7))
        with buyer_conversation(shop) as conv:
            t = _search(conv)
            assert len(_ctx(t.after)["vendors"]) == 7
            cached = json.loads(conv.photo_cache.data.get(search_results_cache.key_for(conv.phone), "{}"))
            assert cached == {}, "aucune image => aucun cache photo, mais la liste reste complète"


# =====================================================================
# C/D. Rupture de stock → TAKE_AVAILABLE / START_TENDER / CANCEL
# =====================================================================


class TestStockShortageDecision:
    def test_request_above_stock_produces_an_explicit_shortage_decision(self, conv):
        t = _to_shortage(conv, index="2")
        sh = _shortage(t.after)
        assert sh["status"] == "ACTIVE"
        assert sh["product_id"] == PROD_B and sh["producer_id"] == P2
        assert sh["available_quantity"] == 20.0
        assert sh["original_requested_quantity"] == 461000.0
        assert sh["unit_price"] == 450000.0
        assert sh["offer"]["offer_id"] == PROD_B
        pending = t.after["pending_interaction"]
        assert pending["kind"] == "CONFIRM_ACTION"
        assert pending["target"]["kind"] == "STOCK_SHORTAGE"
        assert "20" in t.response and "461000" in t.response

    def test_reply_20_takes_the_available_stock_deterministically(self, conv):
        _to_shortage(conv)
        t = conv.send("20")  # aucun script LLM
        assert t.llm_calls == 0 and t.error is None
        cart = _cart(t.after)
        assert len(cart) == 1
        assert cart[0]["product_id"] == PROD_B
        assert cart[0]["producer_id"] == P2
        assert cart[0]["quantity"] == 20.0
        assert cart[0]["price"] == 450000.0
        assert "461" not in json.dumps(cart)
        assert "20" in t.response and "9000000" in t.response.replace(" ", "")

    def test_original_requested_quantity_is_history_only(self, conv):
        _to_shortage(conv)
        t = conv.send("20")
        wm = t.after.get("working_memory") or {}
        assert wm.get("original_requested_quantity") == 461000.0
        assert _cart(t.after)[0]["quantity"] == 20.0
        assert (t.after.get("transaction_payload") or {}).get("quantity") in (None, 20.0)
        assert not _shortage(t.after), "la décision est consommée"
        assert t.after.get("pending_interaction") in (None, {})

    @pytest.mark.parametrize("available,reply", [(7, "7"), (2.5, "2.5"), (12, "12"), (40, "40")])
    def test_take_available_is_generic_not_hardcoded_to_20(self, available, reply):
        shop = Shop(_offers(stock_b=available))
        with buyer_conversation(shop) as conv:
            _to_shortage(conv, index="2", requested=999999)
            assert _shortage(conv.state())["available_quantity"] == float(available)
            t = conv.send(reply)
            assert t.llm_calls == 0
            assert _cart(t.after)[0]["quantity"] == float(reply)
            assert _cart(t.after)[0]["product_id"] == PROD_B

    def test_a_smaller_number_is_a_direct_purchase_quantity_too(self, conv):
        _to_shortage(conv)
        t = conv.send("15")
        assert _cart(t.after)[0]["quantity"] == 15.0

    def test_a_bigger_number_is_revalidated_and_creates_a_new_shortage(self, conv):
        _to_shortage(conv)
        t = conv.send("500")
        assert _cart(t.after) == []
        assert _shortage(t.after)["original_requested_quantity"] == 500.0

    def test_take_available_never_starts_a_tender(self, conv):
        _to_shortage(conv)
        conv.send("20")
        assert not (set(_business_calls(conv)) & AUCTION_TOOLS)
        assert conv.state().get("current_goal") != "PROCUREMENT_CREATE_REQUEST"
        assert conv.state().get("active_form") in (None, "")

    def test_oui_starts_the_tender_with_the_original_quantity_and_no_cart(self, conv):
        _to_shortage(conv)
        t = conv.send("oui")
        assert t.goal_after == "PROCUREMENT_CREATE_REQUEST"
        assert _cart(t.after) == []
        assert (t.after.get("form_data") or {}).get("quantity") == 461000.0
        assert not _shortage(t.after)
        assert "appel d'offres" in t.response.lower()

    def test_non_cancels_both_branches(self, conv):
        _to_shortage(conv)
        t = conv.send("non")
        assert _cart(t.after) == []
        assert t.goal_after != "PROCUREMENT_CREATE_REQUEST"
        assert not _shortage(t.after)
        assert not (set(_business_calls(conv)) & AUCTION_TOOLS)

    def test_a_stale_shortage_cannot_capture_a_number_in_an_unrelated_confirmation(self, conv):
        """La décision ne répond qu'à SA question : un dict résiduel dans
        working_memory + un CONFIRM_ACTION sans rapport ne suffisent pas."""
        from ladini.graphs.agents.market_coach.core.pending_interaction import (
            InteractionKind,
            set_pending_interaction,
        )
        from ladini.graphs.agents.market_coach.domain.stock_shortage import (
            build_shortage_state,
            shortage_awaiting_reply,
        )

        offer = {"product_id": PROD_B, "producer_id": P2, "name": "boeufs", "price": 450000.0, "unit": "UNITE"}
        state = {
            "working_memory": {
                "stock_shortage": build_shortage_state(
                    offer=offer, requested_quantity=461000, available_quantity=20, unit="UNITE"
                )
            },
            **set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation"),
        }
        assert shortage_awaiting_reply(state) is None


# =====================================================================
# Commande d'achat direct certifiée (PreorderDraft figé) → exécution
# =====================================================================


def _checkout_state(conv: ConversationHarness) -> Dict[str, Any]:
    state = dict(conv.state())
    state["current_goal"] = "BUYER_PREORDER_CONFIRM"
    state["transaction_payload"] = {"resolved_id": "PREORDER_CONFIRM"}
    state["user_phone"] = conv.phone
    return state


def _server_draft_response(cart: List[Dict[str, Any]], order_id: str = "ORD-20") -> Dict[str, Any]:
    line = cart[0]
    return {
        "status": "success",
        "preorder_id": order_id,
        "total_amount": line["line_total"],
        "currency": "XOF",
        "items": [
            {
                "product_id": line["product_id"], "name": line["name"], "quantity": line["quantity"],
                "unit": line["unit"], "price": line["price"], "line_total": line["line_total"],
                "producer_id": line["producer_id"], "tier_id": None,
            }
        ],
        "unresolved_items": [],
    }


class TestDirectPurchaseCertifiedCommand:
    @pytest.fixture(autouse=True)
    def _fake_db(self, monkeypatch):
        _install_fake_preorder_db(monkeypatch)
        monkeypatch.setattr(settings, "ESCROW_PAYMENT_ENABLED", False)

    def _to_cart_of_20(self, conv):
        _to_shortage(conv)
        return conv.send("20")

    def _draft_step(self, conv):
        state = _checkout_state(conv)
        cart = _cart(state)
        runtime = RecordingRuntime(responses={"create_preorder_draft": _server_draft_response(cart)})
        patch = run(create_preorder(state, runtime))
        return state, patch, runtime

    def test_recap_and_command_carry_20_of_the_exact_offer(self, conv):
        self._to_cart_of_20(conv)
        _state, patch, runtime = self._draft_step(conv)
        sent = dict(runtime.calls[0][1])["cart_items"]
        assert len(sent) == 1
        assert sent[0]["product_id"] == PROD_B
        assert sent[0]["producer_id"] == P2
        assert sent[0]["quantity"] == 20.0
        assert sent[0]["price"] == 450000.0
        draft = patch["preorder_draft"]
        assert draft["status"] == PreorderDraftStatus.DRAFT.value
        assert draft["items"][0]["quantity"] == 20.0
        assert "461" not in json.dumps(draft["items"])

    def test_confirm_executes_the_certified_command_with_zero_auction(self, conv):
        self._to_cart_of_20(conv)
        state, patch, _rt = self._draft_step(conv)
        draft_v1 = patch["preorder_draft"]
        gps = dict(state)
        gps.update(patch)
        gps.update(location_shared=True, location_outcome="NEW_LOCATION_ACCEPTED",
                   location_lat=12.3714, location_lon=-1.5197)
        confirm = RecordingRuntime(
            responses={"confirm_preorder_draft": {
                "status": "success", "order_id": draft_v1["order_id"],
                "total_amount": draft_v1["total_amount"], "currency": "XOF"}}
        )
        final = run(create_preorder(gps, confirm))
        assert confirm.call_names.count("confirm_preorder_draft") == 1
        assert not (set(confirm.call_names) & AUCTION_TOOLS)
        assert not (set(_business_calls(conv)) & AUCTION_TOOLS)
        assert final["preorder_draft"]["status"] == PreorderDraftStatus.EXECUTED.value
        persisted = run(preorder_draft_store.load(draft_v1["draft_id"]))
        assert persisted.status == PreorderDraftStatus.EXECUTED
        item = persisted.items[0]
        assert (item.product_id if hasattr(item, "product_id") else item["product_id"]) == PROD_B
        kwargs = dict(confirm.calls[0][1])
        assert kwargs["preorder_id"] == draft_v1["order_id"]
        assert kwargs["idempotency_key"]

    def test_raw_state_mutation_after_the_recap_cannot_alter_the_executed_command(self, conv):
        self._to_cart_of_20(conv)
        state, patch, _rt = self._draft_step(conv)
        draft_v1 = patch["preorder_draft"]
        mutated = dict(state)
        mutated.update(patch)
        mutated["active_cart"] = [dict(_cart(state)[0], quantity=461000.0, product_id=PROD_C)]
        mutated["transaction_payload"] = {"quantity": 461000.0, "product": "boeufs"}
        mutated.update(location_shared=True, location_outcome="NEW_LOCATION_ACCEPTED",
                       location_lat=12.3714, location_lon=-1.5197)
        confirm = RecordingRuntime(
            responses={"confirm_preorder_draft": {"status": "success", "order_id": draft_v1["order_id"]}}
        )
        final = run(create_preorder(mutated, confirm))
        exec_items = final["preorder_draft"]["items"]
        assert exec_items[0]["quantity"] == 20.0 and exec_items[0]["product_id"] == PROD_B
        assert "confirm_preorder_draft" in confirm.call_names
        assert dict(confirm.calls[0][1])["preorder_id"] == draft_v1["order_id"]

    def test_double_confirm_yields_a_single_order(self, conv):
        self._to_cart_of_20(conv)
        state, patch, _rt = self._draft_step(conv)
        draft_v1 = patch["preorder_draft"]
        gps = dict(state)
        gps.update(patch)
        gps.update(location_shared=True, location_outcome="NEW_LOCATION_ACCEPTED",
                   location_lat=12.3714, location_lon=-1.5197)
        confirm = RecordingRuntime(
            responses={"confirm_preorder_draft": {"status": "success", "order_id": draft_v1["order_id"]}}
        )
        first = run(create_preorder(gps, confirm))
        again = dict(gps)
        again.update(first)
        again.update(location_shared=True, location_outcome="NEW_LOCATION_ACCEPTED",
                     location_lat=12.3714, location_lon=-1.5197)
        run(create_preorder(again, confirm))
        assert confirm.call_names.count("confirm_preorder_draft") == 1, confirm.call_names

    def test_stock_that_dropped_before_confirm_is_never_silently_ordered(self, conv):
        self._to_cart_of_20(conv)
        state, patch, _rt = self._draft_step(conv)
        conv.shop.stock[PROD_B] = 18.0  # 20 -> 18 entre le récap et le confirm
        gps = dict(state)
        gps.update(patch)
        gps.update(location_shared=True, location_outcome="NEW_LOCATION_ACCEPTED",
                   location_lat=12.3714, location_lon=-1.5197)
        confirm = RecordingRuntime(
            responses={"confirm_preorder_draft": {
                "status": "error", "reason": "insufficient_stock",
                "message": "Stock insuffisant : 18 disponibles.", "available_quantity": 18.0}}
        )
        final = run(create_preorder(gps, confirm))
        assert final["preorder_draft"]["status"] != PreorderDraftStatus.EXECUTED.value
        assert "commande" not in str(final.get("final_response", "")).lower() or "18" in str(
            final.get("final_response", "")
        ) or final["preorder_draft"]["status"] != PreorderDraftStatus.EXECUTED.value


# =====================================================================
# Golden E2E — les deux scénarios sont séparés
# =====================================================================


class TestGoldenScenarios:
    def test_golden_search_photo3_select3_then_request_461000_needs_no_shortage(self, conv):
        # SEARCH
        t = _search(conv)
        assert [v["product_id"] for v in _ctx(t.after)["vendors"]] == [PROD_A, PROD_B, PROD_C]
        menu_before = json.dumps(_ctx(t.after)["vendors"], sort_keys=True, default=str)
        # photo 3
        d = _photo(conv, "3")
        assert _photo_urls(d) == ["https://img/c1.jpg", "https://img/c2.jpg"]
        assert json.dumps(_ctx(conv.state())["vendors"], sort_keys=True, default=str) == menu_before
        # 3
        t = conv.send("3")
        chosen = _chosen(t.after)
        assert chosen["product_id"] == PROD_C and chosen["price"] == 461000.0
        assert chosen["available_qty"] == 461000
        # Je veux 461000 -> aucune rupture
        t = conv.send("Je veux 461000", llm=_set_quantity(461000.0))
        assert "insuffisant" not in t.response.lower()
        assert not _shortage(t.after)
        cart = _cart(t.after)
        assert len(cart) == 1 and cart[0]["product_id"] == PROD_C and cart[0]["quantity"] == 461000.0
        assert not (set(_business_calls(conv)) & AUCTION_TOOLS)

    @pytest.fixture(autouse=False)
    def _fake_db(self, monkeypatch):
        _install_fake_preorder_db(monkeypatch)
        monkeypatch.setattr(settings, "ESCROW_PAYMENT_ENABLED", False)

    def test_golden_shortage_reply20_confirm_creates_one_order_of_20_and_no_auction(
        self, conv, _fake_db
    ):
        # select B, request 461000 -> shortage (20)
        t = _to_shortage(conv, index="2")
        assert "20" in t.response and "insuffisant" in t.response.lower()
        # reply 20 -> purchase_quantity = 20, DIRECT
        t = conv.send("20")
        assert t.llm_calls == 0
        cart = _cart(t.after)
        assert [(line["product_id"], line["quantity"]) for line in cart] == [(PROD_B, 20.0)]
        # confirmation (PreorderDraft certifié) puis exécution
        state = _checkout_state(conv)
        rt = RecordingRuntime(responses={"create_preorder_draft": _server_draft_response(cart)})
        patch = run(create_preorder(state, rt))
        draft = patch["preorder_draft"]
        assert draft["items"][0]["quantity"] == 20.0
        gps = dict(state)
        gps.update(patch)
        gps.update(location_shared=True, location_outcome="NEW_LOCATION_ACCEPTED",
                   location_lat=12.3714, location_lon=-1.5197)
        confirm = RecordingRuntime(
            responses={"confirm_preorder_draft": {"status": "success", "order_id": draft["order_id"]}}
        )
        final = run(create_preorder(gps, confirm))
        assert final["preorder_draft"]["status"] == PreorderDraftStatus.EXECUTED.value
        # DB-boundary assertions : une commande, le bon produit/producteur/quantité/prix, zéro appel d'offres
        assert confirm.call_names.count("confirm_preorder_draft") == 1
        created = dict(rt.calls[0][1])["cart_items"][0]
        assert (created["product_id"], created["producer_id"], created["quantity"], created["price"]) == (
            PROD_B, P2, 20.0, 450000.0,
        )
        all_tools = _business_calls(conv) + rt.call_names + confirm.call_names
        assert not (set(all_tools) & AUCTION_TOOLS)

    def test_full_exact_whatsapp_scenario_with_photo3_and_same_producer_offers(self, conv):
        """Le scénario réel, dans l'ordre : menu, photo 3, « 3 », quantité 461000."""
        t = _search(conv)
        assert "Page 2/2" not in t.response
        _photo(conv, "3")
        t = conv.send("3")
        assert _chosen(t.after)["product_id"] == PROD_C, t.response
        assert "Gilbert-prod" in t.response and "461000" in t.response
        t = conv.send("Je veux 461000", llm=_set_quantity(461000.0))
        assert _cart(t.after)[0]["product_id"] == PROD_C
        assert t.goal_after != "PROCUREMENT_CREATE_REQUEST"
