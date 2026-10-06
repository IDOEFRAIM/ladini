"""`ProcurementReconciliationService` (2026-09-03, phase 1 — recovery des
`ProcurementDraft` bloqués en `EXECUTING`).

Réutilise les deux faux moteurs déjà écrits pour les deux tables réelles
qu'orchestre ce service — `test_procurement_draft_persistence.py` (table
`procurement_drafts`) et `test_mcp_idempotency.py` (table
`mcp_idempotency_records`) — plutôt que d'en réinventer des variantes
locales (mêmes garanties de fidélité SQL, une seule source de vérité pour
la sémantique CAS simulée)."""
from __future__ import annotations

from typing import Optional

import pytest

from tests.conftest import run
from tests.architecture.test_procurement_draft_persistence import (
    _FakeDraftTable,
    _install_fake_db as _install_fake_draft_db,
)
from tests.unit.test_mcp_idempotency import (
    _FakeIdempotencyTable,
    _install_fake_db as _install_fake_idempotency_db,
)

from ladini.graphs.agents.market_coach.domain.procurement_draft import (
    ProcurementDraft,
    ProcurementDraftStatus,
    execution_key,
)
from ladini.services.database import mcp_idempotency_store, procurement_draft_store
from ladini.services.reconciliation import procurement_reconciliation_service as svc


def _executing_draft(draft_id: str = "recon1") -> ProcurementDraft:
    v1 = ProcurementDraft.new(
        draft_id=draft_id, product="tomates", quantity=2000.0, unit="KG",
        price=250.0, price_unit="KG",
    )
    return v1._confirm_to_executing()


def _install(monkeypatch):
    draft_table = _install_fake_draft_db(monkeypatch)
    idem_table = _install_fake_idempotency_db(monkeypatch)
    return draft_table, idem_table


class TestFindStaleExecutingCandidates:
    def test_uses_the_configured_threshold_not_a_hardcoded_one(self, monkeypatch):
        """Mandat §1.2 : pas de timeout métier codé en dur — dérivé de
        `settings.PROCUREMENT_EXECUTING_STALE_SECONDS`."""
        from ladini.core.settings import settings

        draft_table, _ = _install(monkeypatch)
        executing = _executing_draft("cand1")
        run(procurement_draft_store.insert(executing, conversation_id="c"))
        # `insert` écrit un DRAFT v1 — force le statut EXECUTING directement
        # dans le faux magasin pour ce test de seuil (le chemin normal passe
        # par compare_and_swap, testé ailleurs).
        draft_table.force_updated_at("cand1", 0)  # antique

        seen_threshold = {}
        original = procurement_draft_store.find_stale_executing

        async def _spy(*, older_than_seconds):
            seen_threshold["value"] = older_than_seconds
            return await original(older_than_seconds=older_than_seconds)

        monkeypatch.setattr(procurement_draft_store, "find_stale_executing", _spy)
        run(svc.find_stale_executing_candidates())
        assert seen_threshold["value"] == settings.PROCUREMENT_EXECUTING_STALE_SECONDS


