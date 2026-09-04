"""Architectural hardening pass (2026-09-03) — tests qui échouent
IMMÉDIATEMENT si une deuxième autorité de mutation/confirmation/réponse est
réintroduite au-dessus de la refonte transactionnelle `ProcurementDraft`.

Complète (ne remplace pas) test_procurement_draft_transactional_contract.py
— celui-là verrouille le SCÉNARIO et les invariants 1-2-3-4-5-7 ; celui-ci
verrouille l'ARCHITECTURE elle-même : une seule source de vérité, une seule
autorité de mutation, une seule primitive d'idempotence, aucun mélange
domaine/présentation."""
from __future__ import annotations

import inspect
import re
import threading
from pathlib import Path

import pytest

from agriconnect.graphs.agents.market_coach.domain.procurement_draft import (
    ConfirmationTarget,
    ConfirmProcurementDraft,
    IllegalDraftTransition,
    ProcurementDraft,
    ProcurementDraftStatus,
    ProcurementOutcomeKind,
    UpdateProcurementDraft,
    apply_domain_action,
    build_response_plan,
    finalize_after_execution,
    resolve_domain_action,
)
from agriconnect.graphs.agents.market_coach.nodes.confirmation_gate import (
    confirmation_gate,
)
from agriconnect.graphs.agents.market_coach.nodes.memory import memory_update
from tests.conftest import make_state, run, stub_runtime

_ALWAYS_CLAIM = lambda key: True  # noqa: E731


def _draft(**fields) -> ProcurementDraft:
    base = {
        "product": "tomates",
        "quantity": 2000.0,
        "unit": "KG",
        "price": 250.0,
        "price_unit": "KG",
    }
    base.update(fields)
    return ProcurementDraft.new(draft_id="hardening", **base)


# =====================================================================
# TEST A — une mutation de ProcurementDraft ne passe jamais par memory_update
# =====================================================================


class TestA_MemoryUpdateNeverMutatesTheDraft:
    def test_memory_update_never_writes_the_procurement_draft_key(self, stub_runtime):
        draft = _draft()
        state = make_state(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            procurement_draft=draft.to_dict(),
            interpreted_event="UPDATE",
            extracted_entities={"quantity": 1.0, "unit": "TONNE"},
            normalized_text="1 tonne",
        )
        result = run(memory_update(state, stub_runtime()))
        assert "procurement_draft" not in result, (
            "memory_update a écrit procurement_draft — seule "
            "flows/buyer/procurement_confirmation.py::resolve_procurement_confirmation "
            "a le droit de le faire"
        )

    def test_memory_update_never_writes_transaction_payload_once_the_draft_owns_the_goal(
        self, stub_runtime
    ):
        """Root cause exacte de l'incident (quantity_display figé) : DEUX
        écrivains de la même donnée métier. `transaction_payload` doit
        rester totalement absent du patch — pas seulement inchangé en
        valeur — tant que le draft existe pour ce goal."""
        draft = _draft()
        state = make_state(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            procurement_draft=draft.to_dict(),
            transaction_payload={"product": "tomates", "quantity": 2000, "unit": "KG", "price": 250},
            interpreted_event="UPDATE",
            extracted_entities={"quantity": 1.0, "unit": "TONNE"},
            normalized_text="1 tonne",
        )
        result = run(memory_update(state, stub_runtime()))
        assert "transaction_payload" not in result

    def test_no_source_file_writes_procurement_draft_except_the_two_authorized_ones(self):
        """Balayage source : seuls `nodes/confirmation_gate.py` (bootstrap
        v1) et `flows/buyer/procurement_confirmation.py` (toute mutation
        suivante) ont le droit d'assigner `procurement_draft` dans un patch
        d'état. Un 3e écrivain serait une 2e autorité."""
        root = Path("src/agriconnect/graphs/agents/market_coach")
        authorized = {"confirmation_gate.py", "procurement_confirmation.py"}
        offenders = []
        for path in root.rglob("*.py"):
            if path.name in authorized or "test" in path.name:
                continue
            text = path.read_text(encoding="utf-8")
            if re.search(r'["\']procurement_draft["\']\s*:', text):
                offenders.append(str(path))
        assert offenders == [], f"écrivain(s) non autorisé(s) de procurement_draft : {offenders}"


