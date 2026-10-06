"""`PreorderReconciliationService` (2026-09-03, clôture escrow/IPN) — sur
le modèle de `test_procurement_reconciliation_service.py`, adapté aux 2
candidats PREORDER (EXECUTING et AWAITING_PAYMENT)."""
from __future__ import annotations

import threading

import pytest

from tests.conftest import run
from tests.architecture.test_preorder_draft_persistence import _draft, _install_fake_db
from tests.unit.test_mcp_idempotency import _install_fake_db as _install_fake_idempotency_db

from ladini.graphs.agents.market_coach.domain.preorder_draft import (
    PreorderDraft,
    PreorderDraftStatus,
    execution_key,
)
from ladini.services.database import mcp_idempotency_store, preorder_draft_store
from ladini.services.reconciliation import preorder_reconciliation_service as svc


def _install(monkeypatch):
    draft_table = _install_fake_db(monkeypatch)
    idem_table = _install_fake_idempotency_db(monkeypatch)
    return draft_table, idem_table


def _executing_draft(draft_id: str = "recon1") -> PreorderDraft:
    v1 = _draft(draft_id=draft_id)
    return v1._confirm_to_executing(delivery_lat=1.0, delivery_lon=1.0)


class TestFindStaleCandidates:
    def test_uses_the_configured_threshold(self, monkeypatch):
        from ladini.core.settings import settings

        _install(monkeypatch)
        seen = {}
        original = preorder_draft_store.find_stale_by_status

        async def _spy(status, *, older_than_seconds):
            seen[status] = older_than_seconds
            return await original(status, older_than_seconds=older_than_seconds)

        monkeypatch.setattr(preorder_draft_store, "find_stale_by_status", _spy)
        run(svc.find_stale_executing_candidates())
        run(svc.find_stale_awaiting_payment_candidates())
        assert seen["EXECUTING"] == settings.PREORDER_EXECUTING_STALE_SECONDS
        assert seen["AWAITING_PAYMENT"] == settings.PREORDER_EXECUTING_STALE_SECONDS


class TestReconcileExecutingDraft:
    def test_not_executing_is_a_safe_no_op(self, monkeypatch):
        _install(monkeypatch)
        draft = _draft(draft_id="d1")  # DRAFT
        result = run(svc.reconcile_executing_draft(draft))
        assert result.outcome is svc.ReconciliationOutcome.NOT_APPLICABLE
        assert result.persisted is False

    def test_a_completed_mcp_record_finalizes_to_executed(self, monkeypatch):
        draft_table, _ = _install(monkeypatch)
        executing = _executing_draft("recon-found")
        run(preorder_draft_store.insert(_draft(draft_id="recon-found"), conversation_id="c"))
        run(
            preorder_draft_store.compare_and_swap(
                "recon-found", expected_version=1, new_draft=executing
            )
        )

        key = execution_key(executing)
        run(mcp_idempotency_store.claim(key, svc.PREORDER_MCP_TOOL_NAME, "hashX"))
        run(
            mcp_idempotency_store.complete(
                key, svc.PREORDER_MCP_TOOL_NAME, {"status": "success", "order_id": "order-1"}
            )
        )

        result = run(svc.reconcile_executing_draft(executing))
        assert result.outcome is svc.ReconciliationOutcome.EXTERNAL_EFFECT_FOUND
        assert result.final_status == PreorderDraftStatus.EXECUTED.value

    def test_no_mcp_record_is_ambiguous_never_a_guess(self, monkeypatch):
        _install(monkeypatch)
        executing = _executing_draft("recon-none")
        run(preorder_draft_store.insert(_draft(draft_id="recon-none"), conversation_id="c"))
        run(
            preorder_draft_store.compare_and_swap(
                "recon-none", expected_version=1, new_draft=executing
            )
        )

        result = run(svc.reconcile_executing_draft(executing))
        assert result.outcome is svc.ReconciliationOutcome.AMBIGUOUS
        assert result.final_status == PreorderDraftStatus.EXECUTION_UNKNOWN.value

    def test_reconciliation_never_calls_confirm_preorder_draft_or_any_mcp_write(self):
        """Preuve structurelle (mandat §13) : ce module ne réécrit JAMAIS
        l'effet externe lui-même."""
        import inspect
        import re

        source = inspect.getsource(svc)
        code_only = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
        for forbidden in ("PreorderGateway", "EscrowGateway(", "MCPToolProvider", "AgriMCPClient"):
            assert forbidden not in code_only


class TestReconciliationConcurrency:
    def test_two_concurrent_reconciliations_on_the_same_draft_yield_exactly_one_finalization(
        self, monkeypatch
    ):
        _install(monkeypatch)
        executing = _executing_draft("recon-race")
        run(preorder_draft_store.insert(_draft(draft_id="recon-race"), conversation_id="c"))
        run(
            preorder_draft_store.compare_and_swap(
                "recon-race", expected_version=1, new_draft=executing
            )
        )
        key = execution_key(executing)
        run(mcp_idempotency_store.claim(key, svc.PREORDER_MCP_TOOL_NAME, "hashX"))
        run(
            mcp_idempotency_store.complete(
                key, svc.PREORDER_MCP_TOOL_NAME, {"status": "success", "order_id": "order-race"}
            )
        )

        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(5)

        def worker():
            barrier.wait()
            r = run(svc.reconcile_executing_draft(executing))
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        persisted_count = sum(1 for r in results if r.persisted)
        assert persisted_count == 1
        final = run(preorder_draft_store.load("recon-race"))
        assert final.status == PreorderDraftStatus.EXECUTED


class TestReconcileAwaitingPaymentDraft:
    def test_not_awaiting_payment_is_a_safe_no_op(self, monkeypatch):
        _install(monkeypatch)
        draft = _draft(draft_id="d2")  # DRAFT
        result = run(svc.reconcile_awaiting_payment_draft(draft))
        assert result.outcome is svc.ReconciliationOutcome.NOT_APPLICABLE

    def test_no_invoice_token_found_is_ambiguous_never_a_guess(self, monkeypatch):
        _install(monkeypatch)
        monkeypatch.setattr(svc, "_find_invoice_token", lambda order_id: _none_coro())
        draft = _executing_draft("d3").with_status(PreorderDraftStatus.AWAITING_PAYMENT)
        result = run(svc.reconcile_awaiting_payment_draft(draft))
        assert result.outcome is svc.ReconciliationOutcome.AMBIGUOUS
        assert result.persisted is False


async def _none_coro():
    return None
