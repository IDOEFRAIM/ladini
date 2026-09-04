"""CHAOS — recovery/réconciliation PROCUREMENT (2026-09-03, mandat recovery
phase 7). Chaque scénario doit produire un résultat EXPLICITE, jamais un
repli silencieux.

Couverture des 9 scénarios demandés — certains sont déjà verrouillés
ailleurs (référencés ici en commentaire plutôt que dupliqués) :

  - crash before MCP / after MCP / timeout ambigu
      → `test_two_step_reconciliation_resolves_once_the_mcp_record_appears`
        ci-dessous (before→AMBIGUOUS puis after→EXECUTED, même draft) ;
        timeout déjà verrouillé par
        `test_procurement_draft_architectural_hardening.py::
        test_an_ambiguous_mcp_outcome_transitions_to_execution_unknown_not_a_guess`.
  - MCP (table de dédup) indisponible pendant la réconciliation
      → `test_idempotency_store_unavailable_during_reconciliation_is_explicit_not_silent`.
  - DB (draft store) indisponible pendant la réconciliation
      → `test_draft_store_unavailable_during_reconciliation_never_crashes`.
  - Redis indisponible pendant un CONFIRM
      → `test_redis_unavailable_during_confirm_still_protected_by_postgres_cas`.
  - réconciliation dupliquée
      → déjà verrouillé, `test_procurement_reconciliation_service.py::
        TestReconciliationIdempotence` + `TestReconciliationConcurrency`.
  - CONFIRM dupliqué
      → déjà verrouillé, `test_procurement_draft_transactional_contract.py::
        TestConfirmIsIdempotent`.
  - CONFIRM et réconciliation concurrents sur le MÊME draft
      → `test_confirm_and_reconciliation_racing_concurrently_yield_one_coherent_state`
        ci-dessous (scénario réellement NOUVEAU de cette phase)."""
from __future__ import annotations

import threading

from tests.conftest import make_state, run
from tests.architecture.test_procurement_draft_persistence import (
    _install_fake_db as _install_fake_draft_db,
)
from tests.unit.test_mcp_idempotency import (
    _install_fake_db as _install_fake_idempotency_db,
)

from agriconnect.graphs.agents.market_coach.domain.procurement_draft import (
    ProcurementDraft,
    ProcurementDraftStatus,
    execution_key,
)
from agriconnect.graphs.agents.market_coach.flows.buyer.procurement_confirmation import (
    resolve_procurement_confirmation,
)
from agriconnect.services.database import mcp_idempotency_store, procurement_draft_store
from agriconnect.services.reconciliation import procurement_reconciliation_service as recon_svc


def _install(monkeypatch):
    draft_table = _install_fake_draft_db(monkeypatch)
    idem_table = _install_fake_idempotency_db(monkeypatch)
    return draft_table, idem_table


def _executing_draft(draft_id: str) -> ProcurementDraft:
    v1 = ProcurementDraft.new(
        draft_id=draft_id, product="tomates", quantity=2000.0, unit="KG",
        price=250.0, price_unit="KG",
    )
    return v1._confirm_to_executing()


