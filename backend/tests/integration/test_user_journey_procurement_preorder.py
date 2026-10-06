"""TEST USER-JOURNEY RÉEL — PROCUREMENT et PREORDER (2026-09-03, hardening
transverse avant migration SALES, mandat §3).

Le rapport PREORDER listait comme limite explicite l'absence d'un test qui
ne se contente pas d'appeler `apply_domain_action(...)` isolément mais
simule le VRAI enchaînement de couches :

    message utilisateur (état déjà interprété — voir note ci-dessous)
      → routing (DomainRouter.decide, RÉEL)
      → nœud/flow d'entrée RÉEL (`confirmation_gate`/`create_preorder`)
      → domain action (RÉEL, `apply_domain_action`)
      → persistance (RÉELLE, faux moteur SQL fidèle — mêmes fixtures que
        `test_procurement_draft_persistence.py`/`test_preorder_draft_persistence.py`)
      → confirmation (RÉELLE, `resolve_*_confirmation`)
      → exécution/paiement (RÉELLE logique métier, `RecordingRuntime` en
        frontière `mc_runtime.call_db` — MÊME frontière que
        `services/mcp/gateway.py::_BaseGateway._call`, MÊME convention que
        `tests/evals/runners/harness.py`, réutilisée ici plutôt que
        redéfinie)
      → réponse (RÉELLE, `build_response_plan`/`apply_response_plan`)

## Portée du "message utilisateur" (honnête, comme le reste de ce repo)

Comme TOUTE la suite de tests existante (`tests/conftest.py`,
`graph_builder.py::run_manual_smoke_tests`, `tests/evals/runners/harness.py`
— les 3 documentent la même limite explicitement), l'interpréteur LLM
lui-même n'est PAS exercé ici : il n'existe aucun moyen déterministe/hors
réseau de le faire tourner. Chaque tour part d'un état déjà "interprété"
(`interpreted_event`/`extracted_entities`/`current_goal`) — EXACTEMENT la
même frontière que `RecordingRuntime` documente pour `call_db`. Ce que ce
fichier apporte par rapport à `apply_domain_action(...)` seul : router.py,
le nœud d'entrée RÉEL (pas une fonction domain importée directement), la
persistance RÉELLE (CAS, pas un mock de `apply_domain_action`), ET la
couche paiement RÉELLE pour PREORDER (`apply_payment_outcome`, pas un
raccourci)."""
from __future__ import annotations

import sys
from pathlib import Path

_EVALS_RUNNERS = Path(__file__).resolve().parents[1] / "evals" / "runners"
if str(_EVALS_RUNNERS.parent.parent) not in sys.path:
    sys.path.insert(0, str(_EVALS_RUNNERS.parent.parent))

from ladini.core.settings import settings
from ladini.graphs.agents.market_coach.core.router import DomainRouter
from ladini.graphs.agents.market_coach.domain.preorder_draft import (
    PreorderDraftStatus,
)
from ladini.graphs.agents.market_coach.domain.procurement_draft import (
    ProcurementDraftStatus,
)
from ladini.graphs.agents.market_coach.flows.buyer.preorder import (
    create_preorder,
)
from ladini.graphs.agents.market_coach.flows.buyer.preorder_payment import (
    apply_payment_outcome,
)
from ladini.graphs.agents.market_coach.flows.buyer.procurement_execution_finalizer import (
    finalize_procurement_execution,
)
from ladini.graphs.agents.market_coach.nodes.confirmation_gate import (
    confirmation_gate,
)
from ladini.services.database import preorder_draft_store, procurement_draft_store
from tests.architecture.test_preorder_draft_persistence import (
    _install_fake_db as _install_fake_preorder_db,
)
from tests.architecture.test_procurement_draft_persistence import (
    _install_fake_db as _install_fake_procurement_db,
)
from tests.conftest import make_state, run
from tests.evals.runners.harness import RecordingRuntime


