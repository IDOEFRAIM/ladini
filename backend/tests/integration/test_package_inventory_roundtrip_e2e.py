"""B17 — ROUND-TRIP réel : conversation Producteur (vrai graphe) -> `create_product` RÉEL (ORM `Product`) -> « rechargement
DB » (JSON + Decimal comme PostgreSQL) -> `finalize_multi_order` / `cancel_pending_order` RÉELS -> stock restant.

Seuls le LLM, le MCP et la session SQL sont doublés (aucun PostgreSQL local : le même parcours tourne contre un vrai
PostgreSQL en CI, voir `tests/schema/test_package_inventory_roundtrip_pg.py`). Rien n'est lu depuis `working_memory`,
Redis ni un état de conversation : l'acheteur ne dispose QUE de la ligne rechargée.
"""
from __future__ import annotations

import json
import types
import uuid
from decimal import Decimal
from typing import Any, Dict, List

import pytest

from ladini.domain.models import Order, OrderItem, Product
from ladini.domain.package_inventory import assert_package_inventory_consistency
from ladini.services.database import buyer as buyer_mod
from ladini.services.database import producer as producer_mod
from ladini.services.database.buyer import BuyerMixin
from ladini.services.database.errors import BusinessRuleException
from ladini.services.database.producer import ProducerMgmtMixin
from tests.conftest import run
from tests.integration.test_producer_packaging_publication_e2e import (
    Conv,
    junk_gapal,
    junk_milk,
)


class _NoEvents:
    def __init__(self, *_a: Any, **_k: Any) -> None:
        pass

    def __getattr__(self, name: str):
        async def _n(*a: Any, **k: Any):
            return None

        return _n


class _Session:
    """Session SQL doublée : `scalar` -> le produit rechargé / la commande ; `add` capture."""

    def __init__(self, products: Dict[Any, Any]) -> None:
        self.products, self.added, self.order = products, [], None

    async def scalar(self, stmt: Any):
        if self.order is not None and "orders" in str(stmt).lower().split("from")[1][:30]:
            return self.order
        return next(iter(self.products.values()), None)

    async def scalars(self, _stmt: Any):
        return types.SimpleNamespace(all=lambda: list(self.products.values()))

    async def execute(self, _stmt: Any):
        return types.SimpleNamespace(first=lambda: None, all=lambda: [], scalar_one_or_none=lambda: None)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def _profile():
    return types.SimpleNamespace(id=uuid.uuid4(), zone_id=uuid.uuid4()), types.SimpleNamespace(id=uuid.uuid4())


def _producer_service(session: _Session):
    class _S(ProducerMgmtMixin):
        @property
        def session(self):
            return session

    s = _S()

    async def _phone(phone=None, producer_id=None):
        return phone

    async def _cfg(name):
        return None

    async def _prof(phone):
        return types.SimpleNamespace(id=uuid.uuid4()), types.SimpleNamespace(id=uuid.uuid4())

    async def _guess(product_name=None):
        return "Produits"

    s._resolve_producer_phone, s.get_product_category_unit_config = _phone, _cfg
    s.get_producer_profile, s.guess_category = _prof, _guess
    return s


def _buyer_service(session: _Session):
    class _S(BuyerMixin):
        @property
        def session(self):
            return session

    s = _S()
    user, prof = _profile()

    async def _gb(phone=None, **k):
        return user, prof

    s.get_buyer_profile = _gb
    s._buyer_profile = prof
    return s


def _publish(monkeypatch, kwargs: Dict[str, Any]) -> Product:
    """create_product RÉEL (kwargs EXACTS produits par la conversation) puis « rechargement » comme PostgreSQL."""
    monkeypatch.setattr(producer_mod, "BusinessEventEmitter", _NoEvents)
    session = _Session({})
    svc = _producer_service(session)
    fields = {k: v for k, v in kwargs.items() if k in {
        "phone", "name", "price", "quantity_for_sale", "unit", "pricing_tiers", "commercial_offer",
        "category_label", "description", "local_names", "sub_category_id"}}
    fields.setdefault("phone", "+22670000001")  # injecté par le contexte MCP en production
    res = run(svc.create_product(**fields))
    assert res["status"] == "success", res
    persisted = next(o for o in session.added if isinstance(o, Product))
    return _reload(persisted)


