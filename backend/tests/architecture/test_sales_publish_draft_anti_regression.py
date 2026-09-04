"""ANTI-RÉGRESSION — reproduction du bug historique PROCUREMENT sous SALES
(2026-09-04, migration SALES, mandat §20).

Bug original (voir `domain/procurement_draft.py`, docstring module) :
`quantity`/`unit` (contrat d'exécution) et `quantity_display`/`unit_display`
(texte affiché) étaient deux représentations INDÉPENDANTES du même fait
métier — une correction utilisateur pouvait mettre à jour l'AFFICHAGE sans
que l'EXÉCUTION ne suive (ou l'inverse), parce que rien ne garantissait
qu'elles restent synchronisées.

Scénario verrouillé ici, exactement comme demandé par le mandat :

    valeur A (prix=250)
    → correction B (prix=275)
    → correction C (prix=300)
    → confirmation

    Résultat attendu, SANS AMBIGUÏTÉ :
        réponse = C
        draft   = C
        confirmation_target = C
        exécution = C

    JAMAIS :
        affichage C + exécution B
        affichage B + exécution C

`SalesPublishDraft` rend cette classe de bug STRUCTURELLEMENT impossible
(pas seulement "corrigée par un patch ponctuel") : `render_summary()` est
une PROJECTION PURE du MÊME objet immuable que celui qui produit
`execution_payload()` — il n'existe, PAR CONSTRUCTION, aucun second endroit
où une valeur pourrait diverger."""
from __future__ import annotations

from tests.conftest import make_state, run
from tests.architecture.test_sales_publish_draft_persistence import _install_fake_db

from agriconnect.graphs.agents.market_coach.domain.sales_publish_draft import (
    SalesPublishDraftStatus,
)
from agriconnect.graphs.agents.market_coach.nodes.confirmation_gate import (
    confirmation_gate,
)
from agriconnect.services.database import sales_publish_draft_store


