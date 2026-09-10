"""Contrat transactionnel SALES (2026-09-04, migration SALES) — symétrique
à `test_procurement_draft_transactional_contract.py`/
`test_preorder_transactional_contract.py`, adapté à
`SalesPublishDraft` (pas de GPS, pas d'escrow — le gabarit le plus proche
est PROCUREMENT)."""
from __future__ import annotations

import threading

import pytest

from tests.conftest import make_state, run
from tests.architecture.test_sales_publish_draft_persistence import _draft, _install_fake_db

from ladini.graphs.agents.market_coach.core.confirmation_target import ConfirmationTarget
from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
    CancelSalesPublishDraft,
    ConfirmSalesPublishDraft,
    NoSalesPublishAction,
    RejectSalesPublishConfirmation,
    SalesPublishDraft,
    SalesPublishDraftStatus,
    SalesPublishOutcomeKind,
    UpdateSalesPublishDraft,
    apply_domain_action,
    check_confirmation_target_invariant,
    resolve_domain_action,
)
from ladini.graphs.agents.market_coach.flows.producer.sales_confirmation import (
    resolve_sales_confirmation,
)
from ladini.services.database import sales_publish_draft_store as store_mod

_ALWAYS_CLAIM = lambda key: True  # noqa: E731


class TestInvariantUpdateProducesExactlyOneNewVersion:
    def test_update_bumps_version_by_exactly_one(self):
        draft = _draft(draft_id="d1")
        updated = draft.with_updates(price=999.0)
        assert updated.version == draft.version + 1
        assert updated.draft_id == draft.draft_id

    def test_no_op_update_does_not_bump_version(self):
        draft = _draft(draft_id="d1b", price=250.0)
        same = draft.with_updates(price=250.0)
        assert same.version == draft.version
        assert same is draft


class TestInvariantUpdateInvalidatesThePreviousConfirmationTarget:
    def test_a_confirm_targeting_the_pre_update_version_is_rejected(self):
        v1 = _draft(draft_id="d2")
        v2 = v1.with_updates(price=999.0)
        stale_target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
        outcome = apply_domain_action(v2, ConfirmSalesPublishDraft(target=stale_target), claim=_ALWAYS_CLAIM)
        assert outcome.kind == SalesPublishOutcomeKind.STALE_TARGET
        assert outcome.draft.status == SalesPublishDraftStatus.DRAFT


class TestInvariantStaleConfirmNeverExecutes:
    def test_confirm_with_a_stale_target_never_reaches_executing(self):
        v1 = _draft(draft_id="d3")
        v2 = v1.with_updates(price=1.0)
        target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
        outcome = apply_domain_action(v2, ConfirmSalesPublishDraft(target=target), claim=_ALWAYS_CLAIM)
        assert outcome.kind == SalesPublishOutcomeKind.STALE_TARGET
        assert outcome.draft.status != SalesPublishDraftStatus.EXECUTING


class TestInvariantConfirmWithoutTargetNeverExecutes:
    def test_confirm_with_no_target_is_rejected(self):
        draft = _draft(draft_id="d4")
        outcome = apply_domain_action(draft, ConfirmSalesPublishDraft(target=None), claim=_ALWAYS_CLAIM)
        assert outcome.kind == SalesPublishOutcomeKind.NO_TARGET
        assert outcome.draft.status != SalesPublishDraftStatus.EXECUTING


class TestInvariantDoubleConfirmExecutesExactlyOnce:
    def test_a_repeated_confirm_on_an_executing_draft_never_re_executes(self):
        draft = _draft(draft_id="d5")
        target = ConfirmationTarget(draft_id=draft.draft_id, draft_version=draft.version)
        first = apply_domain_action(draft, ConfirmSalesPublishDraft(target=target), claim=_ALWAYS_CLAIM)
        assert first.kind == SalesPublishOutcomeKind.CONFIRMED_READY_FOR_EXECUTION
        executing = first.draft

        retry = apply_domain_action(executing, ConfirmSalesPublishDraft(target=target), claim=_ALWAYS_CLAIM)
        assert retry.kind == SalesPublishOutcomeKind.ALREADY_EXECUTING


class TestInvariantRejectPreservesTheDraftForCorrection:
    def test_reject_does_not_touch_the_draft(self):
        draft = _draft(draft_id="d6")
        outcome = apply_domain_action(draft, RejectSalesPublishConfirmation())
        assert outcome.kind == SalesPublishOutcomeKind.CONFIRMATION_REJECTED
        assert outcome.draft is draft  # inchangé, corrigeable au tour suivant


class TestInvariantCancelIsTerminal:
    def test_cancel_transitions_to_cancelled_and_blocks_further_updates(self):
        draft = _draft(draft_id="d7")
        outcome = apply_domain_action(draft, CancelSalesPublishDraft())
        assert outcome.kind == SalesPublishOutcomeKind.CANCELLED
        assert outcome.draft.status == SalesPublishDraftStatus.CANCELLED

        further = apply_domain_action(outcome.draft, UpdateSalesPublishDraft(fields={"price": 1.0}))
        assert further.kind == SalesPublishOutcomeKind.DRAFT_FINALIZED


