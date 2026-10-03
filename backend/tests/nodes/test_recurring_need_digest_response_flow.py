"""`recurring_need_flow` — réponse au digest via `UPDATE_RECURRING_NEED` + `action="CONFIRM_MATCH"|
"REJECT_MATCH"` (VS4 pilote, mandat digest). Couvre les CAS 1/2/3/4/5 du mandat ; CAS 6 (ownership)
et CAS 8 (aucun bouton sans proposition) sont couverts respectivement par
`tests/schema/test_recurring_need_confirmation_service.py` (PostgreSQL réel) et
`tests/unit/test_recurring_supply_digest.py` (rendu pur) ; CAS 7 (MODIFIER) par
`tests/interpreter/test_recurring_supply_digest_bare_confirmation.py`."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.flows.buyer.recurring_need import (
    recurring_need_flow,
)
from ladini.graphs.agents.market_coach.services.mcp.gateway import MCPCallError
from tests.conftest import StubRuntime, make_state, run

_LISTING_ONE_FULL = {
    "status": "success",
    "items": [{"recurring_need_id": "need-tomate", "product": "tomate", "matched_quantity": 40, "status": "ACTIVE", "next_occurrence_id": "occ-need-tomate", "next_occurrence_date": "2026-10-04", "next_occurrence_notified": True}],
}
_LISTING_ONE_PARTIAL = {
    "status": "success",
    "items": [{"recurring_need_id": "need-tomate", "product": "tomate", "matched_quantity": 20, "status": "ACTIVE", "next_occurrence_id": "occ-need-tomate", "next_occurrence_date": "2026-10-04", "next_occurrence_notified": True}],
}
_LISTING_TWO_FULL = {
    "status": "success",
    "items": [
        {"recurring_need_id": "need-tomate", "product": "tomate", "matched_quantity": 40, "status": "ACTIVE", "next_occurrence_id": "occ-need-tomate", "next_occurrence_date": "2026-10-04", "next_occurrence_notified": True},
        {"recurring_need_id": "need-oignon", "product": "oignon", "matched_quantity": 20, "status": "ACTIVE", "next_occurrence_id": "occ-need-oignon", "next_occurrence_date": "2026-10-04", "next_occurrence_notified": True},
    ],
}
_LISTING_NOTHING_ACTIONABLE = {
    "status": "success",
    "items": [{"recurring_need_id": "need-tomate", "product": "tomate", "matched_quantity": 0, "status": "ACTIVE", "next_occurrence_id": "occ-need-tomate", "next_occurrence_date": "2026-10-04", "next_occurrence_notified": True}],
}


def _confirm_state():
    return make_state(
        current_goal="UPDATE_RECURRING_NEED",
        transaction_payload={"action": "CONFIRM_MATCH"},
    )


def _reject_state():
    return make_state(
        current_goal="UPDATE_RECURRING_NEED",
        transaction_payload={"action": "REJECT_MATCH"},
    )


# ── CAS 1 : confirmer une proposition ouverte -> commande créée ────────────

def test_cas1_confirm_with_one_actionable_need_calls_accept_for_that_need():
    seen = []

    def _accept(**kwargs):
        seen.append(kwargs)
        return {"status": "success", "action": "ACCEPT", "order_ids": ["order-1"]}

    runtime = StubRuntime(
        responses={"list_my_recurring_needs": _LISTING_ONE_FULL, "accept_match_proposal": _accept}
    )
    result = run(recurring_need_flow(_confirm_state(), runtime))

    # (2026-09-26, mandat "mismatch CONFIRM vs ACCEPT") : `accept_match_proposal` ne parle que le
    # vocabulaire `MATCH_RESPONSE_ACTIONS = ("ACCEPT", "REJECT")` — "CONFIRM" (verrouillé ici
    # auparavant) n'a jamais été une valeur valide côté service réel.
    # B11-recurring : la réponse vise l'occurrence EXACTE notifiée, jamais « la prochaine ouverte ».
    assert seen == [{"phone": "+22670000000", "recurring_need_id": "need-tomate", "action": "ACCEPT",
                     "occurrence_id": "occ-need-tomate"}]
    assert "confirmé" in result["final_response"].lower()
    assert result["status"] == "COMPLETED"


def test_cas1_confirm_with_two_actionable_needs_calls_accept_for_each():
    calls = []

    def _accept(**kwargs):
        calls.append(kwargs["recurring_need_id"])
        return {"status": "success", "action": "ACCEPT", "order_ids": [f"order-{kwargs['recurring_need_id']}"]}

    runtime = StubRuntime(
        responses={"list_my_recurring_needs": _LISTING_TWO_FULL, "accept_match_proposal": _accept}
    )
    result = run(recurring_need_flow(_confirm_state(), runtime))

    assert set(calls) == {"need-tomate", "need-oignon"}  # CONFIRMER TOUT — les DEUX besoins
    assert result["status"] == "COMPLETED"


# ── CAS 2 : "pas cette fois" -> aucune commande ────────────────────────────

def test_cas2_reject_never_calls_accept_with_confirm_action():
    seen_actions = []

    def _accept(**kwargs):
        seen_actions.append(kwargs["action"])
        return {"status": "success", "action": "REJECT"}

    runtime = StubRuntime(
        responses={"list_my_recurring_needs": _LISTING_ONE_FULL, "accept_match_proposal": _accept}
    )
    result = run(recurring_need_flow(_reject_state(), runtime))

    assert seen_actions == ["REJECT"]
    assert "rien ne sera livré" in result["final_response"].lower()
    assert "commande" not in result["final_response"].lower()


# ── CAS 3 : proposition partielle -> confirmée telle quelle ────────────────

def test_cas3_confirming_a_partial_match_still_calls_accept_normally():
    """La règle "seules les quantités proposées sont commandées" est appliquée par
    `accept_match_proposal` lui-même (voir test_recurring_need_confirmation_service.py) — ce test
    prouve seulement que le flow ne bloque ni ne modifie rien pour un besoin partiel."""
    seen = []

    def _accept(**kwargs):
        seen.append(kwargs)
        return {"status": "success", "action": "ACCEPT", "order_ids": ["order-1"], "quantity_confirmed": 20}

    runtime = StubRuntime(
        responses={"list_my_recurring_needs": _LISTING_ONE_PARTIAL, "accept_match_proposal": _accept}
    )
    result = run(recurring_need_flow(_confirm_state(), runtime))

    assert len(seen) == 1 and seen[0]["action"] == "ACCEPT"
    assert "confirmé" in result["final_response"].lower()


# ── CAS 4 : double CONFIRMER -> pas de double commande ─────────────────────

def test_cas4_a_second_confirm_on_an_already_processed_need_never_claims_success():
    """Simule le 2e appel réel : `accept_match_proposal` lève (occurrence déjà ACCEPTED, hors
    OPEN/MATCHED — voir le service) — le flow ne doit JAMAIS prétendre avoir confirmé."""

    def _already_processed(**kwargs):
        raise MCPCallError(
            tool="accept_match_proposal", message="Aucune proposition en attente pour ce besoin.",
            error_code="BUSINESS_RULE", request_id="n/a",
        )

    runtime = StubRuntime(
        responses={"list_my_recurring_needs": _LISTING_ONE_FULL, "accept_match_proposal": _already_processed}
    )
    result = run(recurring_need_flow(_confirm_state(), runtime))

    assert "n'ai pas pu confirmer" in result["final_response"].lower()
    assert "result" in result
    assert result["result"]["confirmed"] == []
    assert result["result"]["skipped"] == ["tomate"]


# ── CAS 5 : réponse tardive, rien d'actionnable -> aucune commande tentée ──

def test_cas5_nothing_actionable_never_calls_accept_at_all():
    # Aucune réponse stubée pour accept_match_proposal : si le code l'appelait quand même,
    # StubRuntime lèverait — la réussite du test prouve que l'appel n'a jamais eu lieu.
    runtime = StubRuntime(responses={"list_my_recurring_needs": _LISTING_NOTHING_ACTIONABLE})
    result = run(recurring_need_flow(_confirm_state(), runtime))

    assert "rien à confirmer" in result["final_response"].lower()
    assert result["status"] == "COMPLETED"


def test_cas5_no_recurring_need_at_all_also_never_calls_accept():
    runtime = StubRuntime(responses={"list_my_recurring_needs": {"status": "success", "items": []}})
    result = run(recurring_need_flow(_confirm_state(), runtime))

    assert "rien à confirmer" in result["final_response"].lower()
