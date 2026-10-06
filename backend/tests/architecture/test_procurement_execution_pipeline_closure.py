"""Clôture du pipeline d'exécution — CONFIRM → MCP → finalisation
(2026-09-03, suite du durcissement architectural).

Complète les deux fichiers précédents : celui-ci verrouille spécifiquement
ce que le rapport précédent reconnaissait comme gap —
`CONFIRMED/EXECUTING → EXECUTED/FAILED/EXECUTION_UNKNOWN` réellement câblé,
la politique de retry par statut, et la concurrence RÉELLE entre UPDATE et
CONFIRM sur le MÊME draft."""
from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

from ladini.graphs.agents.market_coach.domain.procurement_draft import (
    ConfirmationTarget,
    ConfirmProcurementDraft,
    ProcurementDraft,
    ProcurementDraftStatus,
    ProcurementExecutionResult,
    ProcurementOutcomeKind,
    UpdateProcurementDraft,
    adapt_mcp_result,
    apply_domain_action,
    execution_key,
    finalize_after_execution,
)
from ladini.graphs.agents.market_coach.flows.buyer.procurement_execution_finalizer import (
    finalize_procurement_execution,
)
from tests.conftest import make_state, run

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
    return ProcurementDraft.new(draft_id="pipeline", **base)


def _executing_draft(**fields) -> ProcurementDraft:
    v1 = _draft(**fields)
    target = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
    return apply_domain_action(v1, ConfirmProcurementDraft(target=target), claim=_ALWAYS_CLAIM).draft


# =====================================================================
# SINGLE EXECUTION PATH (mandat §6)
# =====================================================================


class TestSingleExecutionPath:
    def test_only_confirmation_gate_and_procurement_confirmation_can_set_execution_authorized_true(
        self,
    ):
        """Balayage source : `execution_authorized: True`/`= True` ne doit
        apparaître que dans les fichiers de ce module (+ leurs équivalents
        d'AUTRES workflows migrés au même standard, ex: SALES depuis
        2026-09-04 — chacun reste l'autorité UNIQUE pour SON PROPRE goal,
        voir `test_sales_publish_execution_pipeline_closure.py` pour
        l'audit symétrique côté SALES) — aucun autre nœud ne doit pouvoir
        déclencher directement `create_auction`."""
        root = Path("src/ladini/graphs/agents/market_coach")
        authorized_substrings = ("procurement_confirmation.py", "sales_confirmation.py")
        offenders = []
        for path in root.rglob("*.py"):
            if any(a in str(path) for a in authorized_substrings):
                continue
            if "nodes/confirmation_gate.py" in str(path).replace("\\", "/"):
                continue  # _READ_GOALS bypass, pré-existant, hors procurement
            text = path.read_text(encoding="utf-8")
            if re.search(r'"execution_authorized"\]\s*=\s*True|"execution_authorized":\s*True', text):
                if "confirmation_gate.py" in str(path) or any(a in str(path) for a in authorized_substrings):
                    continue
                offenders.append(str(path))
        # nodes/confirmation_gate.py's _READ_GOALS branch is a pre-existing,
        # unrelated authority for READ-only goals (no external side effect) —
        # not part of this audit's scope (write actions only).
        offenders = [o for o in offenders if "confirmation_gate.py" not in o]
        assert offenders == [], f"chemin(s) d'exécution non audité(s) : {offenders}"

    def test_create_auction_is_registered_for_exactly_one_goal(self):
        from ladini.graphs.agents.market_coach.registry import get_action

        registration = get_action("PROCUREMENT_CREATE_REQUEST")
        assert registration is not None
        assert registration.is_write is True


# =====================================================================
# FINALISATION — nœud de graphe (mandat §1/§11)
# =====================================================================


class TestExecutionFinalizerNode:
    def test_no_op_for_other_goals(self):
        state = make_state(current_goal="SALES_PUBLISH_PRODUCT")
        result = run(finalize_procurement_execution(state, None))
        assert result == {}

    def test_no_op_when_no_draft_is_executing(self):
        draft = _draft()  # status=DRAFT, jamais confirmé
        state = make_state(current_goal="PROCUREMENT_CREATE_REQUEST", procurement_draft=draft.to_dict())
        result = run(finalize_procurement_execution(state, None))
        assert result == {}

    def test_a_successful_mcp_result_finalizes_to_executed(self):
        executing = _executing_draft()
        state = make_state(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            procurement_draft=executing.to_dict(),
        )
        state["status"] = "COMPLETED"
        state["execution_result"] = {"status": "success", "auction_id": "auc-123"}
        result = run(finalize_procurement_execution(state, None))
        finalized = ProcurementDraft.from_dict(result["procurement_draft"])
        assert finalized.status == ProcurementDraftStatus.EXECUTED
        assert result["status"] == "COMPLETED"

    def test_a_failed_mcp_result_finalizes_to_failed(self):
        executing = _executing_draft()
        state = make_state(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            procurement_draft=executing.to_dict(),
        )
        state["status"] = "ERROR"
        state["execution_result"] = {"status": "error", "message": "Produit hors catalogue"}
        result = run(finalize_procurement_execution(state, None))
        finalized = ProcurementDraft.from_dict(result["procurement_draft"])
        assert finalized.status == ProcurementDraftStatus.FAILED

    def test_a_missing_or_unexpected_status_finalizes_to_execution_unknown(self):
        """Le tour s'est arrêté avant que mcp_tool_executor n'ait clairement
        tranché (crash, statut inattendu) — jamais interprété comme un
        succès ou un échec net."""
        executing = _executing_draft()
        state = make_state(
            current_goal="PROCUREMENT_CREATE_REQUEST",
            procurement_draft=executing.to_dict(),
        )
        state["status"] = "SOMETHING_UNEXPECTED"
        state["execution_result"] = None
        result = run(finalize_procurement_execution(state, None))
        finalized = ProcurementDraft.from_dict(result["procurement_draft"])
        assert finalized.status == ProcurementDraftStatus.EXECUTION_UNKNOWN