# =====================================================================
# TEST B — un ResponsePlan est construit UNIQUEMENT depuis un DomainOutcome
# =====================================================================


class TestB_ResponsePlanBuiltOnlyFromDomainOutcome:
    def test_build_response_plan_takes_no_state_parameter(self):
        sig = inspect.signature(build_response_plan)
        param_names = set(sig.parameters)
        assert param_names <= {"outcome", "deviation_note"}, (
            f"build_response_plan accepte {param_names} — une fonction de "
            "présentation pure ne doit connaître que l'Outcome et (au plus) "
            "la note LLM déjà calculée, jamais `state`"
        )

    def test_the_same_outcome_always_produces_the_same_plan(self):
        """Pureté : deux appels avec le même outcome donnent le même plan —
        aucune horloge, aucun état global, aucune lecture cachée."""
        draft = _draft()
        outcome = apply_domain_action(
            draft, UpdateProcurementDraft(fields={"quantity": 1.0, "unit": "TONNE"})
        )
        plan1 = build_response_plan(outcome)
        plan2 = build_response_plan(outcome)
        assert plan1 == plan2

    def test_a_renderer_only_transforms_never_decides(self):
        """`apply_response_plan` (flows/buyer/procurement_confirmation.py —
        public depuis que `procurement_execution_finalizer.py` le réutilise
        aussi) ne doit contenir AUCUNE branche sur `ProcurementOutcomeKind`
        — la preuve structurelle qu'il ne réintroduit pas de décision
        métier : il n'importe même pas le type."""
        src = Path(
            "src/agriconnect/graphs/agents/market_coach/flows/buyer/procurement_confirmation.py"
        ).read_text(encoding="utf-8")
        apply_fn = src.split("def apply_response_plan")[1].split("\ndef ")[0]
        # Retire la docstring (mention légitime en prose) avant de chercher
        # un USAGE réel (comparaison de branche) du type — pas sa simple
        # mention documentaire.
        code_only = re.sub(r'""".*?"""', "", apply_fn, flags=re.DOTALL)
        assert "ProcurementOutcomeKind" not in code_only
        assert re.search(r"\bKind\.\w+", code_only) is None


# =====================================================================
# TEST C — un CONFIRM avec target périmée n'atteint jamais l'exécuteur
# =====================================================================


class TestC_StaleTargetNeverReachesTheExecutor:
    def test_stale_target_through_the_full_confirmation_gate_never_sets_executing(self):
        v1 = _draft()
        v2 = v1.with_updates(quantity=1.0)
        state = make_state(current_goal="PROCUREMENT_CREATE_REQUEST", procurement_draft=v2.to_dict())
        state["interpreted_event"] = "CONFIRM"
        state["pending_interaction"] = {
            "kind": "CONFIRM_ACTION",
            "target": {"draft_id": v1.draft_id, "draft_version": v1.version},  # PÉRIMÉE
        }
        result = run(confirmation_gate(state, None))
        assert result.get("status") != "EXECUTING"
        assert result.get("execution_authorized") is not True
        assert "is_certified" not in result or result["is_certified"] is not True

    def test_stale_target_re_targets_the_current_version_not_a_mix(self):
        v1 = _draft()
        v2 = v1.with_updates(quantity=1.0)
        state = make_state(current_goal="PROCUREMENT_CREATE_REQUEST", procurement_draft=v2.to_dict())
        state["interpreted_event"] = "CONFIRM"
        state["pending_interaction"] = {
            "kind": "CONFIRM_ACTION",
            "target": {"draft_id": v1.draft_id, "draft_version": v1.version},
        }
        result = run(confirmation_gate(state, None))
        new_target = result["pending_interaction"]["target"]
        assert new_target == {"draft_id": v2.draft_id, "draft_version": v2.version}


# =====================================================================
# TEST D — deux workers concurrents ne produisent jamais deux effets métier
# =====================================================================