def _reload(p: Product) -> Product:
    """Aller-retour DB : JSONB sérialisé/relu, Numeric -> Decimal. Aucun objet Python partagé avec la publication."""
    return Product(
        id=p.id, name=p.name, price=Decimal(str(p.price)), unit=p.unit,
        quantity_for_sale=Decimal(str(p.quantity_for_sale)).quantize(Decimal("0.001")),
        producer_id=p.producer_id, is_available=True,
        pricing_tiers=json.loads(json.dumps(p.pricing_tiers)) if p.pricing_tiers is not None else None,
        commercial_pricing=json.loads(json.dumps(p.commercial_pricing)) if p.commercial_pricing else None,
    )


def _state(p: Product) -> Dict[str, Any]:
    return {"stock": float(p.quantity_for_sale), "counts": {t["tier_id"]: t.get("available_count") for t in p.pricing_tiers or []}}


def _tier(p: Product, size: float) -> str:
    return next(t["tier_id"] for t in p.pricing_tiers if abs(float(t["base_unit_quantity"]) - size) < 1e-9)


def _buy(monkeypatch, p: Product, tier_id: str | None, qty: float):
    monkeypatch.setattr(buyer_mod, "BusinessEventEmitter", _NoEvents)
    session = _Session({p.id: p})
    svc = _buyer_service(session)
    item = {"product_id": str(p.id), "quantity": qty}
    if tier_id:
        item["tier_id"] = tier_id
    res = run(svc.finalize_multi_order([item], "+22670000009"))
    return res, session


def _order_items(session: _Session) -> List[OrderItem]:
    return [o for o in session.added if isinstance(o, OrderItem)]


# ═════════ CAS 1 : 100 sachets de 500 ml ═════════
def _publish_cas1(monkeypatch) -> Product:
    c = Conv(junk_milk)
    c.say("J aimerais mettre en vente 100 sachet de lait frais pasteurisé de 500ml")
    c.say("500f pour 500mililitre")
    c.say("oui")
    return _publish(monkeypatch, c.created())


def test_cas1_publish_reload_buy_5_sachets(monkeypatch):
    p = _publish_cas1(monkeypatch)
    assert _state(p) == {"stock": 50.0, "counts": {p.pricing_tiers[0]["tier_id"]: 100}}  # rechargé : compte + stock
    assert p.pricing_tiers[0]["price"] == 500.0 and p.pricing_tiers[0]["packaging"] == "sachet"
    assert_package_inventory_consistency(p)
    tid = p.pricing_tiers[0]["tier_id"]
    _res, session = _buy(monkeypatch, p, tid, 5)
    (line,) = _order_items(session)
    assert float(line.price_at_sale) == 500.0 and float(line.quantity) == 5 and float(line.base_unit_quantity) == 2.5
    assert str(line.tier_id) == tid  # identité de la variante dans le snapshot de commande
    order = next(o for o in session.added if isinstance(o, Order))
    assert float(order.total_amount) == 2500.0
    assert _state(p) == {"stock": 47.5, "counts": {tid: 95}}
    assert_package_inventory_consistency(p)
    # nouveau « redémarrage » : on recharge depuis la ligne persistée (JSON), aucun état de conversation
    p2 = _reload(p)
    assert _state(p2) == _state(p)


def test_buyer_without_a_selected_variant_never_falls_back_to_the_legacy_price(monkeypatch):
    p = _publish_cas1(monkeypatch)
    with pytest.raises(BusinessRuleException) as e:
        _buy(monkeypatch, p, None, 5)
    assert getattr(e.value, "reason", None) == "tier_required"
    assert _state(p)["stock"] == 50.0


