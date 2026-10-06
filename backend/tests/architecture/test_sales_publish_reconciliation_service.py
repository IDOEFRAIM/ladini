"""`SalesPublishReconciliationService` (2026-09-04, migration SALES) — sur
le modèle de `test_procurement_reconciliation_service.py`."""
from __future__ import annotations

import threading

from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
    SalesPublishDraft,
    SalesPublishDraftStatus,
    execution_key,
)
from ladini.services.database import mcp_idempotency_store, sales_publish_draft_store
from ladini.services.reconciliation import sales_publish_reconciliation_service as svc
from tests.architecture.test_sales_publish_draft_persistence import (
    _draft,
    _install_fake_db,
)
from tests.conftest import run
from tests.unit.test_mcp_idempotency import (
    _install_fake_db as _install_fake_idempotency_db,
)


def _install(monkeypatch):
    draft_table = _install_fake_db(monkeypatch)
    idem_table = _install_fake_idempotency_db(monkeypatch)
    return draft_table, idem_table


def _executing_draft(draft_id: str = "recon1") -> SalesPublishDraft:
    return _draft(draft_id=draft_id)._confirm_to_executing()


class TestReconcileDraft:
    def test_not_executing_is_a_safe_no_op(self, monkeypatch):
        _install(monkeypatch)
        draft = _draft(draft_id="d1")
        result = run(svc.reconcile_draft(draft))
        assert result.outcome is svc.ReconciliationOutcome.NOT_APPLICABLE
        assert result.persisted is False

    def test_a_completed_mcp_record_finalizes_to_published(self, monkeypatch):
        _install(monkeypatch)
        executing = _executing_draft("recon-found")
        run(sales_publish_draft_store.insert(_draft(draft_id="recon-found"), conversation_id="c"))
        run(
            sales_publish_draft_store.compare_and_swap(
                "recon-found", expected_version=1, new_draft=executing
            )
        )
        key = execution_key(executing)
        run(mcp_idempotency_store.claim(key, svc.SALES_PUBLISH_MCP_TOOL_NAME, "hashX"))
        run(
            mcp_idempotency_store.complete(
                key, svc.SALES_PUBLISH_MCP_TOOL_NAME, {"status": "success", "product_id": "p-1"}
            )
        )

        result = run(svc.reconcile_draft(executing))
        assert result.outcome is svc.ReconciliationOutcome.EXTERNAL_EFFECT_FOUND
        assert result.final_status == SalesPublishDraftStatus.PUBLISHED.value

    def test_no_mcp_record_is_ambiguous_never_a_guess(self, monkeypatch):
        _install(monkeypatch)
        executing = _executing_draft("recon-none")
        run(sales_publish_draft_store.insert(_draft(draft_id="recon-none"), conversation_id="c"))
        run(
            sales_publish_draft_store.compare_and_swap(
                "recon-none", expected_version=1, new_draft=executing
            )
        )
        result = run(svc.reconcile_draft(executing))
        assert result.outcome is svc.ReconciliationOutcome.AMBIGUOUS
        assert result.final_status == SalesPublishDraftStatus.EXECUTION_UNKNOWN.value


class TestReconciliationConcurrency:
    def test_two_concurrent_reconciliations_on_the_same_draft_yield_exactly_one_finalization(
        self, monkeypatch
    ):
        _install(monkeypatch)
        executing = _executing_draft("recon-race")
        run(sales_publish_draft_store.insert(_draft(draft_id="recon-race"), conversation_id="c"))
        run(
            sales_publish_draft_store.compare_and_swap(
                "recon-race", expected_version=1, new_draft=executing
            )
        )
        key = execution_key(executing)
        run(mcp_idempotency_store.claim(key, svc.SALES_PUBLISH_MCP_TOOL_NAME, "hashX"))
        run(
            mcp_idempotency_store.complete(
                key, svc.SALES_PUBLISH_MCP_TOOL_NAME, {"status": "success", "product_id": "p-race"}
            )
        )

        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(5)

        def worker():
            barrier.wait()
            r = run(svc.reconcile_draft(executing))
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(1 for r in results if r.persisted) == 1
        final = run(sales_publish_draft_store.load("recon-race"))
        assert final.status == SalesPublishDraftStatus.PUBLISHED