class TestD_ConcurrentWorkersProduceExactlyOneEffect:
    def test_ten_concurrent_confirmation_gate_calls_yield_exactly_one_execution(
        self, monkeypatch
    ):
        """Même test que TestConcurrentConfirmClaim mais au niveau du NŒUD
        complet (confirmation_gate), pas seulement apply_domain_action —
        preuve que rien entre les deux n'introduit une 2e fenêtre de
        course."""
        import agriconnect.graphs.agents.market_coach.domain.procurement_draft as pd_mod

        claimed_keys: set = set()
        lock = threading.Lock()

        def thread_safe_claim(key: str) -> bool:
            with lock:
                if key in claimed_keys:
                    return False
                claimed_keys.add(key)
                return True

        monkeypatch.setattr(pd_mod, "claim_once", thread_safe_claim)

        draft = _draft()
        base_state = make_state(current_goal="PROCUREMENT_CREATE_REQUEST", procurement_draft=draft.to_dict())
        base_state["interpreted_event"] = "CONFIRM"
        base_state["pending_interaction"] = {
            "kind": "CONFIRM_ACTION",
            "target": {"draft_id": draft.draft_id, "draft_version": draft.version},
        }

        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            result = run(confirmation_gate(dict(base_state), None))
            with results_lock:
                results.append(result.get("status"))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        executing = [r for r in results if r == "EXECUTING"]
        assert len(executing) == 1, f"exactement 1 EXECUTING attendu, obtenu {len(executing)}"


# =====================================================================
# TEST E — un crash après claim ne produit pas un état silencieusement
# incohérent
# =====================================================================


class TestE_CrashAfterClaimLeavesAnHonestState:
    def test_executing_but_not_yet_finalized_is_a_distinct_observable_state(self):
        """Simule : claim gagné, draft transite en EXECUTING en un seul
        appel (DRAFT->CONFIRMED->EXECUTING, mandat §11) — puis le worker
        meurt AVANT que `mcp_tool_executor` (et donc `finalize_after_
        execution`) n'ait pu s'exécuter. Le système ne doit PAS prétendre
        que l'opération est `EXECUTED` : `draft.status` reste `EXECUTING`,
        un état honnêtement "en cours", jamais faussement terminal."""
        v1 = _draft()
        target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
        outcome = apply_domain_action(
            v1, ConfirmProcurementDraft(target=target), claim=_ALWAYS_CLAIM
        )
        assert outcome.kind == ProcurementOutcomeKind.CONFIRMED_READY_FOR_EXECUTION
        executing_draft = outcome.draft
        assert executing_draft.status == ProcurementDraftStatus.EXECUTING
        assert executing_draft.status != ProcurementDraftStatus.EXECUTED, (
            "un crash simulé avant finalisation ne doit jamais être "
            "confondu avec un succès"
        )

    def test_a_retry_after_a_simulated_crash_does_not_re_execute(self):
        """Le worker relance le tour (retry Celery) après le crash — le
        draft est toujours EXECUTING (jamais réinitialisé) : le retry
        obtient `ALREADY_EXECUTING`, ne re-déclenche JAMAIS
        `CONFIRMED_READY_FOR_EXECUTION` — qu'un autre worker soit
        réellement en train de traiter la version ou que ce soit un crash
        passé, la réponse sûre est identique (mandat §12)."""
        v1 = _draft()
        target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
        first = apply_domain_action(v1, ConfirmProcurementDraft(target=target), claim=_ALWAYS_CLAIM)
        executing_draft = first.draft  # état "après crash" : toujours EXECUTING

        retry = apply_domain_action(
            executing_draft, ConfirmProcurementDraft(target=target), claim=_ALWAYS_CLAIM
        )
        assert retry.kind == ProcurementOutcomeKind.ALREADY_EXECUTING

    def test_finalize_after_execution_can_only_be_called_once(self):
        from agriconnect.graphs.agents.market_coach.domain.procurement_draft import (
            ProcurementExecutionResult,
        )

        v1 = _draft()
        target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
        executing = apply_domain_action(
            v1, ConfirmProcurementDraft(target=target), claim=_ALWAYS_CLAIM
        ).draft

        executed = finalize_after_execution(
            executing, ProcurementExecutionResult(success=True, external_id="auc-1")
        )
        assert executed.status == ProcurementDraftStatus.EXECUTED

        with pytest.raises(IllegalDraftTransition):
            finalize_after_execution(executed, ProcurementExecutionResult(success=True))

    def test_a_failed_execution_transitions_to_failed_not_a_silent_confirmed(self):
        from agriconnect.graphs.agents.market_coach.domain.procurement_draft import (
            ProcurementExecutionResult,
        )

        v1 = _draft()
        target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
        executing = apply_domain_action(
            v1, ConfirmProcurementDraft(target=target), claim=_ALWAYS_CLAIM
        ).draft
        failed = finalize_after_execution(
            executing, ProcurementExecutionResult(success=False, error="stock insuffisant")
        )
        assert failed.status == ProcurementDraftStatus.FAILED

    def test_an_ambiguous_mcp_outcome_transitions_to_execution_unknown_not_a_guess(self):
        """Le crash le plus dangereux : l'appel MCP a peut-être réussi
        (auction créée côté serveur) mais le worker meurt avant de le
        savoir. `adapt_mcp_result` ne devine JAMAIS — tout ce qui n'est ni
        clairement COMPLETED ni clairement ERROR devient EXECUTION_UNKNOWN,
        jamais un faux EXECUTED ni un faux FAILED."""
        from agriconnect.graphs.agents.market_coach.domain.procurement_draft import (
            adapt_mcp_result,
        )

        v1 = _draft()
        target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
        executing = apply_domain_action(
            v1, ConfirmProcurementDraft(target=target), claim=_ALWAYS_CLAIM
        ).draft

        # `execution_status`/`execution_result` inexistants — exactement ce
        # qu'on observe si mcp_tool_executor n'a jamais rendu la main.
        ambiguous_result = adapt_mcp_result(execution_status=None, execution_result=None)
        assert ambiguous_result.ambiguous is True

        finalized = finalize_after_execution(executing, ambiguous_result)
        assert finalized.status == ProcurementDraftStatus.EXECUTION_UNKNOWN

        # Un CONFIRM ultérieur (retry, ou l'utilisateur qui retape "oui")
        # exige une réconciliation — ne relance JAMAIS create_auction.
        reconciliation = apply_domain_action(
            finalized, ConfirmProcurementDraft(target=target), claim=_ALWAYS_CLAIM
        )
        assert reconciliation.kind == ProcurementOutcomeKind.RECONCILIATION_REQUIRED


