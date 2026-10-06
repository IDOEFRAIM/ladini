"""`PreorderDraft` lifecycle ↔ underlying `Order` lifecycle — synchronisation
(2026-09-04, audit d'impact Order(DRAFT) orphelin).

## Le gap réel fermé par ce fichier

L'audit `PREORDER_ORDER_DRAFT_ORPHAN_IMPACT_2026-09-04.md` a prouvé un
impact utilisateur RÉEL (pas seulement une "ligne orpheline" en base) :
`get_buyer_orders_dashboard`/`get_transaction_summary` ne filtraient AUCUN
statut — un `Order(status="DRAFT")` abandonné (CANCEL sans synchronisation
de l'`Order`, ou remplacé par un cycle "ajouter d'autres produits") pouvait
donc s'afficher comme une VRAIE commande dans le tableau de bord de
l'acheteur, ou être renvoyé comme "sa dernière transaction" par
`order_tracking.py::check_order_status` — masquant sa VRAIE dernière
commande. Deux catégories de correctifs, verrouillées ici :

1. **Write side** (cause racine) — `_cancel_preorder`
   (`flows/buyer/preorder.py`) et le cycle "ajouter d'autres produits"
   (`bootstrap_preorder_draft`, `flows/buyer/preorder_confirmation.py`)
   transitionnent désormais RÉELLEMENT l'`Order` Postgres sous-jacent en
   même temps que le `PreorderDraft` applicatif — `CANCELLED` (rejet
   explicite acheteur) vs `SUPERSEDED` (remplacé par un nouveau brouillon,
   PAS un rejet — distinction assumée, voir le rapport).
2. **Read side** (filet de sécurité immédiat) — verrouillé séparément par
   `tests/unit/test_buyer_order_reads_exclude_draft_status.py`.

## Portée honnête

Même faux moteur SQL que `test_preorder_draft_persistence.py`
(`_install_fake_db`/`_draft`), même frontière `RecordingRuntime` que le
reste de cette suite pour la couche `mc_runtime.call_db` — les appels
`cancel_preorder_draft` sont capturés (nom + kwargs), jamais exécutés contre
un vrai Postgres."""
from __future__ import annotations

from typing import Any, Dict

from tests.architecture.test_preorder_draft_persistence import _draft, _install_fake_db
from tests.conftest import make_state, run
from tests.evals.runners.harness import RecordingRuntime

from ladini.graphs.agents.market_coach.domain.preorder_draft import PreorderDraftStatus
from ladini.graphs.agents.market_coach.flows.buyer import preorder as preorder_mod
from ladini.graphs.agents.market_coach.flows.buyer.preorder_confirmation import (
    bootstrap_preorder_draft,
)
from ladini.services.database import preorder_draft_store as store_mod

CART = [
    {
        "product_id": "p1", "name": "tomates", "quantity": 55,
        "unit": "KG", "price": 225, "producer_id": "prod1", "status": "ACTIVE",
    }
]


def _cancel_state(monkeypatch, *, order_id: str = "order-abc") -> Dict[str, Any]:
    _install_fake_db(monkeypatch)
    draft = _draft(draft_id="d1", order_id=order_id)
    run(store_mod.insert(draft, conversation_id="+22670000000"))
    state = make_state(
        current_goal="BUYER_PREORDER_CONFIRM",
        user_phone="+22670000000",
        active_cart=CART,
        preorder_draft=draft.to_dict(),
        transaction_payload={"resolved_id": "PREORDER_CANCEL"},
        interpreted_event="REJECT",
    )
    return state


class TestCancelPreorderSynchronizesTheUnderlyingOrder:
    def test_cancel_transitions_both_the_draft_and_the_order(self, monkeypatch):
        state = _cancel_state(monkeypatch, order_id="order-to-cancel")
        runtime = RecordingRuntime()

        result = run(preorder_mod.create_preorder(state, runtime))

        assert result["preorder_draft"]["status"] == "CANCELLED"
        assert "cancel_preorder_draft" in runtime.call_names
        call = next(c for c in runtime.calls if c[0] == "cancel_preorder_draft")
        kwargs = call[1]
        assert kwargs["preorder_id"] == "order-to-cancel"
        assert kwargs.get("target_status", "CANCELLED") == "CANCELLED"
        assert kwargs["buyer_phone"] == "+22670000000"

    def test_double_cancel_calls_the_order_sync_exactly_once(self, monkeypatch):
        """CANCEL x2 doit rester idempotent — la 2e annulation ne
        transitionne plus le draft (`apply_domain_action` la renvoie
        inchangée, `DRAFT_FINALIZED`) et ne doit donc PAS déclencher un
        second appel MCP sur un `Order` déjà non-DRAFT."""
        state = _cancel_state(monkeypatch, order_id="order-double-cancel")
        runtime1 = RecordingRuntime()
        result1 = run(preorder_mod.create_preorder(state, runtime1))
        assert result1["preorder_draft"]["status"] == "CANCELLED"
        assert runtime1.call_names.count("cancel_preorder_draft") == 1

        # 2e annulation — l'état persisté est déjà CANCELLED (même faux
        # moteur SQL, aucune réinitialisation).
        state2 = dict(state)
        state2["preorder_draft"] = result1["preorder_draft"]
        runtime2 = RecordingRuntime()
        result2 = run(preorder_mod.create_preorder(state2, runtime2))

        assert result2["preorder_draft"]["status"] == "CANCELLED"
        assert "cancel_preorder_draft" not in runtime2.call_names

    def test_a_failed_order_sync_never_breaks_the_cancel_confirmation(self, monkeypatch):
        """Best-effort explicite (mandat) : un MCP `cancel_preorder_draft`
        en échec ne doit JAMAIS empêcher l'acheteur de voir sa précommande
        confirmée annulée — seulement journalisé (DEGRADED)."""
        state = _cancel_state(monkeypatch, order_id="order-sync-fails")
        runtime = RecordingRuntime(
            responses={"cancel_preorder_draft": RuntimeError("MCP timeout")}
        )

        result = run(preorder_mod.create_preorder(state, runtime))

        assert result["preorder_draft"]["status"] == "CANCELLED"
        assert "annulée" in result["final_response"].lower()