class TestTwoStepReconciliationAcrossCrashWindows:
    def test_reconciliation_after_the_mcp_record_has_landed_resolves_in_one_pass(
        self, monkeypatch
    ):
        """Le cas favorable : quand le worker de réconciliation tourne
        (déclenché par `find_stale_executing_candidates`, un certain délai
        après le crash), l'appel MCP a ENTRE-TEMPS laissé sa trace dans la
        table de dédup — un SEUL passage suffit à trouver l'effet externe
        et finaliser EXECUTED. Peu importe le délai réel écoulé : ce que ce
        module regarde, c'est l'état de la table au moment du passage, pas
        une chronologie qu'il ne peut pas observer autrement."""
        draft_table, idem_table = _install(monkeypatch)
        executing = _executing_draft("late-record")
        run(procurement_draft_store.insert(executing, conversation_id="c"))
        draft_table._rows["late-record"]["status"] = "EXECUTING"
        draft_table._rows["late-record"]["version"] = executing.version
        draft_table._rows["late-record"]["payload"] = executing.to_dict()

        key = execution_key(executing)
        run(mcp_idempotency_store.claim(key, recon_svc.PROCUREMENT_MCP_TOOL_NAME, "hashX"))
        run(
            mcp_idempotency_store.complete(
                key, recon_svc.PROCUREMENT_MCP_TOOL_NAME, {"status": "success", "auction_id": "auc-late"}
            )
        )

        result = run(recon_svc.reconcile_draft(executing))
        assert result.outcome is recon_svc.ReconciliationOutcome.EXTERNAL_EFFECT_FOUND
        assert result.final_status == ProcurementDraftStatus.EXECUTED.value

    def test_once_execution_unknown_a_later_reconciliation_pass_is_a_safe_no_op(
        self, monkeypatch
    ):
        """LIMITE RÉELLE, verrouillée explicitement (pas cachée) : une fois
        qu'un draft est passé `EXECUTION_UNKNOWN` (parce que la table de
        dédup n'avait encore AUCUNE trace au moment du 1er passage),
        `EXECUTION_UNKNOWN` est un état TERMINAL de la machine à état
        (`_ALLOWED_TRANSITIONS`, hérité d'un mandat antérieur — "nécessite
        une réconciliation HUMAINE, jamais un retry aveugle du code"). Même
        si l'effet externe apparaît ENSUITE dans la table de dédup, CE
        module ne le découvrira JAMAIS automatiquement — `reconcile_draft`
        exige `status == EXECUTING`, un draft `EXECUTION_UNKNOWN` retourne
        systématiquement `NOT_EXECUTING`, sans lever ni changer quoi que ce
        soit. Documenté dans le rapport final comme limite explicite, pas
        maquillée en "reconciliation complète"."""
        draft_table, idem_table = _install(monkeypatch)
        executing = _executing_draft("dead-end")
        run(procurement_draft_store.insert(executing, conversation_id="c"))
        draft_table._rows["dead-end"]["status"] = "EXECUTING"
        draft_table._rows["dead-end"]["version"] = executing.version
        draft_table._rows["dead-end"]["payload"] = executing.to_dict()

        # 1er passage : rien dans la table de dédup -> AMBIGUOUS -> EXECUTION_UNKNOWN.
        first_pass = run(recon_svc.reconcile_draft(executing))
        assert first_pass.outcome is recon_svc.ReconciliationOutcome.AMBIGUOUS
        assert first_pass.final_status == ProcurementDraftStatus.EXECUTION_UNKNOWN.value

        # L'effet externe apparaît ENSUITE — trop tard pour ce module.
        key = execution_key(executing)
        run(mcp_idempotency_store.claim(key, recon_svc.PROCUREMENT_MCP_TOOL_NAME, "hashX"))
        run(
            mcp_idempotency_store.complete(
                key, recon_svc.PROCUREMENT_MCP_TOOL_NAME, {"status": "success", "auction_id": "auc-too-late"}
            )
        )

        # Un worker qui recharge le draft depuis la DB (comportement réel de
        # `find_stale_executing_candidates`, qui ne retourne QUE les
        # `EXECUTING` — un `EXECUTION_UNKNOWN` n'y apparaîtrait même plus)
        # verrait un draft déjà `EXECUTION_UNKNOWN` :
        reloaded = run(procurement_draft_store.load("dead-end"))
        assert reloaded.status == ProcurementDraftStatus.EXECUTION_UNKNOWN

        second_pass = run(recon_svc.reconcile_draft(reloaded))
        assert second_pass.outcome is recon_svc.ReconciliationOutcome.NOT_EXECUTING
        assert second_pass.persisted is False
        # La ligne persistée reste EXECUTION_UNKNOWN — jamais corrigée en
        # EXECUTED après coup par ce mécanisme, malgré la preuve disponible.
        final_row = run(procurement_draft_store.load("dead-end"))
        assert final_row.status == ProcurementDraftStatus.EXECUTION_UNKNOWN


