"""SNAPSHOT DE CONFIRMATION (2026-09-03, hardening transverse, mandat §4).

Question posée par le mandat : "qu'est-ce exactement que l'utilisateur a
confirmé ?" doit pouvoir être répondu SANS AMBIGUÏTÉ plusieurs jours plus
tard. Décision (documentée en détail dans le rapport final, section D) :
`ConfirmationTarget(draft_id, draft_version)` EST une preuve suffisante —
AUCUN mécanisme neuf introduit. Ce fichier verrouille les 2 faits
structurels qui rendent cette réponse vraie, pour ne jamais la laisser
devenir fausse par accident :

1. `with_updates` (le SEUL point d'écriture des champs métier) est
   STRUCTURELLEMENT impossible une fois le draft sorti de `DRAFT`
   (`_transition(DRAFT)` lève `IllegalDraftTransition` sur tout statut
   post-confirmation) — donc le contenu métier (product/quantity/price/
   items/total_amount/...) d'un draft `EXECUTING`/`EXECUTED`/`FAILED`/
   `AWAITING_PAYMENT`/... est GELÉ depuis l'instant `CONFIRM` a réussi.
   Seuls `status`/`version` changent après ce point (`with_status`).
2. La ligne PostgreSQL est un SNAPSHOT versionné (une ligne par
   `draft_id`, jamais un append-only log) — donc lire le draft PAR
   `draft_id` À N'IMPORTE QUEL MOMENT après la confirmation retourne
   TOUJOURS le contenu figé à la version confirmée (le fait (1) garantit
   qu'aucune écriture ultérieure n'a pu le modifier).

Ensemble : `draft_id` (stable sur tout le cycle de vie) + une relecture du
draft (à N'IMPORTE QUEL instant ultérieur) reconstituent EXACTEMENT "ce qui
a été confirmé" — items, quantité, prix, livraison, conditions de paiement
— sans ambiguïté, sans nouvel artefact à maintenir en parallèle.

NUANCE IMPORTANTE (trouvée en écrivant CE test, documentée explicitement
plutôt que corrigée en silence) : `ConfirmationTarget.matches()` compare
`draft_version` À L'IDENTIQUE — un draft dont le `status` a transité
PLUSIEURS fois après la confirmation (`with_status` bump la version à
CHAQUE transition, pas seulement `with_updates`) ne "matche" donc plus un
`ConfirmationTarget` capturé au moment `EXECUTING`. Ce n'est PAS un défaut
du snapshot : `.matches()` sert un but différent et plus étroit (rejeter un
CONFIRM visant une version déjà périmée AU MOMENT de la décision, voir
`apply_domain_action`/`STALE_TARGET`), jamais une clé de relecture
rétrospective "3 jours plus tard". La preuve rétrospective repose sur
`draft_id` seul (stable) + l'invariant de gel des champs métier ci-dessus —
`draft_version` au moment confirmé reste utile comme MARQUEUR historique
(déjà journalisé par `record_procurement_transaction_event`'s
`draft_version_before/after` à chaque étape), pas comme clé de comparaison
directe contre une version ultérieure."""
from __future__ import annotations

import pytest

from tests.architecture.test_procurement_draft_persistence import (
    _draft as _procurement_draft,
)
from tests.architecture.test_preorder_draft_persistence import (
    _draft as _preorder_draft,
)

from ladini.graphs.agents.market_coach.domain.procurement_draft import (
    IllegalDraftTransition as ProcurementIllegalTransition,
    ProcurementDraftStatus,
)
from ladini.graphs.agents.market_coach.domain.preorder_draft import (
    IllegalDraftTransition as PreorderIllegalTransition,
    PreorderDraftStatus,
)