class TestReconcileDraftOutcomes:
    def test_not_executing_is_a_safe_no_op(self, monkeypatch):
        _install(monkeypatch)
        draft = ProcurementDraft.new(
            draft_id="d1", product="tomates", quantity=1.0, unit="KG", price=1.0, price_unit="KG"
        )  # status=DRAFT, jamais confirmé
        result = run(svc.reconcile_draft(draft))
        assert result.outcome is svc.ReconciliationOutcome.NOT_EXECUTING
        assert result.persisted is False

    def test_a_completed_mcp_record_is_found_and_finalizes_to_executed(self, monkeypatch):
        draft_table, idem_table = _install(monkeypatch)
        executing = _executing_draft("recon-found")
        run(procurement_draft_store.insert(executing.with_updates(), conversation_id="c"))
        # Insère directement au statut EXECUTING attendu par ce scénario
        # (contourne le cycle normal DRAFT->EXECUTING pour isoler le test).
        draft_table._rows["recon-found"]["status"] = "EXECUTING"
        draft_table._rows["recon-found"]["version"] = executing.version
        draft_table._rows["recon-found"]["payload"] = executing.to_dict()

        key = execution_key(executing)
        run(mcp_idempotency_store.claim(key, svc.PROCUREMENT_MCP_TOOL_NAME, "hashX"))
        run(
            mcp_idempotency_store.complete(
                key, svc.PROCUREMENT_MCP_TOOL_NAME, {"status": "success", "auction_id": "auc-42"}
            )
        )

        result = run(svc.reconcile_draft(executing))
        assert result.outcome is svc.ReconciliationOutcome.EXTERNAL_EFFECT_FOUND
        assert result.final_status == ProcurementDraftStatus.EXECUTED.value
        assert result.persisted is True
        assert result.finalized_draft.status == ProcurementDraftStatus.EXECUTED

    def test_a_failed_mcp_record_is_confirmed_no_effect_and_finalizes_to_failed(
        self, monkeypatch
    ):
        draft_table, idem_table = _install(monkeypatch)
        executing = _executing_draft("recon-failed")
        run(procurement_draft_store.insert(executing, conversation_id="c"))
        draft_table._rows["recon-failed"]["status"] = "EXECUTING"
        draft_table._rows["recon-failed"]["version"] = executing.version
        draft_table._rows["recon-failed"]["payload"] = executing.to_dict()

        key = execution_key(executing)
        run(mcp_idempotency_store.claim(key, svc.PROCUREMENT_MCP_TOOL_NAME, "hashX"))
        run(mcp_idempotency_store.fail(key, svc.PROCUREMENT_MCP_TOOL_NAME, "stock insuffisant"))

        result = run(svc.reconcile_draft(executing))
        assert result.outcome is svc.ReconciliationOutcome.CONFIRMED_NO_EFFECT
        assert result.final_status == ProcurementDraftStatus.FAILED.value

    def test_no_mcp_record_at_all_is_ambiguous_never_a_guess(self, monkeypatch):
        """Limite honnête du module (mandat §1.6) : `peek()` renvoie `None`
        — crash AVANT même l'appel MCP, ou antérieur au câblage — jamais
        confondu avec un échec confirmé."""
        draft_table, _ = _install(monkeypatch)
        executing = _executing_draft("recon-none")
        run(procurement_draft_store.insert(executing, conversation_id="c"))
        draft_table._rows["recon-none"]["status"] = "EXECUTING"
        draft_table._rows["recon-none"]["version"] = executing.version
        draft_table._rows["recon-none"]["payload"] = executing.to_dict()

        result = run(svc.reconcile_draft(executing))
        assert result.outcome is svc.ReconciliationOutcome.AMBIGUOUS
        assert result.final_status == ProcurementDraftStatus.EXECUTION_UNKNOWN.value

    def test_a_pending_mcp_record_is_ambiguous_not_assumed_failed_or_succeeded(
        self, monkeypatch
    ):
        draft_table, idem_table = _install(monkeypatch)
        executing = _executing_draft("recon-pending")
        run(procurement_draft_store.insert(executing, conversation_id="c"))
        draft_table._rows["recon-pending"]["status"] = "EXECUTING"
        draft_table._rows["recon-pending"]["version"] = executing.version
        draft_table._rows["recon-pending"]["payload"] = executing.to_dict()

        key = execution_key(executing)
        run(mcp_idempotency_store.claim(key, svc.PROCUREMENT_MCP_TOOL_NAME, "hashX"))
        # Jamais complété ni failed — PENDING, un crash exactement au
        # milieu de l'appel MCP réel.

        result = run(svc.reconcile_draft(executing))
        assert result.outcome is svc.ReconciliationOutcome.AMBIGUOUS
        assert result.final_status == ProcurementDraftStatus.EXECUTION_UNKNOWN.value

    def test_reconciliation_never_calls_create_auction_or_any_mcp_write(self, monkeypatch):
        """Preuve structurelle du mandat §1.5/§1.6/§3 : aucune écriture MCP
        n'est JAMAIS déclenchée par ce module — vérifié en s'assurant que le
        module n'importe RIEN qui ressemble à un client/exécuteur MCP."""
        import inspect
        import re

        source = inspect.getsource(svc)
        # Strip module/function docstrings (triple-quoted) before searching
        # — le module PARLE de ces noms en prose (ce qu'il NE fait PAS),
        # cette recherche ne doit matcher que du vrai CODE.
        code_only = re.sub(r'"""[\s\S]*?"""', "", source)
        for forbidden in ("mcp_tool_executor", "MCPToolProvider", "call_db", "AgriMCPClient"):
            assert forbidden not in code_only, f"{forbidden} ne doit jamais apparaître dans le CODE ici"


