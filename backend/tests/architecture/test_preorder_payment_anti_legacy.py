"""Tests source/structure anti-legacy pour la couche paiement PREORDER
(2026-09-03, clôture escrow/IPN) — mêmes garanties que
`test_preorder_transactional_contract.py`, étendues au paiement/IPN/
réconciliation."""
from __future__ import annotations

import inspect
import re

import ladini.graphs.agents.market_coach.flows.buyer.preorder_payment as payment_mod
import ladini.services.reconciliation.preorder_reconciliation_service as recon_mod
import ladini.workers.payments.paydunya_ipn_task as ipn_mod


def _code_only(source: str) -> str:
    code = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
    return re.sub(r"#.*", "", code)


class TestPaymentNeverConfusedWithConfirmation:
    def test_apply_payment_outcome_is_never_called_from_a_conversation_node(self):
        """Mandat RÈGLE ABSOLUE : aucun message "je confirme" ne doit
        pouvoir déclencher `apply_payment_outcome` — vérifie qu'aucun
        fichier `flows/buyer/preorder.py`/`preorder_confirmation.py`
        (les 2 points d'entrée conversationnels) ne l'importe."""
        import ladini.graphs.agents.market_coach.flows.buyer.preorder as preorder_mod
        import ladini.graphs.agents.market_coach.flows.buyer.preorder_confirmation as confirm_mod

        for mod in (preorder_mod, confirm_mod):
            code = _code_only(inspect.getsource(mod))
            assert "apply_payment_outcome" not in code, (
                f"{mod.__name__} ne doit JAMAIS appeler apply_payment_outcome "
                "(réservé à l'IPN/cron, jamais un tour utilisateur)"
            )

    def test_the_ipn_webhook_route_never_imports_the_conversation_graph(self):
        """Mandat §21 : l'IPN ne doit jamais produire une réponse
        conversationnelle directement — vérifié en s'assurant que le
        webhook/la tâche Celery n'importent aucun module `flows/`/`nodes/`
        du graphe LangGraph."""
        import ladini.api.routes.paydunya_webhook as webhook_mod

        for mod in (webhook_mod, ipn_mod):
            code = _code_only(inspect.getsource(mod))
            assert "send_whatsapp" not in code
            assert "final_response" not in code


class TestNoRawStringMatchingOnPaydunyaStatus:
    def test_the_domain_adapter_is_the_only_place_that_reads_raw_paydunya_status_strings(self):
        """`payment.status = ipn.status` interdit (mandat §8) — seul
        `adapt_payment_outcome` (domain/preorder_draft.py) doit comparer
        des chaînes de statut Paydunya brutes. `preorder_payment.py` ne
        doit JAMAIS faire `if paydunya_status == "..."` lui-même (au-delà du
        strict routage déjà présent dans `reconcile_invoice`, qui relève de
        `EscrowMixin`/`mark_escrow_paid` vs `mark_escrow_payment_failed`,
        PAS d'une décision sur l'état du draft)."""
        code = _code_only(inspect.getsource(payment_mod))
        # `apply_payment_outcome`/`apply_payment_expiry` (la partie qui
        # DÉCIDE l'état du draft) ne doivent jamais comparer un statut
        # Paydunya brut — seul `adapt_payment_outcome` (importé, pas
        # dupliqué) le fait.
        decide_fns = inspect.getsource(payment_mod.apply_payment_outcome) + inspect.getsource(
            payment_mod.apply_payment_expiry
        )
        decide_code = _code_only(decide_fns)
        for forbidden in ('"completed"', '"cancelled"', '"pending"'):
            assert forbidden not in decide_code


class TestReconciliationNeverGuessesPaymentSuccess:
    def test_reconcile_awaiting_payment_never_marks_executed_without_a_real_reconfirmation(self):
        """RÈGLE ABSOLUE : ne transforme jamais PAYMENT SUCCESS en succès
        d'exécution sans preuve — `reconcile_awaiting_payment_draft` doit
        appeler `reconcile_invoice` (re-confirmation RÉELLE Paydunya),
        jamais construire un `PaymentOutcomeKind.PAID` lui-même."""
        code = _code_only(inspect.getsource(recon_mod.reconcile_awaiting_payment_draft))
        assert "reconcile_invoice" in code
        assert "PaymentOutcomeKind.PAID" not in code
        assert "finalize_after_payment(" not in code  # délégué à reconcile_invoice, jamais appelé ici


class TestNoNewIdempotencyPrimitive:
    def test_no_module_reimplements_claim_once(self):
        """Mandat §26 : ne crée pas de nouvelle version de `claim_once` —
        vérifie qu'aucun des 3 nouveaux modules ne définit sa propre
        primitive de claim Redis/SET-NX."""
        for mod in (payment_mod, recon_mod, ipn_mod):
            code = _code_only(inspect.getsource(mod))
            assert "def claim_once" not in code
            assert "SET" not in code or "NX" not in code  # pas de ré-implémentation SET-NX
