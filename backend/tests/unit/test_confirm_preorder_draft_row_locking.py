"""Verrouillage `SELECT ... FOR UPDATE` + ordonnancement anti-deadlock —
`BuyerMixin.confirm_preorder_draft` (2026-09-04, clôture frontière
CART→CHECKOUT).

## Portée EXACTE de cette preuve (même discipline que
`test_auction_bid_row_locking.py`, dont ce fichier est le gabarit direct)

Ce dépôt n'a AUCUNE infrastructure de test Postgres réelle. Ce fichier
prouve deux choses honnêtement limitées, jamais un comportement de
concurrence réel :

1. La requête `Order` ET chaque requête `Product` par article portent bien
   `FOR UPDATE` dans le SQL RÉELLEMENT compilé (dialecte postgresql) — la
   protection anti-survente est dans le chemin exécuté, pas dans un
   commentaire.
2. Les verrous `Product` sont acquis dans l'ORDRE lexicographique du
   `product_id` — jamais l'ordre du panier — reproduisant le correctif
   anti-deadlock du 2026-09-04 (tri déterministe avant `FOR UPDATE`, motif
   déjà prouvé pour `finalize_multi_order`). Un ordre non déterministe entre
   deux précommandes concurrentes portant les 2 mêmes produits dans un ordre
   différent formerait un cycle de verrous (deadlock Postgres) — ce test ne
   PROUVE pas l'absence de deadlock sous charge réelle (impossible sans
   Postgres), seulement que l'ordre d'acquisition ne dépend plus de l'ordre
   du panier."""
from __future__ import annotations

import types
import uuid

import pytest

from tests.conftest import run

from ladini.services.database.buyer import BuyerMixin


def _fake_buyer_profile():
    user = types.SimpleNamespace(id=uuid.uuid4(), zone_id=None)
    profile = types.SimpleNamespace(id=uuid.uuid4())
    return user, profile


async def _async_return(value):
    return value


def _compiled_sql_literal(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _compiled_sql(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))


class _CapturingSession:
    """1er `.scalar()` -> l'`Order` verrouillé ; chaque appel suivant ->
    résolu par correspondance du `product_id` littéral dans le SQL compilé
    (ne simule AUCUNE sémantique de verrouillage réelle — voir portée en
    tête de fichier)."""

    def __init__(self, order, products_by_id):
        self.captured_statements: list = []
        self._order = order
        self._products_by_id = products_by_id
        self._n = 0

    async def scalar(self, stmt):
        self.captured_statements.append(stmt)
        self._n += 1
        if self._n == 1:
            return self._order
        sql = _compiled_sql_literal(stmt)
        for pid, product in self._products_by_id.items():
            if str(pid) in sql:
                return product
        return None

    async def execute(self, _stmt):
        # (2026-09-04, F2) : `confirm_preorder_draft` résout le(s) téléphone(s)
        # producteur pour la notification post-confirmation. Depuis Producer
        # Analytics Phase B, les produits de ce test portent un vrai
        # `producer_id` (nécessaire pour que la résolution `producer_id` de
        # `emit_direct_order_created` reste à coût zéro — voir `_product`),
        # ce qui active cette branche : aucun producteur réel n'est simulé,
        # donc `.all()` renvoie une liste vide (aucun téléphone résolu) — la
        # notification elle-même reste hors du périmètre de CE test
        # (verrouillage/ordre), seulement rendue non-crashante ici.
        return types.SimpleNamespace(first=lambda: None, all=lambda: [])

    async def flush(self):
        pass


def _service(session):
    class _Svc(BuyerMixin):
        @property
        def session(self):
            return session

    svc = _Svc()
    svc.get_buyer_profile = lambda phone: _async_return(_fake_buyer_profile())
    return svc


def _product(pid, qty=1000.0):
    return types.SimpleNamespace(
        id=pid, name=f"Produit-{str(pid)[:4]}", quantity_for_sale=qty, unit="KG",
        # Producer Analytics Phase B: emit_direct_order_created resolves producer_id
        # from the already-loaded item.product relationship — a real (non-None) id
        # here keeps that resolution a zero-extra-query read, exactly like production,
        # so it doesn't add a 3rd captured statement to this file's lock-order count.
        producer_id=uuid.uuid4(),
    )


class TestConfirmPreorderDraftLocksOrderAndProducts:
    def test_the_order_lookup_is_for_update(self, monkeypatch):
        session = _CapturingSession(order=None, products_by_id={})
        svc = _service(session)

        with pytest.raises(Exception):
            run(
                svc.confirm_preorder_draft(
                    buyer_phone="+22670000001", preorder_id=str(uuid.uuid4())
                )
            )
        assert session.captured_statements
        sql = _compiled_sql(session.captured_statements[0])
        assert "FOR UPDATE" in sql.upper()

    def test_product_locks_are_acquired_in_lexicographic_product_id_order_not_cart_order(
        self, monkeypatch
    ):
        # IDs choisis pour que l'ordre lexicographique (a... avant f...)
        # diffère explicitement de l'ordre du panier (B ajouté avant A).
        pid_a = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000000")
        pid_b = uuid.UUID("ffffffff-0000-0000-0000-000000000000")
        product_a = _product(pid_a)
        product_b = _product(pid_b)

        item_b = types.SimpleNamespace(
            product_id=pid_b, quantity=5.0, price_at_sale=100.0, base_unit_quantity=None,
            product=product_b,
        )
        item_a = types.SimpleNamespace(
            product_id=pid_a, quantity=5.0, price_at_sale=100.0, base_unit_quantity=None,
            product=product_a,
        )
        order = types.SimpleNamespace(
            id=uuid.uuid4(),
            buyer_id=uuid.uuid4(),
            status="DRAFT",
            items=[item_b, item_a],  # ordre panier : B puis A
            preorder_converted_at=None,
            payment_status=None,
            subtotal=0.0,
            total_amount=0.0,
            currency="XOF",
            # (2026-09-05, Phase 6A) commande hors checkout groupé : la
            # garantie anti-deadlock testée ici doit valoir AUSSI pour une
            # commande seule (le tri porte désormais sur l'union des
            # articles du groupe, qui vaut ici exactement cette commande).
            checkout_group_id=None,
        )
        session = _CapturingSession(
            order=order, products_by_id={pid_a: product_a, pid_b: product_b}
        )
        svc = _service(session)

        result = run(
            svc.confirm_preorder_draft(buyer_phone="+22670000001", preorder_id=str(order.id))
        )

        assert result["status"] == "success"
        # 3 statements : Order (FOR UPDATE) + 2x Product (FOR UPDATE, tri A puis B)
        product_statements = session.captured_statements[1:]
        assert len(product_statements) == 2
        sql_first = _compiled_sql_literal(product_statements[0])
        sql_second = _compiled_sql_literal(product_statements[1])
        assert str(pid_a) in sql_first, "le produit A (id lexicographiquement plus petit) doit être verrouillé EN PREMIER"
        assert str(pid_b) in sql_second
        for stmt in product_statements:
            assert "FOR UPDATE" in _compiled_sql(stmt).upper()

        # Débit de stock réellement appliqué aux DEUX produits.
        assert product_a.quantity_for_sale == 995.0
        assert product_b.quantity_for_sale == 995.0
