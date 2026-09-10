"""Machine de paiement PREORDER (2026-09-03, clôture escrow/IPN) —
`domain/preorder_draft.py::PaymentOutcomeKind`/`adapt_payment_outcome`/
`finalize_after_payment`. Verrouille : PAYMENT ≠ CONFIRMATION (mandat
RÈGLE ABSOLUE), IPN out-of-order (mandat §8), jamais de transition sans
preuve explicite."""
from __future__ import annotations

import threading

import pytest

from tests.conftest import run
from tests.architecture.test_preorder_draft_persistence import _draft, _install_fake_db

from ladini.graphs.agents.market_coach.domain.preorder_draft import (
    IllegalDraftTransition,
    PaymentOutcomeKind,
    PreorderDraftStatus,
    adapt_payment_outcome,
    finalize_after_payment,
    finalize_after_payment_expiry,
)
from ladini.services.database import preorder_draft_store as store_mod


def _awaiting_payment_draft(**fields):
    draft = _draft(**fields)
    executing = draft._confirm_to_executing(delivery_lat=1.0, delivery_lon=1.0)
    return executing.with_status(PreorderDraftStatus.AWAITING_PAYMENT)


class TestAdaptPaymentOutcome:
    """SEUL adaptateur du format Paydunya — jamais un `payment.status =
    ipn.status` direct (mandat §8)."""

    def test_completed_with_confirmed_success_is_paid(self):
        outcome = adapt_payment_outcome("completed", {"status": "success"})
        assert outcome == PaymentOutcomeKind.PAID

    def test_completed_without_a_confirmed_mark_paid_result_is_ambiguous_not_paid(self):
        """RÈGLE ABSOLUE : ne transforme jamais PAYMENT SUCCESS en succès
        d'exécution par simple supposition — exige la confirmation
        EXPLICITE de `mark_escrow_paid`, pas seulement le statut Paydunya."""
        outcome = adapt_payment_outcome("completed", None)
        assert outcome == PaymentOutcomeKind.AMBIGUOUS
        outcome2 = adapt_payment_outcome("completed", {"status": "error"})
        assert outcome2 == PaymentOutcomeKind.AMBIGUOUS

    def test_cancelled_is_failed(self):
        assert adapt_payment_outcome("cancelled", None) == PaymentOutcomeKind.FAILED

    def test_pending_is_its_own_kind_never_actioned(self):
        """Mandat §8 : PAYMENT_PENDING seul ne doit JAMAIS déclencher de
        transition. Distinct d'`AMBIGUOUS` (statut inattendu, mérite
        investigation) — `PENDING` veut dire "pas encore", un cas normal
        de l'IPN out-of-order, jamais loggé comme anomalie. L'appelant
        (`apply_payment_outcome`) teste cet enum, jamais la chaîne brute."""
        assert adapt_payment_outcome("pending", None) == PaymentOutcomeKind.PENDING
        assert PaymentOutcomeKind.PENDING != PaymentOutcomeKind.AMBIGUOUS

    def test_an_unknown_status_is_ambiguous_never_guessed(self):
        assert adapt_payment_outcome("some_new_paydunya_status", None) == PaymentOutcomeKind.AMBIGUOUS
        assert adapt_payment_outcome(None, None) == PaymentOutcomeKind.AMBIGUOUS


class TestFinalizeAfterPayment:
    def test_paid_transitions_to_executed(self):
        draft = _awaiting_payment_draft(draft_id="d1")
        finalized = finalize_after_payment(draft, PaymentOutcomeKind.PAID)
        assert finalized.status == PreorderDraftStatus.EXECUTED
        assert finalized.version == draft.version + 1

    def test_failed_transitions_to_payment_failed_not_generic_failed(self):
        draft = _awaiting_payment_draft(draft_id="d2")
        finalized = finalize_after_payment(draft, PaymentOutcomeKind.FAILED)
        assert finalized.status == PreorderDraftStatus.PAYMENT_FAILED

    def test_ambiguous_transitions_to_execution_unknown_never_a_guess(self):
        draft = _awaiting_payment_draft(draft_id="d3")
        finalized = finalize_after_payment(draft, PaymentOutcomeKind.AMBIGUOUS)
        assert finalized.status == PreorderDraftStatus.EXECUTION_UNKNOWN

    def test_expiry_transitions_to_payment_expired(self):
        draft = _awaiting_payment_draft(draft_id="d4")
        finalized = finalize_after_payment_expiry(draft)
        assert finalized.status == PreorderDraftStatus.PAYMENT_EXPIRED

    def test_finalize_after_payment_refuses_a_draft_not_awaiting_payment(self):
        """Machine à état centralisée — un draft DRAFT/EXECUTED ne peut pas
        être "payé" par erreur d'appel (mandat §3 : chaque transition
        validée, jamais implicite)."""
        draft = _draft(draft_id="d5")  # status=DRAFT
        with pytest.raises(IllegalDraftTransition):
            finalize_after_payment(draft, PaymentOutcomeKind.PAID)