# ═════════ CAS 2 : 50 bidons de 500 ml + 100 bidons de 330 ml ═════════
def _publish_cas2(monkeypatch) -> Product:
    c = Conv(junk_gapal)
    c.say("50 bidons de gapal de 500ml et 100 bidons de 330 mL disponible")
    for answer in ("je veux les vendre", "500 et 350", "oui"):
        c.say(answer)
    return _publish(monkeypatch, c.created())


def test_cas2_two_variants_buy_both_then_oversell_refused_then_cancel_restores_once(monkeypatch):
    p = _publish_cas2(monkeypatch)
    t500, t330 = _tier(p, 0.5), _tier(p, 0.33)
    assert _state(p) == {"stock": 58.0, "counts": {t500: 50, t330: 100}}
    prices = {t["tier_id"]: t["price"] for t in p.pricing_tiers}
    assert prices == {t500: 500.0, t330: 350.0}
    res_a, sess_a = _buy(monkeypatch, p, t500, 10)
    assert _state(p) == {"stock": 53.0, "counts": {t500: 40, t330: 100}}
    _buy(monkeypatch, p, t330, 20)
    st = _state(p)
    assert st["counts"] == {t500: 40, t330: 80} and st["stock"] == pytest.approx(46.4)
    assert_package_inventory_consistency(p)
    # 41 × 500 ml : refusé alors que 46,4 L couvriraient 20,5 L
    with pytest.raises(BusinessRuleException) as e:
        _buy(monkeypatch, p, t500, 41)
    assert getattr(e.value, "reason", None) == "insufficient_package_stock"
    assert _state(p)["counts"] == {t500: 40, t330: 80}
    # annulation de la commande A (10 × 500 ml) : +10 sachets/bidons, +5 L, UNE fois
    order_a = next(o for o in sess_a.added if isinstance(o, Order))
    items_a = _order_items(sess_a)
    for it in items_a:
        it.product, it.product_id = p, p.id
    order_a.items, order_a.status = items_a, "PENDING"
    order_a.buyer_id = None  # le service résout le profil acheteur (doublé) ; l'égalité est ignorée par la session
    monkeypatch.setattr(buyer_mod, "BusinessEventEmitter", _NoEvents)
    sess = _Session({p.id: p})
    sess.order = order_a
    svc = _buyer_service(sess)

    async def _cancel():
        return await svc.cancel_pending_order(str(order_a.id), "+22670000009")

    run(_cancel())
    st = _state(p)
    assert st["counts"] == {t500: 50, t330: 80} and st["stock"] == pytest.approx(51.4)
    with pytest.raises(BusinessRuleException):  # rejouer : refusé, aucun changement
        run(_cancel())
    assert _state(p) == st


# ═════════ conditionnement en MASSE et unité de base ═════════
def test_mass_package_20_sacs_of_25kg_buy_3(monkeypatch):
    kwargs = {
        "phone": "+22670000001", "name": "riz", "price": 15000.0, "quantity_for_sale": 500.0, "unit": "KG",
        "pricing_tiers": [{"quantity": 25, "unit": "KG", "price": 15000, "packaging": "sac", "available_count": 20}],
    }
    p = _publish(monkeypatch, kwargs)
    tid = p.pricing_tiers[0]["tier_id"]
    _res, session = _buy(monkeypatch, p, tid, 3)
    assert _state(p) == {"stock": 425.0, "counts": {tid: 17}}
    assert float(next(o for o in session.added if isinstance(o, Order)).total_amount) == 45000.0


def test_base_unit_product_needs_no_package_inventory(monkeypatch):
    kwargs = {"phone": "+22670000001", "name": "lait", "price": 1200.0, "quantity_for_sale": 50.0, "unit": "LITRE"}
    p = _publish(monkeypatch, kwargs)
    assert p.pricing_tiers is None
    _res, session = _buy(monkeypatch, p, None, 5)
    assert float(p.quantity_for_sale) == 45.0
    assert float(next(o for o in session.added if isinstance(o, Order)).total_amount) == 6000.0