class TestProcurementUserJourney:
    """create (interpreter output) → routing → confirmation_gate (RÉEL
    nœud d'entrée) → domain action → CAS → confirm → create_auction
    (RecordingRuntime) → finalize_procurement_execution (RÉEL nœud de
    finalisation) → EXECUTED → réponse.

    LIMITE DE PORTÉE honnête (même discipline que
    `tests/evals/runners/harness.py`, qui documente la même limite pour
    `mc_runtime.call_db`) : `nodes/executor.py::mcp_tool_executor` — le
    nœud GÉNÉRIQUE qui résout `selected_tool`/valide les arguments/gère le
    retry transitoire — N'EST PAS exercé ici tel quel (sa machinerie de
    résolution d'outil est indépendante du goal et déjà testée ailleurs,
    `tests/nodes/`). Ce test simule sa SORTIE exacte (`state["status"]`
    = statut MCP brut, `state["execution_result"]` = réponse MCP brute)
    après avoir réellement appelé l'outil via `RecordingRuntime` — la
    frontière `mc_runtime.call_db` reste RÉELLEMENT franchie, seule la
    logique de résolution/retry générique de `mcp_tool_executor`
    elle-même est court-circuitée."""

    def test_full_journey_from_complete_slots_to_executed_reuses_the_confirmed_content(
        self, monkeypatch
    ):
        _install_fake_procurement_db(monkeypatch)
        router = DomainRouter.build()

        # ── Tour 1 : les champs requis viennent d'être réunis (le
        # validateur/slot-filling générique, EN AMONT de ce test — hors
        # périmètre déterministe, cf. note de portée — a produit ce
        # `transaction_payload`). ──
        turn1_state = make_state(
            user_role="BUYER",
            current_goal="PROCUREMENT_CREATE_REQUEST",
            transaction_payload={
                "product": "tomates",
                "quantity": 2000.0,
                "unit": "KG",
                "price": 250.0,
                "price_unit": "KG",
            },
        )
        # Routage RÉEL : PROCUREMENT_CREATE_REQUEST n'a pas de RouteRule
        # dédiée dans DomainRouter — vérifie juste que la décision NE
        # LÈVE PAS et reste un routage déterministe (pas un crash de la
        # couche routing avant même d'atteindre le nœud).
        route1 = router.decide(turn1_state)
        assert isinstance(route1, str) and route1

        runtime1 = RecordingRuntime()
        patch1 = run(confirmation_gate(turn1_state, runtime1))

        assert patch1["status"] == "WAITING_INPUT"
        assert patch1["response_strategy"] == "CONFIRMATION"
        draft_v1 = patch1["procurement_draft"]
        assert draft_v1["status"] == ProcurementDraftStatus.DRAFT.value
        assert draft_v1["version"] == 1
        # Persistance RÉELLE — pas seulement le patch en mémoire.
        persisted_v1 = run(procurement_draft_store.load(draft_v1["draft_id"]))
        assert persisted_v1 is not None
        assert persisted_v1.quantity == 2000.0
        assert not runtime1.calls, "la création ne doit appeler AUCUN outil MCP d'écriture"

        # ── Tour 2 : l'utilisateur confirme. ──
        turn2_state = make_state(
            user_role="BUYER",
            current_goal="PROCUREMENT_CREATE_REQUEST",
            interpreted_event="CONFIRM",
            procurement_draft=draft_v1,
            pending_interaction=patch1["pending_interaction"],
        )
        route2 = router.decide(turn2_state)
        assert isinstance(route2, str) and route2

        runtime2 = RecordingRuntime()
        patch2 = run(confirmation_gate(turn2_state, runtime2))
        assert patch2["procurement_draft"]["status"] == ProcurementDraftStatus.EXECUTING.value
        executing_draft = patch2["procurement_draft"]

        # ── "mcp_tool_executor" (frontière réellement franchie, résolution
        # d'outil générique court-circuitée — voir docstring de la classe) ──
        raw_mcp_response = run(
            runtime2.call_db(
                "create_auction",
                product=executing_draft["product"],
                quantity=executing_draft["quantity"],
                unit=executing_draft["unit"],
                price=executing_draft["price"],
            )
        )
        assert runtime2.call_names == ["create_auction"]

        turn2_state_after_exec = dict(turn2_state)
        turn2_state_after_exec.update(patch2)
        # Traduction statut brut MCP -> vocabulaire du domaine ("COMPLETED"/
        # "ERROR") — même règle que celle appliquée inline par
        # `preorder_confirmation.py::_execute_and_finalize` pour PREORDER ;
        # `adapt_mcp_result` (domaine PROCUREMENT) n'accepte QUE "COMPLETED",
        # jamais le mot "success" du transport brut directement.
        turn2_state_after_exec["status"] = (
            "COMPLETED" if str(raw_mcp_response.get("status") or "").lower() != "error" else "ERROR"
        )
        turn2_state_after_exec["execution_result"] = raw_mcp_response

        patch3 = run(finalize_procurement_execution(turn2_state_after_exec, runtime2))

        final_draft = run(procurement_draft_store.load(draft_v1["draft_id"]))
        assert final_draft is not None
        assert final_draft.status == ProcurementDraftStatus.EXECUTED
        # LE contenu exécuté est bien celui CONFIRMÉ (v2, quantity=2000.0),
        # jamais un résidu d'une version antérieure.
        assert final_draft.quantity == 2000.0
        assert patch3.get("status") == "COMPLETED"