class TestReconciliationIdempotence:
    def test_calling_reconcile_twice_on_the_same_draft_object_yields_the_same_final_state(
        self, monkeypatch
    ):
        """Mandat §1.5 : `reconcile(v3)` répété doit produire EXACTEMENT le
        même résultat métier, jamais une double finalisation."""
        draft_table, idem_table = _install(monkeypatch)
        executing = _executing_draft("recon-twice")
        run(procurement_draft_store.insert(executing, conversation_id="c"))
        draft_table._rows["recon-twice"]["status"] = "EXECUTING"
        draft_table._rows["recon-twice"]["version"] = executing.version
        draft_table._rows["recon-twice"]["payload"] = executing.to_dict()

        key = execution_key(executing)
        run(mcp_idempotency_store.claim(key, svc.PROCUREMENT_MCP_TOOL_NAME, "hashX"))
        run(
            mcp_idempotency_store.complete(
                key, svc.PROCUREMENT_MCP_TOOL_NAME, {"status": "success", "auction_id": "auc-1"}
            )
        )

        first = run(svc.reconcile_draft(executing))
        assert first.persisted is True
        assert first.final_status == ProcurementDraftStatus.EXECUTED.value

        # Rejoue avec le MÊME objet draft en mémoire (simule un worker qui
        # retente sans recharger — le pire cas).
        second = run(svc.reconcile_draft(executing))
        assert second.final_status == ProcurementDraftStatus.EXECUTED.value
        # La ligne persistée reste EXACTEMENT celle du premier passage —
        # aucune double finalisation, aucun changement de version au-delà
        # du premier appel gagnant.
        final_row = run(procurement_draft_store.load("recon-twice"))
        assert final_row.version == first.finalized_draft.version


class TestReconciliationConcurrency:
    def test_two_concurrent_reconciliations_on_the_same_draft_yield_exactly_one_finalization(
        self, monkeypatch
    ):
        """Mandat §5 : deux réconciliateurs simultanés sur le MÊME draft ->
        une seule finalisation persistée. Réutilise le CAS déjà en place
        (`compare_and_swap`), aucun verrou ad hoc — vérifié avec de VRAIS
        threads OS, comme les autres tests de concurrence de ce chantier."""
        import threading

        draft_table, idem_table = _install(monkeypatch)
        executing = _executing_draft("recon-race")
        run(procurement_draft_store.insert(executing, conversation_id="c"))
        draft_table._rows["recon-race"]["status"] = "EXECUTING"
        draft_table._rows["recon-race"]["version"] = executing.version
        draft_table._rows["recon-race"]["payload"] = executing.to_dict()

        key = execution_key(executing)
        run(mcp_idempotency_store.claim(key, svc.PROCUREMENT_MCP_TOOL_NAME, "hashX"))
        run(
            mcp_idempotency_store.complete(
                key, svc.PROCUREMENT_MCP_TOOL_NAME, {"status": "success", "auction_id": "auc-7"}
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

        persisted_count = sum(1 for r in results if r.persisted)
        assert persisted_count == 1, f"exactement une finalisation persistée attendue, obtenu {persisted_count}"
        final_row = run(procurement_draft_store.load("recon-race"))
        assert final_row.status == ProcurementDraftStatus.EXECUTED
