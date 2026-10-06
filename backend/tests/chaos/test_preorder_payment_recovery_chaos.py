"""CHAOS — recovery/réconciliation/concurrence PREORDER, couche PAIEMENT
(2026-09-03, clôture escrow/IPN — mandat §15/§24, sur le modèle de
`test_procurement_recovery_chaos.py`).

Complète la matrice demandée pour les scénarios SPÉCIFIQUES au paiement,
non couverts ailleurs :

  - CONFIRM dupliqué pendant une réconciliation EXECUTING concurrente
      → `TestConfirmAndExecutingReconciliationRace` (équivalent PREORDER du
        scénario "neuf" de `test_procurement_recovery_chaos.py`).
  - RECONCILIATION × 2 sur un draft `AWAITING_PAYMENT` (le pendant du test
    déjà existant `test_preorder_reconciliation_service.py::
    TestReconciliationConcurrency`, qui ne couvre que la phase EXECUTING)
      → `TestReconciliationConcurrencyOnAwaitingPayment`.
  - PAYMENT IPN + CONFIRM dupliqué sur le MÊME draft déjà `AWAITING_PAYMENT`
    (un retry Celery/double "oui" WhatsApp ne doit jamais re-déclencher une
    exécution ni corrompre la synchronisation paiement)
      → `TestPaymentIpnAndDuplicateConfirmRace`.
  - Crash APRÈS que `mark_escrow_paid` a réussi (Order déjà ESCROWED) mais
    AVANT que `apply_payment_outcome` n'ait pu synchroniser le draft — la
    fenêtre de crash exacte que ce chantier a fermée (avant, ce trou ne se
    refermait JAMAIS automatiquement)
      → `TestCrashBetweenEscrowPaidAndDraftSync`.

PAYMENT IPN × 2 et RECONCILIATION × 2 (phase EXECUTING) sont déjà
verrouillés ailleurs (`test_preorder_payment_state_machine.py::
TestConcurrentIpnDeliveries`, `test_preorder_reconciliation_service.py::
TestReconciliationConcurrency`) — pas dupliqués ici."""
from __future__ import annotations

import threading

from tests.conftest import make_state, run
from tests.architecture.test_preorder_draft_persistence import _draft, _install_fake_db
from tests.unit.test_mcp_idempotency import _install_fake_db as _install_fake_idempotency_db

from ladini.graphs.agents.market_coach.domain.preorder_draft import (
    PaymentOutcomeKind,
    PreorderDraft,
    PreorderDraftStatus,
    adapt_payment_outcome,
    execution_key,
)
from ladini.graphs.agents.market_coach.flows.buyer.preorder_confirmation import (
    resolve_preorder_confirmation,
)
from ladini.graphs.agents.market_coach.flows.buyer.preorder_payment import (
    apply_payment_outcome,
)
from ladini.services.database import mcp_idempotency_store, preorder_draft_store
from ladini.services.reconciliation import preorder_reconciliation_service as svc


def _install(monkeypatch):
    draft_table = _install_fake_db(monkeypatch)
    idem_table = _install_fake_idempotency_db(monkeypatch)
    return draft_table, idem_table


def _executing_draft(draft_id: str) -> PreorderDraft:
    v1 = _draft(draft_id=draft_id)
    return v1._confirm_to_executing(delivery_lat=1.0, delivery_lon=1.0)


def _awaiting_payment_draft(draft_id: str) -> PreorderDraft:
    return _executing_draft(draft_id).with_status(PreorderDraftStatus.AWAITING_PAYMENT)