# =====================================================================
# TEST F — transaction_payload ne peut pas diverger de ProcurementDraft
# =====================================================================


class TestF_TransactionPayloadCannotDivergeFromTheDraft:
    def test_transaction_payload_is_always_exactly_the_confirmed_draft_execution_payload(
        self,
    ):
        draft = _draft(quantity=1125.0, unit="KG")
        target = ConfirmationTarget(draft_id=draft.draft_id, draft_version=draft.version)
        state = make_state(current_goal="PROCUREMENT_CREATE_REQUEST", procurement_draft=draft.to_dict())
        state["interpreted_event"] = "CONFIRM"
        state["pending_interaction"] = {"kind": "CONFIRM_ACTION", "target": target.to_dict()}
        import agriconnect.graphs.agents.market_coach.domain.procurement_draft as pd_mod

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(pd_mod, "claim_once", _ALWAYS_CLAIM)
            result = run(confirmation_gate(state, None))
        assert result["transaction_payload"] == draft.execution_payload()

    def test_no_other_site_assigns_transaction_payload_for_this_goal_outside_execution_payload(
        self,
    ):
        """Balayage source : dans `procurement_confirmation.py`, la SEULE
        affectation de `transaction_payload` doit venir de
        `draft.execution_payload()`."""
        src = Path(
            "src/agriconnect/graphs/agents/market_coach/flows/buyer/procurement_confirmation.py"
        ).read_text(encoding="utf-8")
        assignments = re.findall(r'patch\["transaction_payload"\]\s*=\s*(.+)', src)
        assert assignments == ["plan.draft.execution_payload()"], assignments


# =====================================================================
# TEST G — une réponse précédente ne peut pas être réutilisée après UPDATE
# =====================================================================