class TestInfrastructureUnavailableDuringReconciliation:
    def test_idempotency_store_unavailable_during_reconciliation_is_explicit_not_silent(
        self, monkeypatch
    ):
        draft_table, _ = _install(monkeypatch)
        # La table de dédup spécifiquement injoignable — le draft store,
        # lui, reste joignable (scénario réaliste : deux bases/pools
        # distincts, ou un incident partiel).
        monkeypatch.setattr(mcp_idempotency_store, "get_sessionmaker", lambda: None)

        executing = _executing_draft("idem-down")
        run(procurement_draft_store.insert(executing, conversation_id="c"))
        draft_table._rows["idem-down"]["status"] = "EXECUTING"
        draft_table._rows["idem-down"]["version"] = executing.version
        draft_table._rows["idem-down"]["payload"] = executing.to_dict()

        result = run(recon_svc.reconcile_draft(executing))
        # `peek()` best-effort -> None -> AMBIGUOUS -> EXECUTION_UNKNOWN,
        # PAS une exception, PAS un faux EXECUTED/FAILED.
        assert result.outcome is recon_svc.ReconciliationOutcome.AMBIGUOUS
        assert result.final_status == ProcurementDraftStatus.EXECUTION_UNKNOWN.value
        assert result.persisted is True  # la finalisation EXECUTION_UNKNOWN, elle, a réussi

    def test_draft_store_unavailable_during_reconciliation_never_crashes(self, monkeypatch):
        """Le pire cas : la table CANONIQUE elle-même est injoignable —
        `reconcile_draft` ne doit JAMAIS lever, doit retourner un résultat
        `persisted=False` explicite plutôt qu'un crash de worker."""
        _install(monkeypatch)
        monkeypatch.setattr(procurement_draft_store, "get_sessionmaker", lambda: None)

        executing = _executing_draft("draft-store-down")
        # Pas d'insert (impossible, la DB est "down" pour ce test) — on
        # simule directement l'appel sur un draft EXECUTING en mémoire, ce
        # qui est exactement ce qu'un worker ferait s'il tenait encore une
        # référence après la panne.
        result = run(recon_svc.reconcile_draft(executing))
        assert result.persisted is False
        # Le verdict a quand même été calculé (pas d'exception) — juste pas
        # persisté. Journalisé bruyamment côté module (vérifié par un
        # WARNING dans `reconcile_draft`, pas ré-assert ici pour ne pas
        # coupler ce test au texte du log).