class TestPreorderUserJourney:
    """cart (interpreter output) → routing → create_preorder (RÉEL nœud
    d'entrée) → domain action → CAS → confirm sans GPS connu → prompt GPS
    → position partagée → escrow initié (RecordingRuntime) →
    AWAITING_PAYMENT → IPN Paydunya (apply_payment_outcome, RÉEL) →
    EXECUTED."""

    def test_full_journey_from_cart_through_gps_to_escrow_awaiting_payment_to_executed(
        self, monkeypatch
    ):
        _install_fake_preorder_db(monkeypatch)
        # Chemin escrow forcé pour ce test — déterministe quel que soit
        # l'environnement (le chemin non-escrow, `confirm_preorder_draft`
        # direct, est déjà couvert par `test_preorder_transactional_contract.py`).
        monkeypatch.setattr(settings, "ESCROW_PAYMENT_ENABLED", True)
        router = DomainRouter.build()
        buyer_phone = "+22670000001"

        # ── Tour 1 : panier prêt, l'utilisateur dit "je confirme" ──
        # directement depuis le panier (chemin RÉEL documenté par
        # `create_preorder`, point 4 de sa docstring : bootstrap + tentative
        # de confirmation dans le MÊME tour).
        cart = [
            {
                "product_id": "prod-tomates",
                "name": "Tomates",
                "quantity": 50.0,
                "unit": "KG",
                "price": 300.0,
                "line_total": 15000.0,
                "producer_id": "prod-1",
                "status": "ACTIVE",
            }
        ]
        turn1_state = make_state(
            user_role="BUYER",
            user_phone=buyer_phone,
            current_goal="BUYER_PREORDER_CONFIRM",
            active_cart=cart,
            transaction_payload={"resolved_id": "PREORDER_CONFIRM"},
        )
        route1 = router.decide(turn1_state)
        assert isinstance(route1, str) and route1

        runtime1 = RecordingRuntime(
            responses={
                "create_preorder_draft": {
                    "status": "success",
                    "preorder_id": "order-journey-1",
                }
            }
        )
        patch1 = run(create_preorder(turn1_state, runtime1))

        # Aucun point de livraison connu -> NEEDS_LOCATION -> prompt GPS,
        # le draft reste DRAFT (v1), JAMAIS EXECUTING sans localisation.
        assert "create_preorder_draft" in runtime1.call_names
        draft_v1 = patch1.get("preorder_draft")
        assert draft_v1 is not None, f"draft absent du patch: {patch1}"
        assert draft_v1["status"] == PreorderDraftStatus.DRAFT.value
        order_id = draft_v1["order_id"]
        persisted_v1 = run(preorder_draft_store.load(draft_v1["draft_id"]))
        assert persisted_v1 is not None
        assert persisted_v1.status == PreorderDraftStatus.DRAFT
        assert persisted_v1.total_amount == 15000.0

        # ── Tour 2 : l'utilisateur partage sa position GPS native. ──
        turn2_state = make_state(
            user_role="BUYER",
            user_phone=buyer_phone,
            current_goal="BUYER_PREORDER_CONFIRM",
            preorder_draft=draft_v1,
            pending_interaction=patch1.get("pending_interaction"),
            location_shared=True,
            location_outcome="NEW_LOCATION_ACCEPTED",
            location_lat=12.3714,
            location_lon=-1.5197,
        )
        route2 = router.decide(turn2_state)
        assert isinstance(route2, str) and route2

        runtime2 = RecordingRuntime(
            responses={
                "initiate_escrow_payment": {
                    "status": "success",
                    "order_id": order_id,
                    "checkout_url": "https://paydunya.example/invoice/journey-1",
                    "ttl_hours": 24,
                }
            }
        )
        patch2 = run(create_preorder(turn2_state, runtime2))

        assert "initiate_escrow_payment" in runtime2.call_names
        after_gps = run(preorder_draft_store.load(draft_v1["draft_id"]))
        assert after_gps is not None
        assert after_gps.status == PreorderDraftStatus.AWAITING_PAYMENT
        assert after_gps.delivery_lat == 12.3714
        assert after_gps.delivery_lon == -1.5197
        assert after_gps.total_amount == 15000.0, "le montant escroté est celui CONFIRMÉ, pas un résidu"

        # ── "Tour" 3 : pas un message utilisateur — l'IPN Paydunya RÉEL
        # (mandat §3 : "exécution/paiement" fait partie du parcours). ──
        outcome = run(
            apply_payment_outcome(
                order_id, paydunya_status="completed", mark_paid_result={"status": "success", "order_id": order_id}
            )
        )
        assert outcome is not None
        assert outcome.draft.status == PreorderDraftStatus.EXECUTED

        final = run(preorder_draft_store.load(draft_v1["draft_id"]))
        assert final is not None
        assert final.status == PreorderDraftStatus.EXECUTED
        assert final.total_amount == 15000.0