class TestIpnOutOfOrder:
    """Mandat §8 — rejoue exactement les 2 scénarios demandés."""

    def test_pending_after_success_never_regresses_an_already_paid_draft(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.buyer.preorder_payment as pp_mod

        _install_fake_db(monkeypatch)
        run(store_mod.insert(_draft(draft_id="seq1", order_id="order-seq1"), conversation_id="c"))
        run(
            store_mod.compare_and_swap(
                "seq1",
                expected_version=1,
                new_draft=_awaiting_payment_draft(draft_id="seq1", order_id="order-seq1"),
            )
        )

        # Séquence : PENDING, SUCCESS, PENDING (le dernier ne doit RIEN faire).
        r1 = run(pp_mod.apply_payment_outcome("order-seq1", paydunya_status="pending", mark_paid_result=None))
        assert r1 is None  # pas actionnable

        r2 = run(
            pp_mod.apply_payment_outcome(
                "order-seq1", paydunya_status="completed", mark_paid_result={"status": "success"}
            )
        )
        assert r2 is not None
        assert r2.draft.status == PreorderDraftStatus.EXECUTED

        r3 = run(pp_mod.apply_payment_outcome("order-seq1", paydunya_status="pending", mark_paid_result=None))
        # Le draft est déjà EXECUTED (plus AWAITING_PAYMENT) — no-op garanti
        # par le guard de statut, jamais une régression.
        assert r3 is None
        final = run(store_mod.load("seq1"))
        assert final.status == PreorderDraftStatus.EXECUTED

    def test_duplicate_success_events_produce_exactly_one_mutation(self, monkeypatch):
        import ladini.graphs.agents.market_coach.flows.buyer.preorder_payment as pp_mod

        _install_fake_db(monkeypatch)
        run(store_mod.insert(_draft(draft_id="seq2", order_id="order-seq2"), conversation_id="c"))
        run(
            store_mod.compare_and_swap(
                "seq2",
                expected_version=1,
                new_draft=_awaiting_payment_draft(draft_id="seq2", order_id="order-seq2"),
            )
        )

        r1 = run(
            pp_mod.apply_payment_outcome(
                "order-seq2", paydunya_status="completed", mark_paid_result={"status": "success"}
            )
        )
        assert r1 is not None
        version_after_first = r1.draft.version

        r2 = run(
            pp_mod.apply_payment_outcome(
                "order-seq2", paydunya_status="completed", mark_paid_result={"status": "success"}
            )
        )
        # 2e événement identique -> NO-OP (draft déjà EXECUTED), jamais une
        # 2e mutation.
        assert r2 is None
        final = run(store_mod.load("seq2"))
        assert final.version == version_after_first
        assert final.status == PreorderDraftStatus.EXECUTED


class TestConcurrentIpnDeliveries:
    def test_two_workers_processing_the_same_ipn_simultaneously_yield_one_transition(
        self, monkeypatch
    ):
        """Mandat §7 : IPN identique reçu par deux workers simultanément ->
        exactement une transition. Réutilise le CAS déjà en place, aucun
        nouveau verrou."""
        import ladini.graphs.agents.market_coach.flows.buyer.preorder_payment as pp_mod

        _install_fake_db(monkeypatch)
        run(store_mod.insert(_draft(draft_id="race1", order_id="order-race1"), conversation_id="c"))
        run(
            store_mod.compare_and_swap(
                "race1",
                expected_version=1,
                new_draft=_awaiting_payment_draft(draft_id="race1", order_id="order-race1"),
            )
        )

        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(5)

        def worker():
            barrier.wait()
            r = run(
                pp_mod.apply_payment_outcome(
                    "order-race1", paydunya_status="completed", mark_paid_result={"status": "success"}
                )
            )
            with results_lock:
                results.append(r is not None)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Au plus UNE des 5 tentatives concurrentes a réellement gagné le
        # CAS (les autres arrivent après que le statut ait déjà changé,
        # traitées comme no-op par le guard `status != AWAITING_PAYMENT`
        # dans les CAS où elles arrivent après coup, ou par le CAS
        # lui-même si elles lisent le même snapshot).
        final = run(store_mod.load("race1"))
        assert final.status == PreorderDraftStatus.EXECUTED
        # DRAFT(1) -> EXECUTING(2) -> AWAITING_PAYMENT(3, posé directement
        # pour isoler ce test) -> EXECUTED(4) : peu importe le compte exact
        # de versions, ce qui compte est qu'il n'y ait qu'UN SEUL passage
        # AWAITING_PAYMENT -> EXECUTED malgré les 5 tentatives concurrentes.
        assert final.version == 4
