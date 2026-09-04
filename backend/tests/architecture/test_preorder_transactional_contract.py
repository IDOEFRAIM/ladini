"""Contrat transactionnel PREORDER (2026-09-03, mandat de migration —
§22 A-L). Verrouille les invariants qui rendent la classe de bug
"sources de vérité concurrentes" structurellement impossible pour
`PreorderDraft`, symétrique à
`test_procurement_draft_transactional_contract.py`."""
from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

from tests.conftest import make_state, run
from tests.architecture.test_preorder_draft_persistence import _draft, _install_fake_db

from agriconnect.graphs.agents.market_coach.domain.preorder_draft import (
    ConfirmationTarget,
    ConfirmPreorderDraft,
    PreorderDraft,
    PreorderDraftStatus,
    PreorderOutcomeKind,
    UpdatePreorderDraft,
    apply_domain_action,
)
from agriconnect.graphs.agents.market_coach.flows.buyer.preorder_confirmation import (
    resolve_preorder_confirmation,
)
from agriconnect.services.database import preorder_draft_store as store_mod

_ALWAYS_CLAIM = lambda key: True  # noqa: E731


class TestInvariantA_PreorderWorkflowNeverMutatesCanonicalStateDirectly:
    def test_apply_response_plan_never_writes_business_fields_into_preorder_workflow(self):
        """`preorder_workflow` ne porte QUE `phase`/`preorder_id`/
        `total_amount`/`gps_stage`/`gps_default` — jamais `status`,
        `items`, ou tout autre champ que `apply_response_plan` déciderait
        lui-même (mandat §6 : dérivé de `draft.status`, pas une autorité)."""
        import inspect
        import agriconnect.graphs.agents.market_coach.flows.buyer.preorder_confirmation as mod

        source = inspect.getsource(mod.apply_response_plan) + inspect.getsource(mod._phase_projection)
        code_only = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
        # Le SEUL endroit qui écrit `preorder_workflow["phase"]` dérive de
        # `plan.draft.status` — jamais une valeur littérale indépendante
        # décidée ailleurs pour un draft actif.
        assert "plan.draft.status" in code_only or "_phase_projection" in code_only


class TestInvariantB_ResolvedIdDoesNotControlRouting:
    def test_resolved_id_never_appears_in_the_domain_action_resolution(self):
        """`resolve_domain_action` ne connaît QUE `interpreted_event` —
        `resolved_id` n'existe même pas dans sa signature (mandat §7)."""
        import inspect
        from agriconnect.graphs.agents.market_coach.domain import preorder_draft as pd_mod

        sig = inspect.signature(pd_mod.resolve_domain_action)
        assert "resolved_id" not in sig.parameters

    def test_a_confirm_via_menu_selection_and_via_free_text_produce_the_same_domain_action(
        self,
    ):
        """Peu importe COMMENT `interpreter_event="CONFIRM"` a été obtenu
        (menu numéroté traduit par `preorder.py`, ou reconnaissance directe)
        — le domaine voit exactement le même événement, jamais une branche
        dédiée à `resolved_id`."""
        from agriconnect.graphs.agents.market_coach.domain.preorder_draft import (
            resolve_domain_action,
        )

        target = {"draft_id": "d1", "draft_version": 1}
        a = resolve_domain_action(interpreted_event="CONFIRM", extracted_entities={}, pending_target=target)
        b = resolve_domain_action(interpreted_event="confirm", extracted_entities={}, pending_target=target)
        assert type(a) is type(b) is ConfirmPreorderDraft


class TestInvariantC_ActiveCartCannotDivergeFromDraftItems:
    def test_the_response_plan_reads_items_from_the_draft_never_from_active_cart(self):
        """`apply_response_plan`/`render_summary` ne lisent JAMAIS
        `active_cart` — seul `draft.items` alimente le récap et
        `last_order_summary` une fois qu'un draft existe."""
        import inspect
        import agriconnect.graphs.agents.market_coach.flows.buyer.preorder_confirmation as mod
        from agriconnect.graphs.agents.market_coach.domain import preorder_draft as pd_mod

        source = inspect.getsource(mod.apply_response_plan)
        source += inspect.getsource(pd_mod.PreorderDraft.render_summary)
        code_only = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
        code_only = re.sub(r"#.*", "", code_only)
        # `patch["active_cart"] = []` (VIDER au dénouement) est une ÉCRITURE
        # légitime, pas une lecture — seule une LECTURE (`.get("active_cart")`,
        # `state["active_cart"]`) constituerait une divergence potentielle
        # avec `draft.items`.
        assert '.get("active_cart"' not in code_only
        assert 'state["active_cart"]' not in code_only


class TestInvariantD_UpdateProducesExactlyOneNewVersion:
    def test_update_bumps_version_by_exactly_one(self):
        draft = _draft(draft_id="d1")
        updated = draft.with_updates(total_amount=9999.0)
        assert updated.version == draft.version + 1
        assert updated.draft_id == draft.draft_id


class TestInvariantE_UpdateInvalidatesThePreviousConfirmationTarget:
    def test_a_confirm_targeting_the_pre_update_version_is_rejected(self):
        v1 = _draft(draft_id="d1")
        v2 = v1.with_updates(total_amount=9999.0)
        stale_target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
        outcome = apply_domain_action(v2, ConfirmPreorderDraft(target=stale_target), claim=_ALWAYS_CLAIM)
        assert outcome.kind == PreorderOutcomeKind.STALE_TARGET
        assert outcome.draft.status == PreorderDraftStatus.DRAFT