# ═════════ Acheteur : VRAI graphe, vérification de stock = VRAI service sur la ligne rechargée ═════════
def _buyer_conv(monkeypatch, p: Product, name: str):
    from tests.integration.test_buyer_deterministic_product_switch_e2e import (
        _Conv,
        _offer,
        _Rt,
    )

    offer = _offer(name, "LITRE", "Ferme1", str(p.id))
    offer["price"] = float(p.price)  # le prix LEGACY (shadow) — ne doit jamais devenir le total du panier
    offer["pricing_tiers"] = json.loads(json.dumps(p.pricing_tiers))
    offer["available_quantity"] = float(p.quantity_for_sale)
    monkeypatch.setattr(buyer_mod, "BusinessEventEmitter", _NoEvents)
    svc = _buyer_service(_Session({p.id: p}))

    class _RealStock(_Rt):
        async def call_db(self, tool: str, **kw: Any) -> Any:
            if tool == "validate_stock_availability_atomic":
                self.tool_log.append((tool, dict(kw)))
                allowed = {k: v for k, v in kw.items() if k in {
                    "product_id", "quantity", "unit", "buyer_phone", "tier_id", "package_count"}}
                return await svc.validate_stock_availability_atomic(**allowed)
            return await super().call_db(tool, **kw)

    c = _Conv([offer])
    c.rt = _RealStock(c.llm, [offer])
    from langgraph.checkpoint.memory import MemorySaver

    from ladini.graphs.agents.market_coach.core.graph_builder import build_graph

    c.graph = build_graph("BUYER", mc_runtime=c.rt, checkpointer=MemorySaver())
    return c


def _cart(st):
    return [(x["quantity"], x.get("base_unit_quantity"), x["line_total"]) for x in st["active_cart"]]


def test_buyer_graph_cas1_five_sachets_cart_is_2500_and_2_5_litres(monkeypatch):
    p = _publish_cas1(monkeypatch)
    c = _buyer_conv(monkeypatch, p, "lait frais pasteurisé")
    c.say("je veux acheter du lait")
    c.say("1")  # le seul conditionnement : sachet 500 ml
    st = c.say("5")
    assert _cart(st) == [(5, 2.5, 2500.0)]  # 5 × 500 FCFA (prix du PACKAGE, pas le prix legacy) ; 2,5 L
    check = c.rt.of("validate_stock_availability_atomic")[-1]
    assert check["tier_id"] == p.pricing_tiers[0]["tier_id"] and check["package_count"] == 5


def test_buyer_graph_cannot_oversell_a_variant_even_when_global_litres_suffice(monkeypatch):
    p = _publish_cas2(monkeypatch)
    t500 = _tier(p, 0.5)
    _buy(monkeypatch, p, t500, 10)  # 500 ml : 40 restants, stock 53 L
    c = _buyer_conv(monkeypatch, p, "lait gapal")
    st = c.say("je veux acheter du lait")
    assert "500" in st["final_response"] and "330" in st["final_response"]
    order = [t["base_unit_quantity"] for t in st["tier_selection_context"]["tiers"]]
    c.say(str(order.index(0.5) + 1))
    st = c.say("41")  # 41 × 0,5 L = 20,5 L <= 53 L, mais seulement 40 bidons de 500 ml
    assert st["active_cart"] == []
    assert "40" in (st.get("final_response") or "")
    # Conversation neuve (la récupération conversationnelle APRÈS un refus est un point d'UX distinct, documenté) :
    # exactement les 40 bidons disponibles passent.
    c2 = _buyer_conv(monkeypatch, p, "lait gapal")
    c2.say("je veux acheter du lait")
    c2.say(str(order.index(0.5) + 1))
    st = c2.say("40")
    assert _cart(st) == [(40, 20.0, 20000.0)]
