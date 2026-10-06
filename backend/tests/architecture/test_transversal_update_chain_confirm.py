"""TEST TRANSVERSE UPDATE → UPDATE → CONFIRM (2026-09-03, hardening
transverse avant migration SALES).

Le rapport PREORDER (`PREORDER_ESCROW_IPN_RECONCILIATION_2026-09-03.md`,
"Limites restantes" §1) reconnaissait qu'un seul UPDATE (v1→v2) était
testé, jamais une CHAÎNE de 2 corrections (v1→v2→v3) avec une tentative de
confirmation à CHAQUE version intermédiaire. Ce fichier ferme ce gap, pour
les DEUX workflows migrés (PROCUREMENT et PREORDER), avec le MÊME
scénario :

    draft v1
    → update → v2
    → update → v3
    → confirm(v1)  → doit être rejeté (STALE_TARGET), v1 n'exécute JAMAIS
    → confirm(v2)  → doit être rejeté (STALE_TARGET), v2 n'exécute JAMAIS
    → confirm(v3)  → SEULE cible valide, exécute AVEC le contenu de v3

Ne se contente pas de vérifier le `kind` de l'outcome (déjà fait par
`TestInvariantE`/`TestInvariantF` dans les 2 fichiers de contrat
transactionnel existants, sur UNE SEULE mutation) — vérifie explicitement
le CONTENU métier persisté après l'exécution : c'est bien la valeur de v3
qui est exécutée, jamais un résidu de v1/v2 (exactement la classe de bug
que toute cette architecture existe pour rendre impossible — voir
`test_preorder_transactional_contract.py::TestInvariantL` pour l'équivalent
"affichage", ce fichier verrouille l'équivalent "exécution")."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.domain.preorder_draft import (
    ConfirmPreorderDraft,
    PreorderDraftStatus,
    PreorderOutcomeKind,
)
from ladini.graphs.agents.market_coach.domain.preorder_draft import (
    apply_domain_action as apply_preorder_action,
)
from ladini.graphs.agents.market_coach.domain.procurement_draft import (
    ConfirmationTarget,
    ConfirmProcurementDraft,
    ProcurementDraftStatus,
    ProcurementOutcomeKind,
)
from ladini.graphs.agents.market_coach.domain.procurement_draft import (
    apply_domain_action as apply_procurement_action,
)
from tests.architecture.test_preorder_draft_persistence import (
    _draft as _preorder_draft,
)
from tests.architecture.test_procurement_draft_persistence import (
    _draft as _procurement_draft,
)

_ALWAYS_CLAIM = lambda key: True  # noqa: E731


class TestProcurementUpdateChainThenConfirm:
    def test_two_updates_then_confirm_at_each_version_only_v3_executes_with_v3_content(self):
        v1 = _procurement_draft(draft_id="chain-1", quantity=100.0)
        v2 = v1.with_updates(quantity=200.0)
        v3 = v2.with_updates(quantity=300.0)
        assert (v1.version, v2.version, v3.version) == (1, 2, 3)

        # confirm(v1) sur l'état COURANT (v3) — cible périmée de 2 versions.
        target_v1 = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
        outcome_v1 = apply_procurement_action(
            v3, ConfirmProcurementDraft(target=target_v1), claim=_ALWAYS_CLAIM
        )
        assert outcome_v1.kind == ProcurementOutcomeKind.STALE_TARGET
        assert outcome_v1.draft.status == ProcurementDraftStatus.DRAFT
        assert outcome_v1.draft.version == 3, "le draft courant reste v3, inchangé par le rejet"

        # confirm(v2) sur l'état COURANT (v3) — cible périmée d'1 version.
        target_v2 = ConfirmationTarget(draft_id=v2.draft_id, draft_version=v2.version)
        outcome_v2 = apply_procurement_action(
            v3, ConfirmProcurementDraft(target=target_v2), claim=_ALWAYS_CLAIM
        )
        assert outcome_v2.kind == ProcurementOutcomeKind.STALE_TARGET
        assert outcome_v2.draft.status == ProcurementDraftStatus.DRAFT

        # confirm(v3) — SEULE cible valide.
        target_v3 = ConfirmationTarget(draft_id=v3.draft_id, draft_version=v3.version)
        outcome_v3 = apply_procurement_action(
            v3, ConfirmProcurementDraft(target=target_v3), claim=_ALWAYS_CLAIM
        )
        assert outcome_v3.kind == ProcurementOutcomeKind.CONFIRMED_READY_FOR_EXECUTION
        assert outcome_v3.draft.status == ProcurementDraftStatus.EXECUTING
        # LE contenu exécuté est bien celui de v3 — jamais un résidu de
        # v1 (100.0) ou v2 (200.0).
        assert outcome_v3.draft.quantity == 300.0
        assert outcome_v3.draft.version == 4, "1 bump pour la transition CONFIRMED->EXECUTING"

    def test_confirming_v1_or_v2_never_transitions_the_draft_to_executing_even_transiently(self):
        """Renforce le test précédent : même si `apply_domain_action` était
        appelé à répétition avec v1/v2 (retry naïf d'un appelant bugué), le
        draft ne doit JAMAIS passer par EXECUTING avec un contenu périmé —
        pas seulement "le dernier appel gagnant est correct"."""
        v1 = _procurement_draft(draft_id="chain-2", quantity=1.0)
        v2 = v1.with_updates(quantity=2.0)
        v3 = v2.with_updates(quantity=3.0)

        current = v3
        for stale in (v1, v2):
            target = ConfirmationTarget(draft_id=stale.draft_id, draft_version=stale.version)
            outcome = apply_procurement_action(
                current, ConfirmProcurementDraft(target=target), claim=_ALWAYS_CLAIM
            )
            assert outcome.draft.status != ProcurementDraftStatus.EXECUTING
            current = outcome.draft  # inchangé, toujours v3/DRAFT

        assert current.version == 3
        assert current.status == ProcurementDraftStatus.DRAFT


class TestPreorderUpdateChainThenConfirm:
    def test_two_updates_then_confirm_at_each_version_only_v3_executes_with_v3_content(self):
        v1 = _preorder_draft(draft_id="pchain-1", total_amount=1000.0)
        v2 = v1.with_updates(total_amount=2000.0)
        v3 = v2.with_updates(total_amount=3000.0)
        assert (v1.version, v2.version, v3.version) == (1, 2, 3)

        target_v1 = ConfirmationTarget(draft_id=v1.draft_id, draft_version=v1.version)
        outcome_v1 = apply_preorder_action(
            v3,
            ConfirmPreorderDraft(target=target_v1, delivery_lat=1.0, delivery_lon=1.0),
            claim=_ALWAYS_CLAIM,
        )
        assert outcome_v1.kind == PreorderOutcomeKind.STALE_TARGET
        assert outcome_v1.draft.status == PreorderDraftStatus.DRAFT
        assert outcome_v1.draft.version == 3

        target_v2 = ConfirmationTarget(draft_id=v2.draft_id, draft_version=v2.version)
        outcome_v2 = apply_preorder_action(
            v3,
            ConfirmPreorderDraft(target=target_v2, delivery_lat=1.0, delivery_lon=1.0),
            claim=_ALWAYS_CLAIM,
        )
        assert outcome_v2.kind == PreorderOutcomeKind.STALE_TARGET
        assert outcome_v2.draft.status == PreorderDraftStatus.DRAFT

        target_v3 = ConfirmationTarget(draft_id=v3.draft_id, draft_version=v3.version)
        outcome_v3 = apply_preorder_action(
            v3,
            ConfirmPreorderDraft(target=target_v3, delivery_lat=1.0, delivery_lon=1.0),
            claim=_ALWAYS_CLAIM,
        )
        assert outcome_v3.kind == PreorderOutcomeKind.CONFIRMED_READY_FOR_EXECUTION
        assert outcome_v3.draft.status == PreorderDraftStatus.EXECUTING
        assert outcome_v3.draft.total_amount == 3000.0
        assert outcome_v3.draft.version == 4

    def test_confirming_v1_or_v2_never_transitions_the_draft_to_executing_even_transiently(self):
        v1 = _preorder_draft(draft_id="pchain-2", total_amount=10.0)
        v2 = v1.with_updates(total_amount=20.0)
        v3 = v2.with_updates(total_amount=30.0)

        current = v3
        for stale in (v1, v2):
            target = ConfirmationTarget(draft_id=stale.draft_id, draft_version=stale.version)
            outcome = apply_preorder_action(
                current,
                ConfirmPreorderDraft(target=target, delivery_lat=1.0, delivery_lon=1.0),
                claim=_ALWAYS_CLAIM,
            )
            assert outcome.draft.status != PreorderDraftStatus.EXECUTING
            current = outcome.draft

        assert current.version == 3
        assert current.status == PreorderDraftStatus.DRAFT
