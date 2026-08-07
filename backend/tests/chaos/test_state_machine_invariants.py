"""CHAOS 4 — Invariants de la machine à états (tolérance zéro).

Les trois invariants qui protègent l'argent et la confiance :
  I1. AUCUNE écriture sans confirmation explicite de l'utilisateur.
  I2. AUCUNE exécution sans autorisation posée par confirmation_gate.
  I3. AUCUN résidu de réponse/exécution ne fuit d'un tour vers le suivant.
Prouvés sur l'INTÉGRALITÉ du catalogue d'intents, pas sur un échantillon.
"""
from __future__ import annotations

import pytest

from conftest import RecordingRuntime, run


# =====================================================================
# I1 — Toute écriture exige une confirmation explicite (catalogue ENTIER)
# =====================================================================

def _write_goals():
    from agriconnect.graphs.agents.market_coach.core.base import _WRITE_GOALS
    return sorted(_WRITE_GOALS)


def _read_goals():
    from agriconnect.graphs.agents.market_coach.core.base import _READ_GOALS
    return sorted(_READ_GOALS)


@pytest.mark.parametrize("goal", _write_goals())
def test_no_write_executes_without_explicit_confirm(goal):
    """Rupture prévenue : une vente/enchère/commande exécutée sans que
    l'utilisateur ait dit « oui ». Pour CHAQUE intent WRITE du catalogue :
    le premier passage dans confirmation_gate doit parquer la transaction
    (WAITING_CONFIRMATION) avec execution_authorized=False — même si un
    état amont hostile prétend le contraire."""
    from agriconnect.graphs.agents.market_coach.nodes.confirmation_gate import (
        confirmation_gate,
    )

    state = {
        "current_goal": goal,
        "transaction_payload": {"product": "maïs", "quantity": 50, "price": 250},
        "interpreted_event": "NEW_TASK",   # PAS un CONFIRM
        "execution_authorized": True,      # forgé — doit être écrasé
    }
    out = run(confirmation_gate(state, None))
    assert out["status"] == "WAITING_CONFIRMATION", f"{goal}: écriture non parquée"
    assert out["execution_authorized"] is False, f"{goal}: autorisation forgée non écrasée"
    assert out.get("confirmation_summary"), f"{goal}: récap absent — confirmation aveugle"


@pytest.mark.parametrize("goal", _write_goals())
def test_confirm_event_authorizes_exactly_once(goal):
    """Rupture prévenue : le « oui » de l'utilisateur ignoré (re-demande en
    boucle — bug historique du canal de confirmation). CONFIRM en attente
    de confirmation doit basculer en EXECUTING/authorized."""
    from agriconnect.graphs.agents.market_coach.nodes.confirmation_gate import (
        confirmation_gate,
    )

    state = {
        "current_goal": goal,
        "transaction_payload": {"product": "maïs", "quantity": 50, "price": 250},
        "interpreted_event": "CONFIRM",
        "waiting_for_confirmation": True,
    }
    out = run(confirmation_gate(state, None))
    assert out["status"] == "EXECUTING" and out["execution_authorized"] is True, goal


@pytest.mark.parametrize("goal", _write_goals())
def test_reject_cancels_and_purges_payload(goal):
    """Rupture prévenue : l'utilisateur dit « non » mais le payload survit et
    ré-alimente la transaction suivante (fuite inter-tunnel, source n°1 des
    bugs de re-ask). REJECT doit purger via le sentinel __reset__."""
    from agriconnect.graphs.agents.market_coach.nodes.confirmation_gate import (
        confirmation_gate,
    )

    state = {
        "current_goal": goal,
        "transaction_payload": {"product": "maïs", "quantity": 50},
        "interpreted_event": "REJECT",
        "waiting_for_confirmation": True,
    }
    out = run(confirmation_gate(state, None))
    assert out["execution_authorized"] is False
    assert out["transaction_payload"] == {"__reset__": True}, f"{goal}: payload non purgé"
    assert out["current_goal"] is None, f"{goal}: goal non déverrouillé après refus"


@pytest.mark.parametrize("goal", _read_goals())
def test_reads_never_blocked_by_confirmation(goal):
    """Symétrique anti-friction : une LECTURE ne doit JAMAIS exiger de
    confirmation (demander « confirmez-vous ? » pour voir son stock = UX
    insupportable sur WhatsApp). Auto-pass exigé pour tout READ."""
    from agriconnect.graphs.agents.market_coach.nodes.confirmation_gate import (
        confirmation_gate,
    )

    out = run(confirmation_gate({"current_goal": goal, "transaction_payload": {}}, None))
    assert out["status"] == "EXECUTING" and out["execution_authorized"] is True, goal


# =====================================================================
# I2 — L'exécuteur refuse tout état non autorisé
# =====================================================================

def test_executor_refuses_unauthorized_state_with_zero_db_calls():
    """Rupture prévenue : un chemin de graphe bugué (ou un état restauré
    corrompu) atteint l'exécuteur sans passer par confirmation_gate.
    L'exécuteur doit refuser ET ne jamais toucher la couche outil."""
    from agriconnect.graphs.agents.market_coach.nodes.executor import mcp_tool_executor

    rt = RecordingRuntime()
    state = {
        "user_phone": "+22670000000",
        "user_role": "PRODUCER",
        "current_goal": "SALES_PUBLISH_PRODUCT",
        "execution_authorized": False,   # ← le point du test
        "transaction_payload": {"product": "maïs", "quantity": 50, "price": 250},
    }
    result = run(mcp_tool_executor(state, rt))
    assert result["status"] == "ERROR"
    assert "execution_not_authorized" in (result.get("validation_errors") or [])
    assert rt.calls == [], "outil appelé sans autorisation = écriture sauvage"