class TestAddMoreSupersedesThePreviousOrder:
    def test_recomposing_the_cart_supersedes_the_old_order_never_cancels_it(self, monkeypatch):
        """Le cycle "ajouter d'autres produits" REMPLACE le brouillon — ce
        n'est PAS un rejet acheteur : l'ancien `Order` doit devenir
        `SUPERSEDED`, jamais `CANCELLED` (distinction assumée par le
        modèle métier, voir le rapport d'audit)."""
        _install_fake_db(monkeypatch)
        existing = _draft(draft_id="d2", order_id="order-v1", total_amount=5000.0)
        run(store_mod.insert(existing, conversation_id="+22670000001"))

        state = make_state(user_phone="+22670000001", preorder_draft=existing.to_dict())
        runtime = RecordingRuntime(
            responses={
                "create_preorder_draft": {
                    "status": "success",
                    "preorder_id": "order-v2",
                    "total_amount": 9000.0,
                    "currency": "XOF",
                    "items": [
                        {"product_id": "p2", "name": "maïs", "quantity": 20,
                         "unit": "KG", "price": 450.0, "line_total": 9000.0}
                    ],
                }
            }
        )

        patch = run(
            bootstrap_preorder_draft(
                state, runtime,
                items_payload=[{"product_id": "p2", "name": "maïs", "quantity": 20, "unit": "KG", "price": 450.0}],
                meta={"total_amount": 9000.0, "currency": "XOF"},
            )
        )

        new_draft = patch["preorder_draft"]
        assert new_draft["order_id"] == "order-v2"
        assert new_draft["draft_id"] == "d2"  # MÊME draft, nouvelle version
        assert new_draft["version"] == existing.version + 1

        assert "cancel_preorder_draft" in runtime.call_names
        call = next(c for c in runtime.calls if c[0] == "cancel_preorder_draft")
        kwargs = call[1]
        assert kwargs["preorder_id"] == "order-v1"
        assert kwargs["target_status"] == "SUPERSEDED"

    def test_add_more_twice_never_leaves_two_active_drafts(self, monkeypatch):
        """ADD_MORE x2 — un seul `PreorderDraft` (même draft_id) porte à
        chaque fois le DERNIER `order_id` ; chaque ancien `Order` traversé
        est superseded exactement une fois."""
        _install_fake_db(monkeypatch)
        v1 = _draft(draft_id="d3", order_id="order-r1", total_amount=1000.0)
        run(store_mod.insert(v1, conversation_id="+22670000002"))

        state1 = make_state(user_phone="+22670000002", preorder_draft=v1.to_dict())
        runtime1 = RecordingRuntime(
            responses={
                "create_preorder_draft": {
                    "status": "success", "preorder_id": "order-r2",
                    "total_amount": 2000.0, "currency": "XOF", "items": [],
                }
            }
        )
        patch1 = run(
            bootstrap_preorder_draft(
                state1, runtime1,
                items_payload=[{"product_id": "p1", "name": "x", "quantity": 1, "unit": "KG", "price": 100.0}],
                meta={"total_amount": 2000.0, "currency": "XOF"},
            )
        )
        v2 = patch1["preorder_draft"]
        assert v2["order_id"] == "order-r2"
        assert runtime1.call_names.count("cancel_preorder_draft") == 1

        state2 = make_state(user_phone="+22670000002", preorder_draft=v2)
        runtime2 = RecordingRuntime(
            responses={
                "create_preorder_draft": {
                    "status": "success", "preorder_id": "order-r3",
                    "total_amount": 3000.0, "currency": "XOF", "items": [],
                }
            }
        )
        patch2 = run(
            bootstrap_preorder_draft(
                state2, runtime2,
                items_payload=[{"product_id": "p1", "name": "x", "quantity": 1, "unit": "KG", "price": 100.0}],
                meta={"total_amount": 3000.0, "currency": "XOF"},
            )
        )
        v3 = patch2["preorder_draft"]
        assert v3["draft_id"] == "d3"  # même filiation, un seul draft actif
        assert v3["order_id"] == "order-r3"

        call2 = next(c for c in runtime2.calls if c[0] == "cancel_preorder_draft")
        assert call2[1]["preorder_id"] == "order-r2"  # supersede le v2, pas le v1
        assert call2[1]["target_status"] == "SUPERSEDED"

        # Une seule ligne PostgreSQL de draft existe pour ce cycle (pas un
        # doublon "actif") — chargée sous son unique draft_id.
        loaded = run(store_mod.load("d3"))
        assert loaded.order_id == "order-r3"
        assert loaded.status == PreorderDraftStatus.DRAFT