class TestInvariantDomainNeverComparesRawText:
    def test_resolve_domain_action_signature_has_no_text_parameter(self):
        import inspect

        sig = inspect.signature(resolve_domain_action)
        for name in sig.parameters:
            assert "text" not in name.lower(), (
                f"resolve_domain_action accepte un paramètre texte ({name}) — "
                "le domaine ne doit connaître que interpreted_event/entities."
            )

    def test_confirm_event_never_derived_from_raw_message_content(self):
        action = resolve_domain_action(
            interpreted_event="CONFIRM", extracted_entities={}, pending_target=None
        )
        assert isinstance(action, ConfirmSalesPublishDraft)
        # Aucun autre événement que "CONFIRM" (déjà interprété EN AMONT) ne
        # peut produire cette action — "oui"/"ok" ne sont jamais lus ici.
        other = resolve_domain_action(
            interpreted_event="UNKNOWN", extracted_entities={}, pending_target=None
        )
        assert not isinstance(other, ConfirmSalesPublishDraft)
        assert isinstance(other, NoSalesPublishAction)


class TestConfirmationTargetInvariantChecker:
    def test_target_matching_the_current_version_is_not_a_violation(self):
        draft = _draft(draft_id="d8")
        target = {"draft_id": draft.draft_id, "draft_version": draft.version}
        assert check_confirmation_target_invariant(target, draft) is None

    def test_target_pointing_a_stale_version_is_a_reported_violation(self):
        draft = _draft(draft_id="d9")
        updated = draft.with_updates(price=1.0)
        target = {"draft_id": draft.draft_id, "draft_version": draft.version}
        violation = check_confirmation_target_invariant(target, updated)
        assert violation is not None

    def test_target_pointing_a_missing_draft_is_a_reported_violation(self):
        target = {"draft_id": "ghost", "draft_version": 1}
        violation = check_confirmation_target_invariant(target, None)
        assert violation is not None


class TestNodeLevelConfirmAuthorizesExecution:
    def test_the_node_resolves_pending_interaction_on_confirm(self, monkeypatch):
        import ladini.graphs.agents.market_coach.domain.sales_publish_draft as sd_mod
        from ladini.graphs.agents.market_coach.core.pending_interaction import (
            InteractionKind,
            get_pending_interaction,
        )

        monkeypatch.setattr(sd_mod, "claim_once", _ALWAYS_CLAIM)
        _install_fake_db(monkeypatch)
        v1 = _draft(draft_id="node1")
        run(store_mod.insert(v1, conversation_id="c"))

        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            sales_publish_draft=v1.to_dict(),
            interpreted_event="CONFIRM",
            extracted_entities={},
        )
        state["pending_interaction"] = {
            "kind": "CONFIRM_ACTION",
            "target": {"draft_id": v1.draft_id, "draft_version": v1.version},
        }
        patch = run(resolve_sales_confirmation(state, None))
        merged = dict(state)
        merged.update(patch)
        assert patch["status"] == "EXECUTING"
        assert get_pending_interaction(merged).kind == InteractionKind.NONE
        assert patch["execution_authorized"] is True
        assert patch["transaction_payload"] == v1.execution_payload()


class TestRealThreadConcurrencyUpdateAndConfirm:
    def test_real_threads_racing_update_and_confirm_yield_at_most_one_execution(self, monkeypatch):
        """UPDATE + CONFIRM concurrents (mandat §19) — soit UPDATE gagne
        (CONFIRM retombe alors STALE_TARGET), soit CONFIRM gagne (UPDATE
        retombe alors DRAFT_FINALIZED, le draft n'étant plus DRAFT) —
        JAMAIS les deux ne réussissent, jamais une double exécution."""
        import ladini.graphs.agents.market_coach.domain.sales_publish_draft as sd_mod

        monkeypatch.setattr(sd_mod, "claim_once", _ALWAYS_CLAIM)
        _install_fake_db(monkeypatch)
        v1 = _draft(draft_id="race1")
        run(store_mod.insert(v1, conversation_id="c"))

        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def update_worker():
            state = make_state(
                current_goal="SALES_PUBLISH_PRODUCT",
                sales_publish_draft=v1.to_dict(),
                interpreted_event="UPDATE",
                extracted_entities={"price": 999.0},
            )
            state["pending_interaction"] = {
                "kind": "CONFIRM_ACTION",
                "target": {"draft_id": v1.draft_id, "draft_version": v1.version},
            }
            barrier.wait()
            patch = run(resolve_sales_confirmation(state, None))
            with results_lock:
                results.append(("update", patch.get("status")))

        def confirm_worker():
            state = make_state(
                current_goal="SALES_PUBLISH_PRODUCT",
                sales_publish_draft=v1.to_dict(),
                interpreted_event="CONFIRM",
                extracted_entities={},
            )
            state["pending_interaction"] = {
                "kind": "CONFIRM_ACTION",
                "target": {"draft_id": v1.draft_id, "draft_version": v1.version},
            }
            barrier.wait()
            patch = run(resolve_sales_confirmation(state, None))
            with results_lock:
                results.append(("confirm", patch.get("status")))

        threads = [threading.Thread(target=update_worker), threading.Thread(target=confirm_worker)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        executing_count = sum(1 for _, status in results if status == "EXECUTING")
        assert executing_count <= 1, f"au plus une exécution attendue, obtenu {results}"
        final = run(store_mod.load("race1"))
        assert final is not None
        assert final.status in (SalesPublishDraftStatus.DRAFT, SalesPublishDraftStatus.EXECUTING)