class TestG_NoPreviousResponseCanBeReused:
    def test_v1_and_v2_produce_different_response_text_and_v1s_text_never_recurs(self):
        v1 = _draft(quantity=2000.0, unit="KG")
        outcome1 = apply_domain_action(v1, UpdateProcurementDraft(fields={"quantity": 2250.0}))
        plan1 = build_response_plan(outcome1)

        outcome2 = apply_domain_action(
            outcome1.draft, UpdateProcurementDraft(fields={"quantity": 1125.0})
        )
        plan2 = build_response_plan(outcome2)

        assert plan1.final_response != plan2.final_response
        assert "2250" in plan1.final_response
        assert "2250" not in plan2.final_response
        assert "1125" in plan2.final_response

    def test_no_stored_summary_field_exists_on_the_draft_itself(self):
        """Preuve structurelle (mandat §8/§10) : `ProcurementDraft` n'a
        aucun champ `confirmation_summary`/`final_response` mémorisé — le
        texte est TOUJOURS recalculé par `render_summary()`, jamais relu."""
        field_names = {f for f in ProcurementDraft.__dataclass_fields__}
        assert "confirmation_summary" not in field_names
        assert "final_response" not in field_names
        assert "quantity_display" not in field_names
        assert "unit_display" not in field_names


# =====================================================================
# MACHINE D'ÉTAT — transitions illégales bloquées, pas seulement journalisées
# (mandat §6/§7)
# =====================================================================


class TestStateMachineTransitionsAreEnforced:
    def test_updating_a_confirmed_draft_is_refused_not_silently_applied(self):
        v1 = _draft()
        confirmed = v1.with_status(ProcurementDraftStatus.CONFIRMED)
        with pytest.raises(IllegalDraftTransition):
            confirmed.with_updates(quantity=1.0)

    def test_confirming_an_already_cancelled_draft_is_refused(self):
        v1 = _draft()
        cancelled = v1.with_status(ProcurementDraftStatus.CANCELLED)
        with pytest.raises(IllegalDraftTransition):
            cancelled.with_status(ProcurementDraftStatus.CONFIRMED)

    def test_apply_domain_action_never_lets_an_illegal_transition_escape_as_an_exception(self):
        """`apply_domain_action` intercepte `IllegalDraftTransition` et la
        traduit en `ProcurementOutcome` — un nœud du graphe ne doit JAMAIS
        avoir à attraper cette exception lui-même."""
        v1 = _draft()
        confirmed = v1.with_status(ProcurementDraftStatus.CONFIRMED)
        action = UpdateProcurementDraft(fields={"quantity": 1.0})
        outcome = apply_domain_action(confirmed, action)  # ne lève pas
        assert outcome.kind == ProcurementOutcomeKind.DRAFT_FINALIZED
        assert outcome.draft.status == ProcurementDraftStatus.CONFIRMED  # inchangé

    def test_update_message_after_confirmation_gets_an_honest_finalized_message(self):
        v1 = _draft()
        confirmed = v1.with_status(ProcurementDraftStatus.CONFIRMED)
        state = make_state(current_goal="PROCUREMENT_CREATE_REQUEST", procurement_draft=confirmed.to_dict())
        state["interpreted_event"] = "UPDATE"
        state["extracted_entities"] = {"quantity": 1.0, "unit": "TONNE"}
        result = run(confirmation_gate(state, None))
        assert result.get("status") != "EXECUTING"
        assert "déjà" in result["final_response"].lower()


# =====================================================================
# IDEMPOTENCE PRIMITIVE UNIQUE (mandat §5)
# =====================================================================


class TestSingleIdempotencyPrimitive:
    def test_procurement_confirm_and_response_dispatch_share_the_same_underlying_claim(self):
        """`domain/procurement_draft.py::claim_once` et
        `api/response_dispatch.py::claim_response_item` doivent tous deux
        déléguer à `core/idempotency.py::claim_once` — même fonction Python,
        pas deux implémentations qui se ressemblent."""
        import agriconnect.api.response_dispatch as dispatch_mod
        import agriconnect.core.idempotency as idempotency_mod
        import agriconnect.graphs.agents.market_coach.domain.procurement_draft as pd_mod

        assert pd_mod.claim_once is idempotency_mod.claim_once
        assert not hasattr(dispatch_mod, "_redis_client"), (
            "response_dispatch.py garde encore son propre client Redis — "
            "la consolidation n'est pas complète"
        )