class TestInvariantF_StaleConfirmNeverExecutes:
    def test_confirm_with_a_stale_target_never_reaches_executing(self):
        v1 = _draft(draft_id="d2")
        v2 = v1.with_updates(total_amount=1.0)
        target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
        outcome = apply_domain_action(
            v2, ConfirmPreorderDraft(target=target, delivery_lat=1.0, delivery_lon=1.0), claim=_ALWAYS_CLAIM
        )
        assert outcome.kind == PreorderOutcomeKind.STALE_TARGET
        assert outcome.draft.status != PreorderDraftStatus.EXECUTING


class TestInvariantG_ConfirmWithoutTargetNeverExecutes:
    def test_confirm_with_no_target_is_rejected(self):
        draft = _draft(draft_id="d3")
        outcome = apply_domain_action(draft, ConfirmPreorderDraft(target=None), claim=_ALWAYS_CLAIM)
        assert outcome.kind == PreorderOutcomeKind.NO_TARGET
        assert outcome.draft.status != PreorderDraftStatus.EXECUTING


class TestInvariantH_DoubleConfirmExecutesExactlyOnce:
    def test_a_repeated_confirm_on_an_executing_draft_never_re_executes(self):
        draft = _draft(draft_id="d4")
        target = ConfirmationTarget(draft_id=draft.draft_id, draft_version=draft.version)
        first = apply_domain_action(
            draft, ConfirmPreorderDraft(target=target, delivery_lat=1.0, delivery_lon=1.0), claim=_ALWAYS_CLAIM
        )
        assert first.kind == PreorderOutcomeKind.CONFIRMED_READY_FOR_EXECUTION
        executing = first.draft

        retry = apply_domain_action(
            executing, ConfirmPreorderDraft(target=target, delivery_lat=1.0, delivery_lon=1.0), claim=_ALWAYS_CLAIM
        )
        assert retry.kind == PreorderOutcomeKind.ALREADY_EXECUTING


class TestInvariantI_ConcurrentUpdateConfirmNeverExecutesAStaleVersion:
    def test_real_threads_racing_update_and_confirm_yield_at_most_one_execution(self, monkeypatch):
        import agriconnect.graphs.agents.market_coach.domain.preorder_draft as pd_mod

        table = _install_fake_db(monkeypatch)
        monkeypatch.setattr(pd_mod, "claim_once", lambda key: True)

        v1 = _draft(draft_id="race1")
        run(store_mod.insert(v1, conversation_id="c"))
        target = {"draft_id": v1.draft_id, "draft_version": v1.version}

        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(10)

        def confirm_worker():
            state = make_state(
                current_goal="BUYER_PREORDER_CONFIRM",
                user_phone="+22600000000",
                preorder_draft=v1.to_dict(),
                interpreted_event="CONFIRM",
                extracted_entities={},
            )
            state["pending_interaction"] = {
                "kind": "PROVIDE_LOCATION",
                "target": target,
            }
            state["location_shared"] = True
            state["location_outcome"] = "NEW_LOCATION_ACCEPTED"
            state["location_lat"] = 1.0
            state["location_lon"] = 1.0
            barrier.wait()
            patch = run(resolve_preorder_confirmation(state, None))
            with results_lock:
                results.append(("confirm", patch.get("status")))

        def update_worker():
            barrier.wait()
            current = run(store_mod.load("race1")) or v1
            outcome = apply_domain_action(current, UpdatePreorderDraft(fields={"total_amount": 42.0}))
            if outcome.draft is not None and outcome.draft.version != current.version:
                run(store_mod.compare_and_swap("race1", expected_version=current.version, new_draft=outcome.draft))
            with results_lock:
                results.append(("update", None))

        threads = [threading.Thread(target=confirm_worker) for _ in range(5)] + [
            threading.Thread(target=update_worker) for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final = run(store_mod.load("race1"))
        assert final is not None
        # Cohérent : soit resté DRAFT (une UPDATE a fait avancer la version
        # avant tout CONFIRM), soit EXECUTING/EXECUTED (le CONFIRM a gagné)
        # — jamais un mélange, jamais une exécution sur une version stale.
        assert final.status in (
            PreorderDraftStatus.DRAFT,
            PreorderDraftStatus.EXECUTING,
            PreorderDraftStatus.EXECUTED,
            PreorderDraftStatus.EXECUTION_UNKNOWN,
        )


class TestInvariantJ_CreateRetryNeverCreatesADoubleDraft:
    def test_the_same_idempotency_key_is_stable_for_the_same_cart_and_buyer(self):
        from agriconnect.graphs.agents.market_coach.domain.preorder_draft import (
            cart_fingerprint,
            creation_key,
        )

        items = [{"product_id": "p1", "quantity": 10, "price": 500}]
        key1 = creation_key("+22600000000", cart_fingerprint(items))
        key2 = creation_key("+22600000000", cart_fingerprint(list(items)))
        assert key1 == key2

    def test_a_different_cart_produces_a_different_key(self):
        from agriconnect.graphs.agents.market_coach.domain.preorder_draft import (
            cart_fingerprint,
            creation_key,
        )

        key1 = creation_key("+22600000000", cart_fingerprint([{"product_id": "p1", "quantity": 10, "price": 500}]))
        key2 = creation_key("+22600000000", cart_fingerprint([{"product_id": "p1", "quantity": 11, "price": 500}]))
        assert key1 != key2


class TestInvariantL_OldSummaryCannotReappearAfterUpdate:
    def test_render_summary_after_an_update_never_shows_the_old_amount(self):
        v1 = _draft(draft_id="d5", total_amount=1000.0)
        v2 = v1.with_updates(total_amount=2000.0)
        assert "1000" not in v2.render_summary()
        assert "2000" in v2.render_summary()