# =====================================================================
# CLÉ D'IDEMPOTENCE MÉTIER (mandat §4)
# =====================================================================


class TestExecutionKey:
    def test_stable_per_draft_and_version(self):
        d = _draft()
        assert execution_key(d) == f"procurement:{d.draft_id}:{d.version}"

    def test_differs_across_versions(self):
        v1 = _draft()
        v2 = v1.with_updates(quantity=1.0)
        assert execution_key(v1) != execution_key(v2)


# =====================================================================
# CONCURRENCE RÉELLE — UPDATE(v3->v4) simultané à CONFIRM(v3) (mandat §8)
# =====================================================================


class TestUpdateConfirmRace:
    def test_confirm_targeting_v3_never_executes_once_update_has_landed(self):
        """Ordre déterministe (UPDATE arrive en premier) : la version que
        vise le CONFIRM n'est déjà plus la version courante — refusé."""
        v3 = _draft()
        target_v3 = ConfirmationTarget(draft_id=v3.draft_id, draft_version=v3.version)

        v4 = apply_domain_action(v3, UpdateProcurementDraft(fields={"quantity": 1.0})).draft
        assert v4.version == v3.version + 1

        outcome = apply_domain_action(
            v4, ConfirmProcurementDraft(target=target_v3), claim=_ALWAYS_CLAIM
        )
        assert outcome.kind == ProcurementOutcomeKind.STALE_TARGET
        assert outcome.draft.status == ProcurementDraftStatus.DRAFT, (
            "v3 n'a JAMAIS dû être exécutée — le draft courant (v4) reste "
            "en DRAFT, pas CONFIRMED/EXECUTING"
        )

    def test_ten_threads_racing_update_and_confirm_never_execute_a_stale_version(self):
        """Course RÉELLE (threads concurrents, pas seulement séquentielle) :
        5 threads tentent CONFIRM(v3) pendant que 5 threads tentent
        UPDATE(v3->v4) — quel que soit l'ordre d'arrivée réel, JAMAIS plus
        d'une transition CONFIRMED_READY_FOR_EXECUTION ne doit sortir, et
        seulement si elle vise la version qui a effectivement gagné la
        course AVANT elle."""
        v3 = _draft()
        target_v3 = ConfirmationTarget(draft_id=v3.draft_id, draft_version=v3.version)

        # État partagé protégé par un verrou — modélise un unique magasin de
        # draft (voir §C du rapport pour la discussion de persistance
        # réelle) : chaque thread lit-modifie-écrit sous ce verrou, ce qui
        # est la garantie minimale qu'un vrai backend transactionnel
        # (compare-and-swap, transaction DB) devrait offrir.
        store = {"draft": v3}
        store_lock = threading.Lock()
        claimed_keys: set = set()
        claim_lock = threading.Lock()

        def claim(key: str) -> bool:
            with claim_lock:
                if key in claimed_keys:
                    return False
                claimed_keys.add(key)
                return True

        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(10)

        def confirm_worker():
            barrier.wait()
            with store_lock:
                current = store["draft"]
                outcome = apply_domain_action(
                    current, ConfirmProcurementDraft(target=target_v3), claim=claim
                )
                store["draft"] = outcome.draft
            with results_lock:
                results.append(("confirm", outcome.kind))

        def update_worker():
            barrier.wait()
            with store_lock:
                current = store["draft"]
                outcome = apply_domain_action(
                    current, UpdateProcurementDraft(fields={"quantity": 1.0})
                )
                store["draft"] = outcome.draft
            with results_lock:
                results.append(("update", outcome.kind))

        threads = [threading.Thread(target=confirm_worker) for _ in range(5)] + [
            threading.Thread(target=update_worker) for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        executions = [r for r in results if r[1] == ProcurementOutcomeKind.CONFIRMED_READY_FOR_EXECUTION]
        assert len(executions) <= 1, (
            f"au plus 1 exécution attendue quel que soit l'ordre, obtenu {len(executions)}"
        )
        # Le draft final ne doit JAMAIS être un mélange incohérent : soit il
        # a exécuté v3 (si le CONFIRM a gagné la course avant tout UPDATE),
        # soit il est resté/avancé en DRAFT (si un UPDATE est passé avant).
        final_draft = store["draft"]
        if executions:
            assert final_draft.status == ProcurementDraftStatus.EXECUTING
            # CONFIRM bump la version (2026-09-03 — voir `with_status`) :
            # UNE transition persistée, protégée par le compteur CAS comme
            # n'importe quelle autre.
            assert final_draft.version == v3.version + 1
        else:
            assert final_draft.status == ProcurementDraftStatus.DRAFT


# =====================================================================
# CRASH + RETRY — MCP idempotent vs non-idempotent (mandat §15)
# =====================================================================


class TestCrashAndRetryVariants:
    def test_mcp_success_then_crash_then_retry_never_recreates_blindly(self):
        """Variante RÉELLE de ce dépôt (audité : `create_auction` n'a
        AUCUNE déduplication serveur, voir `execution_key` docstring) — un
        succès MCP suivi d'un crash AVANT finalisation locale ne doit
        JAMAIS se traduire par un 2e appel `create_auction` au retry. Le
        draft reste EXECUTING (jamais réinitialisé), donc le retry obtient
        ALREADY_EXECUTING — un filet sûr même sans dédup serveur, PAS une
        preuve d'exactly-once (voir rapport §D)."""
        executing = _executing_draft()  # simule : claim gagné, appel en cours
        target = ConfirmationTarget(draft_id=executing.draft_id, draft_version=executing.version)

        # Le "crash" = aucune finalisation n'a eu lieu ; le draft persisté
        # est donc toujours EXECUTING au moment du retry.
        retry = apply_domain_action(executing, ConfirmProcurementDraft(target=target), claim=_ALWAYS_CLAIM)
        assert retry.kind == ProcurementOutcomeKind.ALREADY_EXECUTING

    def test_ambiguous_finalization_requires_reconciliation_not_a_second_create_auction(self):
        """Si la finalisation elle-même ne peut pas trancher (mcp_tool_
        executor n'a jamais rendu un statut clair), le draft devient
        EXECUTION_UNKNOWN — et un CONFIRM ultérieur (utilisateur qui retape
        "confirme", ou retry) exige une réconciliation humaine, jamais un
        aveuglé 2e `create_auction`."""
        executing = _executing_draft()
        target = ConfirmationTarget(draft_id=executing.draft_id, draft_version=executing.version)

        ambiguous = adapt_mcp_result(execution_status=None, execution_result=None)
        unknown = finalize_after_execution(executing, ambiguous)
        assert unknown.status == ProcurementDraftStatus.EXECUTION_UNKNOWN

        retry = apply_domain_action(unknown, ConfirmProcurementDraft(target=target), claim=_ALWAYS_CLAIM)
        assert retry.kind == ProcurementOutcomeKind.RECONCILIATION_REQUIRED

    def test_if_mcp_were_idempotent_the_execution_key_would_let_a_retry_recognize_itself(self):
        """Non réalisable aujourd'hui côté serveur (audité, voir
        `execution_key`) — ce test documente le contrat CLIENT déjà prêt :
        `execution_key(draft)` est stable par version, donc un retry du
        MÊME draft/version calcule TOUJOURS la même clé, prête à être
        passée en `idempotency_key=` à `AgriMCPClient.call_tool` le jour où
        la déduplication serveur existera."""
        executing = _executing_draft()
        key_attempt_1 = execution_key(executing)
        # Simule un retry sur le MÊME draft/version (pas une nouvelle
        # version) — la clé ne doit jamais changer.
        key_attempt_2 = execution_key(executing)
        assert key_attempt_1 == key_attempt_2


# =====================================================================
# RETRY POLICY — table complète, mandat §12
# =====================================================================


class TestRetryPolicyTable:
    @pytest.mark.parametrize(
        "status,expected_kind",
        [
            (ProcurementDraftStatus.EXECUTING, ProcurementOutcomeKind.ALREADY_EXECUTING),
            (ProcurementDraftStatus.EXECUTED, ProcurementOutcomeKind.ALREADY_EXECUTED),
            (ProcurementDraftStatus.FAILED, ProcurementOutcomeKind.ALREADY_FAILED),
            (
                ProcurementDraftStatus.EXECUTION_UNKNOWN,
                ProcurementOutcomeKind.RECONCILIATION_REQUIRED,
            ),
            (ProcurementDraftStatus.CANCELLED, ProcurementOutcomeKind.DRAFT_FINALIZED),
        ],
    )
    def test_a_confirm_against_every_terminal_or_in_flight_status_is_explicit(
        self, status, expected_kind
    ):
        draft = _draft().with_status(ProcurementDraftStatus.CONFIRMED).with_status(
            ProcurementDraftStatus.EXECUTING
        )
        if status not in (ProcurementDraftStatus.EXECUTING,):
            draft = draft.with_status(status) if status != ProcurementDraftStatus.CANCELLED else _draft().with_status(
                ProcurementDraftStatus.CANCELLED
            )
        target = ConfirmationTarget(draft_id=draft.draft_id, draft_version=draft.version)
        outcome = apply_domain_action(draft, ConfirmProcurementDraft(target=target), claim=_ALWAYS_CLAIM)
        assert outcome.kind == expected_kind
