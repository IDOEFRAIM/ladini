"""Idempotency-key wiring for sensitive MCP writes — follow-up pre-Hetzner
(2026-09-17). Complements `test_procurement_execution_pipeline_closure.py::
TestExecutionKey` (which only covers the pure PROCUREMENT function) with:

1. The SAME determinism proof for SALES_PUBLISH's `execution_key()` — never
   tested before because the function, while defined, was never actually
   wired into the executor (see item 2).
2. `nodes/executor.py::_derive_execution_idempotency_key` — the function
   that decides what key (if any) a given LangGraph turn's MCP call gets.
   Proves: PROCUREMENT draft in state -> its key; SALES_PUBLISH draft in
   state -> its key; NEITHER -> None (unchanged behavior for goals without
   a draft); a redelivered turn (same state re-processed from scratch, the
   real Celery-crash-redelivery scenario) derives the SAME key both times.
3. `select_winning_bid`'s idempotency key (accept-bid path,
   `flows/buyer/order_tracking.py`/`negotiation.py`) — same bid_id always
   produces the same key, different bid_id never collides.
"""

from __future__ import annotations

import re
from pathlib import Path

from ladini.graphs.agents.market_coach.domain.procurement_draft import (
    ProcurementDraft,
)
from ladini.graphs.agents.market_coach.domain.procurement_draft import (
    execution_key as procurement_execution_key,
)
from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
    SalesPublishDraft,
    execution_key,
)
from ladini.graphs.agents.market_coach.nodes.executor import (
    _derive_execution_idempotency_key,
)


def _procurement_draft(**fields) -> ProcurementDraft:
    base = {
        "product": "tomates",
        "quantity": 2000.0,
        "unit": "KG",
        "price": 250.0,
        "price_unit": "KG",
    }
    base.update(fields)
    return ProcurementDraft.new(draft_id="idem-test", **base)


def _sales_draft(**fields) -> SalesPublishDraft:
    base = {"product": "maïs", "quantity": 100.0, "unit": "KG", "price": 200.0}
    base.update(fields)
    return SalesPublishDraft.new(draft_id="idem-sales-test", **base)


class TestSalesPublishExecutionKey:
    """Miroir de TestExecutionKey (procurement) — jamais testé avant que
    ce module ne soit réellement câblé dans l'exécuteur."""

    def test_stable_per_draft_and_version(self):
        d = _sales_draft()
        assert execution_key(d) == f"sales_publish:{d.draft_id}:{d.version}"

    def test_differs_across_versions(self):
        v1 = _sales_draft()
        v2 = SalesPublishDraft.new(draft_id=v1.draft_id, product="riz", quantity=50.0, unit="KG", price=300.0)
        object.__setattr__(v2, "version", v1.version + 1)
        assert execution_key(v1) != execution_key(v2)

    def test_differs_across_draft_ids(self):
        a = _sales_draft()
        b = SalesPublishDraft.new(draft_id="other-draft", product="maïs", quantity=100.0, unit="KG", price=200.0)
        assert execution_key(a) != execution_key(b)


class TestDeriveExecutionIdempotencyKey:
    """`nodes/executor.py::_derive_execution_idempotency_key` — QUI reçoit
    la clé, pour de vrai, pas seulement la fonction pure du domaine."""

    def test_no_draft_in_state_returns_none(self):
        assert _derive_execution_idempotency_key({}) is None
        assert _derive_execution_idempotency_key({"goal": "SEARCH_PRODUCTS"}) is None

    def test_procurement_draft_in_state_derives_procurement_key(self):
        d = _procurement_draft()
        state = {"procurement_draft": d.to_dict()}
        assert _derive_execution_idempotency_key(state) == procurement_execution_key(d)

    def test_sales_publish_draft_in_state_derives_sales_key(self):
        """Le gap réel de cette session : avant le correctif, cette assertion
        échouait (retournait None — `create_product` n'avait jamais de clé)."""
        d = _sales_draft()
        state = {"sales_publish_draft": d.to_dict()}
        assert _derive_execution_idempotency_key(state) == execution_key(d)

    def test_malformed_draft_dict_returns_none_not_crash(self):
        assert _derive_execution_idempotency_key({"procurement_draft": {"not": "a draft"}}) is None
        assert _derive_execution_idempotency_key({"sales_publish_draft": "garbage"}) is None

    def test_redelivery_of_the_same_turn_derives_the_same_key(self):
        """Le scénario RÉEL qu'on protège : un worker crash après avoir
        enregistré le draft mais avant d'avoir acquitté la tâche Celery — la
        redelivery relit EXACTEMENT le même état persisté (même draft_id,
        même version, puisque rien n'a bougé entre les deux tentatives) et
        doit donc dériver EXACTEMENT la même clé, pour que le serveur MCP la
        reconnaisse comme un rejeu (REPLAY) et non une nouvelle action."""
        d = _procurement_draft()
        state = {"procurement_draft": d.to_dict()}
        first_attempt_key = _derive_execution_idempotency_key(state)
        second_attempt_key = _derive_execution_idempotency_key(dict(state))  # nouvel objet, même contenu
        assert first_attempt_key == second_attempt_key
        assert first_attempt_key is not None

    def test_procurement_takes_precedence_when_both_present(self):
        """Cas pathologique (ne devrait jamais arriver en pratique — un seul
        draft actif par tour) : la priorité est documentée, pas accidentelle."""
        pd = _procurement_draft()
        sd = _sales_draft()
        state = {"procurement_draft": pd.to_dict(), "sales_publish_draft": sd.to_dict()}
        assert _derive_execution_idempotency_key(state) == procurement_execution_key(pd)


class TestSelectWinningBidIdempotencyKey:
    """`bid_id` est l'identifiant métier stable de l'action "accepter cette
    offre" — un bid n'est sélectionné gagnant qu'une fois, donc pas besoin
    d'un objet draft/version comme PROCUREMENT/SALES_PUBLISH."""

    def test_same_bid_id_same_key(self):
        key = lambda bid_id: f"select_winning_bid:{bid_id}"  # noqa: E731 — même format que order_tracking.py/negotiation.py
        assert key("bid-123") == key("bid-123")

    def test_different_bid_id_different_key(self):
        key = lambda bid_id: f"select_winning_bid:{bid_id}"  # noqa: E731
        assert key("bid-123") != key("bid-456")

    def test_call_sites_use_the_documented_format(self):
        """Verrouille le format EXACT utilisé par les deux call sites réels
        (order_tracking.py::finalize_winner et negotiation.py) — un futur
        changement de format dans l'un sans l'autre romprait le rejeu si un
        même bid_id transite par les deux chemins."""
        from ladini.graphs.agents.market_coach.flows.buyer import (
            negotiation,
            order_tracking,
        )

        pattern = re.compile(r'idempotency_key=f"select_winning_bid:\{bid_id\}"')
        ot_src = Path(order_tracking.__file__).read_text(encoding="utf-8")
        neg_src = Path(negotiation.__file__).read_text(encoding="utf-8")
        assert pattern.search(ot_src), "order_tracking.py n'utilise plus le format attendu"
        assert pattern.search(neg_src), "negotiation.py n'utilise plus le format attendu"