class TestHistoricalDisplayExecutionMismatchCannotReoccur:
    def test_value_a_then_correction_b_then_correction_c_then_confirm_uses_exactly_c(
        self, monkeypatch
    ):
        _install_fake_db(monkeypatch)

        # ── Valeur A : publication initiale, prix=250. ──
        turn1 = make_state(
            user_role="PRODUCER",
            current_goal="SALES_PUBLISH_PRODUCT",
            transaction_payload={"product": "Maïs blanc", "quantity": 100.0, "unit": "KG", "price": 250.0},
        )
        patch1 = run(confirmation_gate(turn1, None))
        draft_a = patch1["sales_publish_draft"]
        assert draft_a["price"] == 250.0
        assert draft_a["version"] == 1
        assert "250" in patch1["final_response"]

        # ── Correction B : prix=275 (l'utilisateur se ravise une 1re fois). ──
        turn2 = make_state(
            user_role="PRODUCER",
            current_goal="SALES_PUBLISH_PRODUCT",
            sales_publish_draft=draft_a,
            pending_interaction=patch1["pending_interaction"],
            interpreted_event="UPDATE",
            extracted_entities={"price": 275.0},
        )
        # Le domaine reçoit l'update via `resolve_sales_confirmation` — mais
        # `confirmation_gate` ne route un draft déjà existant que sur
        # `sales_publish_draft` déjà posé (voir dispatch) : simule le
        # 2e tour réel en appelant directement le résolveur, comme le ferait
        # `_resolve_sales_draft_based_confirmation`.
        from agriconnect.graphs.agents.market_coach.flows.producer.sales_confirmation import (
            resolve_sales_confirmation,
        )

        patch2 = run(resolve_sales_confirmation(turn2, None))
        draft_b = patch2["sales_publish_draft"]
        assert draft_b["price"] == 275.0
        assert draft_b["version"] == 2
        assert "275" in patch2["final_response"]
        assert "250" not in patch2["final_response"].split("\n\n")[0], (
            "l'AFFICHAGE après la correction B ne doit plus montrer 250"
        )

        # ── Correction C : prix=300 (l'utilisateur se ravise une 2e fois). ──
        turn3 = make_state(
            user_role="PRODUCER",
            current_goal="SALES_PUBLISH_PRODUCT",
            sales_publish_draft=draft_b,
            pending_interaction=patch2["pending_interaction"],
            interpreted_event="UPDATE",
            extracted_entities={"price": 300.0},
        )
        patch3 = run(resolve_sales_confirmation(turn3, None))
        draft_c = patch3["sales_publish_draft"]
        assert draft_c["price"] == 300.0
        assert draft_c["version"] == 3
        assert "300" in patch3["final_response"]

        # ── Confirmation — DOIT utiliser EXACTEMENT C (300), jamais A (250)
        # ni B (275). ──
        turn4 = make_state(
            user_role="PRODUCER",
            current_goal="SALES_PUBLISH_PRODUCT",
            sales_publish_draft=draft_c,
            pending_interaction=patch3["pending_interaction"],
            interpreted_event="CONFIRM",
        )
        patch4 = run(resolve_sales_confirmation(turn4, None))

        # RÉPONSE = C (le patch de confirmation ne porte pas de texte de
        # récap lui-même — c'est le pipeline d'exécution générique qui
        # rendra la réponse finale à partir de `transaction_payload`, posé
        # ci-dessous — mais le DRAFT persisté au moment de la confirmation
        # DOIT déjà porter C).
        assert patch4["sales_publish_draft"]["status"] == SalesPublishDraftStatus.EXECUTING.value
        assert patch4["sales_publish_draft"]["price"] == 300.0
        assert patch4["sales_publish_draft"]["version"] == 4

        # EXÉCUTION = C : `transaction_payload` (ce que `mcp_tool_executor`
        # va réellement envoyer à `create_product`) porte EXACTEMENT le
        # contenu du draft confirmé — jamais un résidu de A/B.
        assert patch4["execution_authorized"] is True
        assert patch4["transaction_payload"]["price"] == 300.0
        assert patch4["transaction_payload"]["product"] == "Maïs blanc"

        # CONFIRMATION_TARGET = C : la persistance canonique reflète EXACTEMENT
        # ce que la confirmation a visé — pas A, pas B.
        persisted = run(sales_publish_draft_store.load(draft_c["draft_id"]))
        assert persisted is not None
        assert persisted.price == 300.0
        assert persisted.status == SalesPublishDraftStatus.EXECUTING

        # Jamais l'inverse : A/B ne sont PLUS atteignables — une tentative de
        # confirmer A ou B (cible périmée) est rejetée, jamais exécutée.
        from agriconnect.graphs.agents.market_coach.domain.sales_publish_draft import (
            ConfirmSalesPublishDraft,
            SalesPublishDraft,
            SalesPublishOutcomeKind,
            apply_domain_action,
        )
        from agriconnect.graphs.agents.market_coach.core.confirmation_target import (
            ConfirmationTarget,
        )

        current = SalesPublishDraft.from_dict(patch4["sales_publish_draft"])
        for stale_version, stale_price in ((1, 250.0), (2, 275.0), (3, 300.0)):
            stale_target = ConfirmationTarget(draft_id=current.draft_id, draft_version=stale_version)
            outcome = apply_domain_action(current, ConfirmSalesPublishDraft(target=stale_target))
            # v3 (300, la version qui a RÉELLEMENT été confirmée) retombe
            # sur ALREADY_EXECUTING (c'est la version courante — déjà en
            # cours), jamais une ré-exécution ; v1/v2 sont carrément
            # périmées mais le résultat de sûreté est le même : aucune
            # nouvelle exécution n'est déclenchée dans tous les cas.
            assert outcome.kind in (
                SalesPublishOutcomeKind.ALREADY_EXECUTING,
                SalesPublishOutcomeKind.STALE_TARGET,
            )
            assert outcome.draft.status == SalesPublishDraftStatus.EXECUTING
            assert outcome.draft.price == 300.0, (
                f"une tentative de confirmer v{stale_version} (prix historique "
                f"{stale_price}) ne doit JAMAIS faire réapparaître ce prix"
            )