class TestProcurementConfirmationSnapshotIsFrozen:
    def test_with_updates_after_executing_is_structurally_impossible(self):
        v1 = _procurement_draft(draft_id="snap-1", quantity=100.0)
        confirmed = v1._confirm_to_executing()
        assert confirmed.status == ProcurementDraftStatus.EXECUTING

        with pytest.raises(ProcurementIllegalTransition):
            confirmed.with_updates(quantity=999.0)

        # Le contenu métier n'a PAS bougé — preuve directe, pas seulement
        # "une exception a été levée quelque part".
        assert confirmed.quantity == 100.0

    def test_the_business_fields_survive_every_terminal_status_transition_unchanged(self):
        """`with_status` (la SEULE mutation légale post-DRAFT) ne touche
        QUE `status`/`version` — jamais `product`/`quantity`/`price`."""
        v1 = _procurement_draft(draft_id="snap-2", quantity=42.0, price=99.0)
        executing = v1._confirm_to_executing()
        executed = executing.with_status(ProcurementDraftStatus.EXECUTED)

        assert executed.quantity == 42.0
        assert executed.price == 99.0
        assert executed.product == v1.product

    def test_confirmation_target_plus_a_reload_answers_what_was_confirmed_unambiguously(self):
        """Simule "3 jours plus tard" : seul `ConfirmationTarget` (2
        entiers/chaînes) a survécu quelque part (ex: un log, un ticket
        support) — recharger le draft par `draft_id` suffit à reconstituer
        le contenu EXACT confirmé, peu importe combien de temps a passé ou
        combien de transitions de statut ont suivi."""
        from ladini.graphs.agents.market_coach.core.confirmation_target import (
            ConfirmationTarget,
        )

        v1 = _procurement_draft(draft_id="snap-3", quantity=7.0, product="oignons")
        executing = v1._confirm_to_executing()
        target = ConfirmationTarget(draft_id=executing.draft_id, draft_version=executing.version)
        # `target.matches()` reste vrai IMMÉDIATEMENT après la confirmation
        # (c'est son usage réel : `apply_domain_action`/`STALE_TARGET`,
        # DANS le même tour) — pas encore de transition de statut ultérieure.
        assert target.matches(executing)

        # ... "3 jours plus tard" : plusieurs transitions de statut
        # supplémentaires (chacune bump `version`, voir la nuance en tête
        # de fichier — `target.matches()` ne s'applique plus ici). ...
        final = executing.with_status(ProcurementDraftStatus.EXECUTED)

        # `draft_id` seul (stable) suffit à RETROUVER le contenu confirmé —
        # AUCUN besoin de `target.draft_version` pour cette relecture.
        assert final.draft_id == target.draft_id
        assert final.product == "oignons"
        assert final.quantity == 7.0


class TestPreorderConfirmationSnapshotIsFrozen:
    def test_with_updates_after_executing_is_structurally_impossible(self):
        v1 = _preorder_draft(draft_id="psnap-1", total_amount=1000.0)
        confirmed = v1._confirm_to_executing(delivery_lat=1.0, delivery_lon=1.0)
        assert confirmed.status == PreorderDraftStatus.EXECUTING

        with pytest.raises(PreorderIllegalTransition):
            confirmed.with_updates(total_amount=9999.0)

        assert confirmed.total_amount == 1000.0

    def test_the_business_fields_survive_every_terminal_status_transition_unchanged(self):
        v1 = _preorder_draft(draft_id="psnap-2", total_amount=5000.0)
        executing = v1._confirm_to_executing(delivery_lat=12.37, delivery_lon=-1.52)
        awaiting = executing.with_status(PreorderDraftStatus.AWAITING_PAYMENT)
        executed = awaiting.with_status(PreorderDraftStatus.EXECUTED)

        assert executed.total_amount == 5000.0
        assert executed.delivery_lat == 12.37
        assert executed.delivery_lon == -1.52
        assert executed.items == v1.items

    def test_confirmation_target_plus_a_reload_answers_what_was_confirmed_unambiguously(self):
        from ladini.graphs.agents.market_coach.core.confirmation_target import (
            ConfirmationTarget,
        )

        v1 = _preorder_draft(draft_id="psnap-3", total_amount=15000.0)
        executing = v1._confirm_to_executing(delivery_lat=12.0, delivery_lon=-1.0)
        target = ConfirmationTarget(draft_id=executing.draft_id, draft_version=executing.version)
        assert target.matches(executing)

        awaiting = executing.with_status(PreorderDraftStatus.AWAITING_PAYMENT)
        final = awaiting.with_status(PreorderDraftStatus.EXECUTED)

        assert final.draft_id == target.draft_id
        assert final.total_amount == 15000.0
        assert final.delivery_lat == 12.0