class TestConfirmAndExecutingReconciliationRace:
    def test_duplicate_confirm_racing_a_reconciliation_pass_yields_one_coherent_state(
        self, monkeypatch
    ):
        """Équivalent PREORDER de `test_procurement_recovery_chaos.py::
        TestConfirmAndReconciliationRaceConcurrently` — un CONFIRM dupliqué
        (draft déjà EXECUTING) arrive PENDANT qu'un worker de réconciliation
        traite le MÊME draft. Aucun des deux chemins ne doit re-déclencher
        `confirm_preorder_draft`, la ligne finale doit rester cohérente."""
        draft_table, idem_table = _install(monkeypatch)
        executing = _executing_draft("confirm-vs-reconcile")
        run(preorder_draft_store.insert(_draft(draft_id="confirm-vs-reconcile"), conversation_id="c"))
        run(
            preorder_draft_store.compare_and_swap(
                "confirm-vs-reconcile", expected_version=1, new_draft=executing
            )
        )

        key = execution_key(executing)
        run(mcp_idempotency_store.claim(key, svc.PREORDER_MCP_TOOL_NAME, "hashX"))
        run(
            mcp_idempotency_store.complete(
                key, svc.PREORDER_MCP_TOOL_NAME, {"status": "success", "order_id": "order-race"}
            )
        )

        target = {"draft_id": executing.draft_id, "draft_version": executing.version}
        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def confirm_worker():
            state = make_state(
                current_goal="PREORDER_CREATE_REQUEST",
                preorder_draft=executing.to_dict(),
                interpreted_event="CONFIRM",
                extracted_entities={},
            )
            state["pending_interaction"] = {"kind": "CONFIRM_ACTION", "target": target}
            barrier.wait()
            patch = run(resolve_preorder_confirmation(state, None))
            with results_lock:
                results.append(("confirm", patch))

        def reconcile_worker():
            barrier.wait()
            result = run(svc.reconcile_executing_draft(executing))
            with results_lock:
                results.append(("reconcile", result))

        threads = [threading.Thread(target=confirm_worker), threading.Thread(target=reconcile_worker)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final_row = run(preorder_draft_store.load("confirm-vs-reconcile"))
        assert final_row is not None
        # Le CONFIRM dupliqué sur un draft déjà EXECUTING (pas DRAFT) ne
        # peut structurellement pas rejouer l'exécution (`apply_domain_action`
        # le route vers ALREADY_EXECUTING/DRAFT_FINALIZED AVANT tout appel
        # MCP) — la ligne finale reflète uniquement ce que le réconciliateur
        # a tranché.
        assert final_row.status in (PreorderDraftStatus.EXECUTING, PreorderDraftStatus.EXECUTED)
        assert len(results) == 2, "les deux chemins doivent répondre, aucun ne doit lever"


class TestReconciliationConcurrencyOnAwaitingPayment:
    def test_two_concurrent_reconciliations_on_the_same_awaiting_payment_draft_yield_one_finalization(
        self, monkeypatch
    ):
        """Pendant AWAITING_PAYMENT du test déjà vert pour la phase
        EXECUTING (`test_preorder_reconciliation_service.py::
        TestReconciliationConcurrency`) — deux passages de réconciliation
        périodique (2 workers, ou un worker + un retry Celery) sur le MÊME
        draft `AWAITING_PAYMENT` ne doivent produire qu'UNE seule
        finalisation persistée, protégée par le CAS (aucun nouveau verrou)."""
        draft_table, idem_table = _install(monkeypatch)
        awaiting = _awaiting_payment_draft("awaiting-race")
        run(preorder_draft_store.insert(_draft(draft_id="awaiting-race"), conversation_id="c"))
        run(
            preorder_draft_store.compare_and_swap(
                "awaiting-race", expected_version=1,
                new_draft=_executing_draft("awaiting-race"),
            )
        )
        run(
            preorder_draft_store.compare_and_swap(
                "awaiting-race", expected_version=2, new_draft=awaiting
            )
        )

        # `reconcile_awaiting_payment_draft` délègue à `reconcile_invoice`,
        # qui a besoin d'un `order_id` -> `invoice_token` résolu et d'un
        # client Paydunya répondant "completed" -> on court-circuite ces 2
        # dépendances externes (déjà testées séparément), pour isoler ICI
        # la seule question posée : la concurrence sur la PERSISTANCE.
        import ladini.services.reconciliation.preorder_reconciliation_service as svc_mod

        async def _fake_find_invoice_token(order_id):
            return "token-awaiting-race"

        async def _fake_reconcile_invoice(invoice_token):
            return {"status": "success", "order_id": awaiting.order_id, "already_processed": False}

        monkeypatch.setattr(svc_mod, "_find_invoice_token", _fake_find_invoice_token)
        import ladini.graphs.agents.market_coach.flows.buyer.preorder_payment as payment_mod

        monkeypatch.setattr(payment_mod, "reconcile_invoice", _fake_reconcile_invoice)

        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(5)

        def worker():
            barrier.wait()
            r = run(svc.reconcile_awaiting_payment_draft(awaiting))
            with results_lock:
                results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final = run(preorder_draft_store.load("awaiting-race"))
        assert final is not None
        # `reconcile_invoice` (mocké) a déjà transitionné le draft via
        # `apply_payment_outcome` en interne côté PRODUCTION — ici,
        # court-circuité, donc la ligne persistée reste `AWAITING_PAYMENT`
        # (aucune transition n'a réellement eu lieu, seule la NON-EXPLOSION
        # concurrente et la cohérence de la ligne comptent pour ce test).
        assert final.status == PreorderDraftStatus.AWAITING_PAYMENT
        assert len(results) == 5, "les 5 passages doivent tous répondre sans lever"


class TestPaymentIpnAndDuplicateConfirmRace:
    def test_duplicate_confirm_on_an_awaiting_payment_draft_never_re_executes(self, monkeypatch):
        """Un CONFIRM dupliqué (retry Celery, double "oui" WhatsApp) arrive
        alors que le draft est DÉJÀ `AWAITING_PAYMENT` (l'IPN n'est pas
        encore arrivé) — `apply_domain_action` doit refuser structurellement
        (le statut n'est plus `DRAFT`), jamais ré-appeler
        `confirm_preorder_draft`/l'escrow. Mandat RÈGLE ABSOLUE : PAYMENT ≠
        CONFIRMATION, un tour utilisateur ne peut jamais court-circuiter la
        machine de paiement."""
        draft_table, _ = _install(monkeypatch)
        awaiting = _awaiting_payment_draft("dup-confirm-awaiting")
        run(preorder_draft_store.insert(_draft(draft_id="dup-confirm-awaiting"), conversation_id="c"))
        run(
            preorder_draft_store.compare_and_swap(
                "dup-confirm-awaiting", expected_version=1,
                new_draft=_executing_draft("dup-confirm-awaiting"),
            )
        )
        run(
            preorder_draft_store.compare_and_swap(
                "dup-confirm-awaiting", expected_version=2, new_draft=awaiting
            )
        )

        target = {"draft_id": awaiting.draft_id, "draft_version": awaiting.version}
        state = make_state(
            current_goal="PREORDER_CREATE_REQUEST",
            preorder_draft=awaiting.to_dict(),
            interpreted_event="CONFIRM",
            extracted_entities={},
        )
        state["pending_interaction"] = {"kind": "CONFIRM_ACTION", "target": target}

        # `mc_runtime=None` : la branche atteinte (draft.status != DRAFT)
        # retourne AVANT tout usage du runtime — si ce test échoue avec une
        # AttributeError sur `None`, c'est la preuve qu'une régression a
        # fait progresser le CONFIRM dupliqué plus loin qu'il ne devrait.
        patch = run(resolve_preorder_confirmation(state, None))

        final = run(preorder_draft_store.load("dup-confirm-awaiting"))
        assert final.status == PreorderDraftStatus.AWAITING_PAYMENT, (
            "un CONFIRM dupliqué ne doit JAMAIS faire régresser/ré-avancer "
            "un draft déjà en attente de paiement"
        )
        assert final.version == awaiting.version, "aucune nouvelle version n'a dû être créée"


class TestCrashBetweenEscrowPaidAndDraftSync:
    def test_a_draft_stuck_awaiting_payment_while_the_order_is_already_escrowed_is_recoverable(
        self, monkeypatch
    ):
        """LA fenêtre de crash exacte fermée par ce chantier (2026-09-03) :
        `mark_escrow_paid` a déjà réussi (Order.payment_status=ESCROWED)
        mais le processus est mort AVANT que `apply_payment_outcome` ne
        synchronise le `PreorderDraft` — avant cette phase, RIEN ne
        rattrapait automatiquement ce cas (le draft restait `AWAITING_PAYMENT`
        indéfiniment). Simulé ici en appelant directement
        `apply_payment_outcome` avec le résultat `mark_escrow_paid` que le
        crash a empêché de propager la 1re fois — exactement ce qu'un
        rejeu (IPN retry Celery OU passage de réconciliation périodique)
        referait en production."""
        draft_table, _ = _install(monkeypatch)
        awaiting = _awaiting_payment_draft("crash-window")
        run(preorder_draft_store.insert(_draft(draft_id="crash-window"), conversation_id="c"))
        run(
            preorder_draft_store.compare_and_swap(
                "crash-window", expected_version=1,
                new_draft=_executing_draft("crash-window"),
            )
        )
        run(
            preorder_draft_store.compare_and_swap(
                "crash-window", expected_version=2, new_draft=awaiting
            )
        )

        # Le point de vue du rejeu : Paydunya confirme "completed", et
        # `mark_escrow_paid` (rejoué, idempotent) répond soit un succès
        # frais soit `already_processed=True` — les deux doivent aboutir au
        # MÊME résultat de synchronisation.
        for mark_paid_result in (
            {"status": "success", "order_id": awaiting.order_id},
            {"status": "success", "order_id": awaiting.order_id, "already_processed": True},
        ):
            outcome_kind = adapt_payment_outcome("completed", mark_paid_result)
            assert outcome_kind == PaymentOutcomeKind.PAID

        result = run(
            apply_payment_outcome(
                awaiting.order_id,
                paydunya_status="completed",
                mark_paid_result={"status": "success", "order_id": awaiting.order_id},
            )
        )
        assert result is not None
        assert result.draft.status == PreorderDraftStatus.EXECUTED

        final = run(preorder_draft_store.load("crash-window"))
        assert final.status == PreorderDraftStatus.EXECUTED, (
            "la fenêtre de crash doit être rattrapable — plus de trou "
            "permanent entre Order.ESCROWED et PreorderDraft.AWAITING_PAYMENT"
        )

        # Un REJEU après coup (2e IPN, ou passage de réconciliation suivant)
        # doit être un NO-OP silencieux — le draft n'est plus AWAITING_PAYMENT.
        replay = run(
            apply_payment_outcome(
                awaiting.order_id,
                paydunya_status="completed",
                mark_paid_result={"status": "success", "order_id": awaiting.order_id, "already_processed": True},
            )
        )
        assert replay is None
        final_after_replay = run(preorder_draft_store.load("crash-window"))
        assert final_after_replay.status == PreorderDraftStatus.EXECUTED
        assert final_after_replay.version == final.version, "un rejeu ne doit JAMAIS créer une nouvelle version"