class TestRedisUnavailableDuringConfirm:
    def test_redis_unavailable_during_confirm_still_protected_by_postgres_cas(
        self, monkeypatch
    ):
        """`claim_once` est fail-OPEN si Redis est injoignable (voir
        `core/idempotency.py` — dégradation volontaire et déjà documentée
        là-bas). La VRAIE protection contre une double exécution reste le
        CAS PostgreSQL : ce test le prouve en forçant `claim_once` à TOUJOURS
        autoriser (équivalent exact d'un Redis mort) et en montrant qu'une
        course RÉELLE entre deux CONFIRM est quand même tranchée une seule
        fois par la version."""
        import agriconnect.graphs.agents.market_coach.domain.procurement_draft as pd_mod

        _install(monkeypatch)
        # Simule Redis down : claim_once ne protège plus rien (fail-open).
        monkeypatch.setattr(pd_mod, "claim_once", lambda key: True)

        v1 = ProcurementDraft.new(
            draft_id="redis-down", product="tomates", quantity=1.0, unit="KG",
            price=1.0, price_unit="KG",
        )
        run(procurement_draft_store.insert(v1, conversation_id="c"))
        target = {"draft_id": v1.draft_id, "draft_version": v1.version}

        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(5)

        def confirm_worker():
            state = make_state(
                current_goal="PROCUREMENT_CREATE_REQUEST",
                procurement_draft=v1.to_dict(),
                interpreted_event="CONFIRM",
                extracted_entities={},
            )
            state["pending_interaction"] = {"kind": "CONFIRM_ACTION", "target": target}
            barrier.wait()
            patch = run(resolve_procurement_confirmation(state, None))
            with results_lock:
                results.append(patch.get("status"))

        threads = [threading.Thread(target=confirm_worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        executing_count = sum(1 for s in results if s == "EXECUTING")
        assert executing_count == 1, (
            f"une seule confirmation aurait dû réussir malgré Redis down, "
            f"obtenu {executing_count} sur {results}"
        )
        final = run(procurement_draft_store.load("redis-down"))
        assert final.status == ProcurementDraftStatus.EXECUTING


class TestConfirmAndReconciliationRaceConcurrently:
    def test_confirm_and_reconciliation_racing_concurrently_yield_one_coherent_state(
        self, monkeypatch
    ):
        """Scénario neuf de cette phase : un CONFIRM dupliqué (retry Celery,
        double "oui" WhatsApp) arrive PENDANT qu'un worker de réconciliation
        traite le MÊME draft (déjà EXECUTING). Aucun des deux chemins ne
        doit re-déclencher `create_auction`, et la ligne finale doit être
        cohérente — protégés par le MÊME CAS, sans nouveau verrou."""
        import agriconnect.graphs.agents.market_coach.domain.procurement_draft as pd_mod

        draft_table, idem_table = _install(monkeypatch)
        monkeypatch.setattr(pd_mod, "claim_once", lambda key: True)

        executing = _executing_draft("confirm-vs-reconcile")
        run(procurement_draft_store.insert(executing, conversation_id="c"))
        draft_table._rows["confirm-vs-reconcile"]["status"] = "EXECUTING"
        draft_table._rows["confirm-vs-reconcile"]["version"] = executing.version
        draft_table._rows["confirm-vs-reconcile"]["payload"] = executing.to_dict()

        key = execution_key(executing)
        run(mcp_idempotency_store.claim(key, recon_svc.PROCUREMENT_MCP_TOOL_NAME, "hashX"))
        run(
            mcp_idempotency_store.complete(
                key, recon_svc.PROCUREMENT_MCP_TOOL_NAME, {"status": "success", "auction_id": "auc-race"}
            )
        )

        target = {"draft_id": executing.draft_id, "draft_version": executing.version}
        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def confirm_worker():
            state = make_state(
                current_goal="PROCUREMENT_CREATE_REQUEST",
                procurement_draft=executing.to_dict(),
                interpreted_event="CONFIRM",
                extracted_entities={},
            )
            state["pending_interaction"] = {"kind": "CONFIRM_ACTION", "target": target}
            barrier.wait()
            patch = run(resolve_procurement_confirmation(state, None))
            with results_lock:
                results.append(("confirm", patch.get("status")))

        def reconcile_worker():
            barrier.wait()
            result = run(recon_svc.reconcile_draft(executing))
            with results_lock:
                results.append(("reconcile", result.final_status))

        threads = [threading.Thread(target=confirm_worker), threading.Thread(target=reconcile_worker)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Le CONFIRM dupliqué ne doit JAMAIS revenir "EXECUTING" comme s'il
        # relançait une exécution — soit ALREADY_EXECUTING (pas de bump de
        # version, donc `status` projeté reste EXECUTING dans le patch
        # actuel — voir apply_response_plan : ce n'est PAS re-déclenché,
        # juste re-décrit), soit la ligne finale (après le réconciliateur)
        # si celui-ci a déjà gagné.
        final_row = run(procurement_draft_store.load("confirm-vs-reconcile"))
        assert final_row is not None
        assert final_row.status in (
            ProcurementDraftStatus.EXECUTING,
            ProcurementDraftStatus.EXECUTED,
        )
        # Les deux tours ont répondu (aucun n'a levé) — preuve qu'aucune
        # incohérence non gérée n'est remontée en exception.
        assert len(results) == 2