def test_executor_refuses_empty_goal():
    """Rupture prévenue : goal effacé par un nettoyage trop agressif juste
    avant l'exécution → l'exécuteur doit rendre ERROR propre (no_goal),
    jamais un RuntimeError de registry."""
    from agriconnect.graphs.agents.market_coach.nodes.executor import mcp_tool_executor

    rt = RecordingRuntime()
    result = run(mcp_tool_executor(
        {"execution_authorized": True, "current_goal": "", "transaction_payload": {}}, rt,
    ))
    assert result["status"] == "ERROR"
    assert rt.calls == []


# =====================================================================
# I3 — Frontière de tour : aucun résidu ne fuit
# =====================================================================

def test_turn_boundary_resets_response_channel():
    """Rupture prévenue : la réponse du tour N relue au tour N+1 (question
    « figée » — bug réel trouvé le 2026-07-21 : post_response_cleanup avait
    dérivé de CLEANABLE_AFTER_RESPONSE, jamais consommé). Le patch de fin de
    tour doit remettre à None TOUT le canal de réponse."""
    from agriconnect.graphs.agents.market_coach.nodes.cleanup import (
        post_response_cleanup,
    )

    state = {
        "final_response": "Ancienne question ?",
        "ag_ui_component": {"lc_type": "constructor"},
        "response_strategy": "ASK_MISSING_FIELD",
        "onboarding_prompt": "vieux prompt",
        "reply_audio_url": "https://x/a.mp3",
        "status": "WAITING_INPUT",
        "expected_input": "PRICE",
    }
    patch = run(post_response_cleanup(state, None))
    for field in ("final_response", "ag_ui_component", "response_strategy",
                  "onboarding_prompt", "reply_audio_url"):
        assert patch.get(field) is None, f"résidu inter-tour: {field}"


def test_turn_boundary_preserves_confirmation_channel():
    """Rupture prévenue : le nettoyage de fin de tour qui efface le canal de
    confirmation pendant WAITING_CONFIRMATION — le « oui » du tour suivant
    ne déclencherait plus rien et la gate re-demanderait à l'infini."""
    from agriconnect.graphs.agents.market_coach.nodes.cleanup import (
        post_response_cleanup,
    )

    state = {
        "status": "WAITING_CONFIRMATION",
        "waiting_for_confirmation": True,
        "confirmation_summary": "Vente de maïs — 50 KG — 250 FCFA",
        "final_response": "Confirmez-vous ?",
    }
    patch = run(post_response_cleanup(state, None))
    assert "waiting_for_confirmation" not in patch, "canal de confirmation détruit"
    assert "confirmation_summary" not in patch, "récap détruit avant le « oui »"
    assert patch.get("final_response") is None, "le texte, lui, doit être nettoyé"


def test_terminal_goal_flushes_transaction_state():
    """Rupture prévenue : slots du goal terminé (quantity=225…) qui fuient
    dans la demande suivante sans rapport (« je veux des tomates » reprenant
    une enchère morte). Statut terminal ⇒ purge sentinel du payload."""
    from agriconnect.graphs.agents.market_coach.nodes.cleaner import state_cleaner_node

    for terminal in ("COMPLETED", "FAILED", "ERROR"):
        patch = run(state_cleaner_node({
            "status": terminal,
            "current_goal": "SALES_PUBLISH_PRODUCT",
            "transaction_payload": {"product": "maïs", "quantity": 225},
            "working_memory": {"active_goal": "SALES_PUBLISH_PRODUCT", "locked_intent": "SALES_PUBLISH_PRODUCT"},
        }, None))
        assert patch.get("transaction_payload") == {"__reset__": True}, terminal
        wm = patch.get("working_memory") or {}
        assert wm.get("active_goal") is None and wm.get("locked_intent") is None, terminal


# =====================================================================
# Anti-drift : les sources de vérité restent alignées
# =====================================================================

def test_goal_sets_and_registry_integrity():
    """Rupture prévenue : dérive silencieuse entre INTENT_CONFIG, le registre
    d'actions et les goal-sets — la classe de bugs que les Phases 1/2 ont
    éliminée. Ces validateurs lèvent à l'import ; on les exécute explicitement
    pour qu'un drift casse la CI ici, pas en prod."""
    from agriconnect.graphs.agents.market_coach.core.base import validate_config_drift
    from agriconnect.graphs.agents.market_coach.core.goals import _validate_goal_drift
    from agriconnect.graphs.agents.market_coach.registry import validate_integrity

    validate_integrity()
    validate_config_drift()
    _validate_goal_drift()


def test_both_graphs_compile_with_dead_runtime():
    """Rupture prévenue : régression de câblage (nœud/edge manquant) qui ne
    se verrait qu'au premier message réel. Les DEUX graphes doivent compiler
    même avec un runtime minimal — c'est le smoke ultime de déploiement."""
    from agriconnect.graphs.agents.market_coach.core.graph_builder import build_graph

    class _Dead:
        async def call_db(self, *a, **k):
            raise ConnectionError("chaos")

    for role in ("BUYER", "PRODUCER"):
        app = build_graph(role, mc_runtime=_Dead())
        assert app is not None
