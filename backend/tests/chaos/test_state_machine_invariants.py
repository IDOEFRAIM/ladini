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
    from ladini.graphs.agents.market_coach.core.base import _WRITE_GOALS
    return sorted(_WRITE_GOALS)


def _read_goals():
    from ladini.graphs.agents.market_coach.core.base import _READ_GOALS
    return sorted(_READ_GOALS)


# PROCUREMENT_CREATE_REQUEST (2026-09-03) et SALES_PUBLISH_PRODUCT
# (2026-09-04) : ces 2 goals ont désormais leur PROPRE cycle de confirmation
# versionné (draft canonique + `ConfirmationTarget` lié à une version
# précise — `domain/procurement_draft.py`/`domain/sales_publish_draft.py`),
# au lieu du mécanisme générique `confirmation_summary`/`transaction_payload`
# de `confirmation_gate.py`. I1/I2/I3 restent TOUS garantis pour ces goals —
# vérifiés séparément ci-dessous avec le VRAI contrat actuel
# (`pending_interaction` CONFIRM_ACTION, draft versionné), plutôt que le
# contrat générique (`confirmation_summary`, `WAITING_CONFIRMATION`) qui ne
# s'applique plus à eux. Exclus des 3 parametrizations génériques ci-dessous
# pour cette raison.
#
# (2026-09-04, hardening transverse) : ce frozenset était auparavant
# DUPLIQUÉ localement (`{"PROCUREMENT_CREATE_REQUEST"}`, jamais mis à jour
# quand SALES a migré) — c'est CE genre de duplication silencieuse que
# l'audit transverse a précisément pour but de fermer. Importé directement
# depuis `confirmation_gate.py` : une seule source de vérité, ne peut plus
# dériver.
from ladini.graphs.agents.market_coach.nodes.confirmation_gate import (
    _DRAFT_BASED_CONFIRMATION_GOALS,
)


@pytest.mark.parametrize(
    "goal", [g for g in _write_goals() if g not in _DRAFT_BASED_CONFIRMATION_GOALS]
)
def test_no_write_executes_without_explicit_confirm(goal):
    """Rupture prévenue : une vente/enchère/commande exécutée sans que
    l'utilisateur ait dit « oui ». Pour CHAQUE intent WRITE du catalogue :
    le premier passage dans confirmation_gate doit parquer la transaction
    (WAITING_CONFIRMATION) avec execution_authorized=False — même si un
    état amont hostile prétend le contraire."""
    from ladini.graphs.agents.market_coach.nodes.confirmation_gate import (
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


@pytest.mark.parametrize(
    "goal", [g for g in _write_goals() if g not in _DRAFT_BASED_CONFIRMATION_GOALS]
)
def test_confirm_event_authorizes_exactly_once(goal):
    """Rupture prévenue : le « oui » de l'utilisateur ignoré (re-demande en
    boucle — bug historique du canal de confirmation). CONFIRM en attente
    de confirmation doit basculer en EXECUTING/authorized."""
    from ladini.graphs.agents.market_coach.nodes.confirmation_gate import (
        confirmation_gate,
    )

    # `confirmation_summary_goal`/`confirmation_summary_payload` — snapshot
    # GELÉ que `confirmation_gate` aurait posé en levant CETTE confirmation
    # (2026-09-28, hardening P1-A, invariant "confirmé == exécuté") : le
    # chemin CONFIRM vérifie désormais que ce snapshot correspond au but/
    # payload courants avant de certifier — cette fixture doit donc
    # représenter un état RÉALISTE (comme si `confirmation_gate` avait déjà
    # levé cette confirmation au tour précédent), pas un raccourci qui
    # omettrait le snapshot que la garde vérifie.
    payload = {"product": "maïs", "quantity": 50, "price": 250}
    state = {
        "current_goal": goal,
        "transaction_payload": dict(payload),
        "interpreted_event": "CONFIRM",
        "waiting_for_confirmation": True,
        "confirmation_summary_goal": goal,
        "confirmation_summary_payload": dict(payload),
    }
    out = run(confirmation_gate(state, None))
    assert out["status"] == "EXECUTING" and out["execution_authorized"] is True, goal


@pytest.mark.parametrize("goal", sorted(_DRAFT_BASED_CONFIRMATION_GOALS))
def test_no_write_executes_without_explicit_confirm_draft_based_goal(goal):
    """Équivalent I1 pour un goal à draft versionné — contrat RÉEL actuel :
    `WAITING_INPUT`/pending_interaction CONFIRM_ACTION au lieu de
    `WAITING_CONFIRMATION`/`confirmation_summary`. Deux phases couvertes :
    (a) collecte encore incomplète, (b) champs réunis, draft v1 bootstrapé
    — dans les DEUX cas, un `execution_authorized` forgé en amont doit être
    écrasé à False (voir le correctif ajouté dans `_resolve_draft_based_confirmation`)."""
    from ladini.graphs.agents.market_coach.nodes.confirmation_gate import (
        confirmation_gate,
    )

    # (a) Payload encore incomplet (pas de `unit`/`price_unit`) — aucun
    # draft ne peut encore être créé.
    incomplete_state = {
        "current_goal": goal,
        "transaction_payload": {"product": "maïs", "quantity": 50, "price": 250},
        "interpreted_event": "NEW_TASK",
        "execution_authorized": True,  # forgé — doit être écrasé
    }
    out_incomplete = run(confirmation_gate(incomplete_state, None))
    assert out_incomplete["execution_authorized"] is False, (
        f"{goal}: autorisation forgée non écrasée pendant la collecte"
    )
    assert out_incomplete.get("is_certified") is False

    # (b) Payload complet — le draft v1 est bootstrapé, parqué en attente
    # d'une confirmation explicite (jamais exécuté au premier passage).
    complete_state = {
        "current_goal": goal,
        "transaction_payload": {
            "product": "maïs", "quantity": 50, "unit": "KG",
            "price": 250, "price_unit": "KG",
        },
        "interpreted_event": "NEW_TASK",
        "execution_authorized": True,  # forgé — doit être écrasé
    }
    out_complete = run(confirmation_gate(complete_state, None))
    assert out_complete["status"] == "WAITING_INPUT", f"{goal}: draft non parqué"
    assert out_complete["execution_authorized"] is False, (
        f"{goal}: autorisation forgée non écrasée au bootstrap du draft"
    )
    assert out_complete["pending_interaction"]["kind"] == "CONFIRM_ACTION"
    assert out_complete.get("final_response"), f"{goal}: récap absent — confirmation aveugle"


_DRAFT_BASED_GOAL_FIXTURE = {
    # goal -> (state_key, draft_module_path, draft_class_name, extra_new_kwargs)
    "PROCUREMENT_CREATE_REQUEST": (
        "procurement_draft",
        "ladini.graphs.agents.market_coach.domain.procurement_draft",
        "ProcurementDraft",
        {"product": "maïs", "quantity": 50.0, "unit": "KG", "price": 250.0, "price_unit": "KG"},
    ),
    "SALES_PUBLISH_PRODUCT": (
        "sales_publish_draft",
        "ladini.graphs.agents.market_coach.domain.sales_publish_draft",
        "SalesPublishDraft",
        {"product": "maïs", "quantity": 50.0, "unit": "KG", "price": 250.0},
    ),
}


@pytest.mark.parametrize("goal", sorted(_DRAFT_BASED_CONFIRMATION_GOALS))
def test_confirm_event_authorizes_exactly_once_draft_based_goal(goal, monkeypatch):
    """Équivalent I2 pour un goal à draft versionné — CONFIRM contre la
    cible EXACTE du draft en attente doit basculer en EXECUTING/authorized,
    exactement comme le contrat générique pour les autres goals. Dispatché
    par goal (mandat hardening : PROCUREMENT/PREORDER/SALES restent des
    domaines séparés, pas une abstraction commune prématurée — voir
    `_DRAFT_BASED_GOAL_FIXTURE`, une table de données pas de code partagé)."""
    import importlib

    from ladini.graphs.agents.market_coach.nodes.confirmation_gate import (
        confirmation_gate,
    )

    assert goal in _DRAFT_BASED_GOAL_FIXTURE, f"pas de fixture pour {goal} — l'ajouter ci-dessus"
    state_key, module_path, class_name, new_kwargs = _DRAFT_BASED_GOAL_FIXTURE[goal]
    draft_mod = importlib.import_module(module_path)
    draft_cls = getattr(draft_mod, class_name)

    # `claim_once` réel tape Redis (voir tests/architecture/
    # test_procurement_draft_transactional_contract.py pour la même note) —
    # neutralisé ici, le test verrouille I2, pas l'idempotence Redis.
    monkeypatch.setattr(draft_mod, "claim_once", lambda key: True)

    v1 = draft_cls.new(draft_id="chaos-confirm-1", **new_kwargs)
    state = {
        "current_goal": goal,
        state_key: v1.to_dict(),
        "interpreted_event": "CONFIRM",
        "extracted_entities": {},
        "pending_interaction": {
            "kind": "CONFIRM_ACTION",
            "target": {"draft_id": v1.draft_id, "draft_version": v1.version},
        },
    }
    out = run(confirmation_gate(state, None))
    assert out["status"] == "EXECUTING" and out["execution_authorized"] is True, goal


@pytest.mark.parametrize("goal", [g for g in _write_goals() if g not in _DRAFT_BASED_CONFIRMATION_GOALS])
def test_reject_cancels_and_purges_payload(goal):
    """Rupture prévenue : l'utilisateur dit « non » mais le payload survit et
    ré-alimente la transaction suivante (fuite inter-tunnel, source n°1 des
    bugs de re-ask). REJECT doit purger via le sentinel __reset__."""
    from ladini.graphs.agents.market_coach.nodes.confirmation_gate import (
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


@pytest.mark.parametrize("goal", sorted(_DRAFT_BASED_CONFIRMATION_GOALS))
def test_reject_preserves_the_draft_but_still_blocks_execution(goal):
    """Exception scopée à l'invariant ci-dessus : le payload survit
    intentionnellement (pour permettre une correction), mais I1/I2 restent
    absolus — jamais d'exécution/autorisation sur un REJECT, préservé ou non."""
    from ladini.graphs.agents.market_coach.nodes.confirmation_gate import (
        confirmation_gate,
    )

    state = {
        "current_goal": goal,
        "transaction_payload": {"product": "carottes", "quantity": 500, "price": 300},
        "interpreted_event": "REJECT",
        "waiting_for_confirmation": True,
    }
    out = run(confirmation_gate(state, None))
    assert out["execution_authorized"] is False, f"{goal}: REJECT ne doit jamais autoriser l'exécution"
    assert out["is_certified"] is False
    assert "transaction_payload" not in out, f"{goal}: le brouillon doit survivre intact (pas de clé = préservé par merge_dict)"
    assert "current_goal" not in out, f"{goal}: le goal doit survivre pour permettre la correction au tour suivant"


@pytest.mark.parametrize("goal", _read_goals())
def test_reads_never_blocked_by_confirmation(goal):
    """Symétrique anti-friction : une LECTURE ne doit JAMAIS exiger de
    confirmation (demander « confirmez-vous ? » pour voir son stock = UX
    insupportable sur WhatsApp). Auto-pass exigé pour tout READ."""
    from ladini.graphs.agents.market_coach.nodes.confirmation_gate import (
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
    from ladini.graphs.agents.market_coach.nodes.executor import mcp_tool_executor

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
    from ladini.graphs.agents.market_coach.nodes.executor import mcp_tool_executor

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
    from ladini.graphs.agents.market_coach.nodes.cleanup import (
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
    from ladini.graphs.agents.market_coach.nodes.cleanup import (
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
    from ladini.graphs.agents.market_coach.nodes.cleaner import state_cleaner_node

    for terminal in ("COMPLETED", "FAILED", "ERROR"):
        patch = run(state_cleaner_node({
            "status": terminal,
            "current_goal": "SALES_PUBLISH_PRODUCT",
            "transaction_payload": {"product": "maïs", "quantity": 225},
            "working_memory": {"active_goal": "SALES_PUBLISH_PRODUCT"},
        }, None))
        assert patch.get("transaction_payload") == {"__reset__": True}, terminal
        wm = patch.get("working_memory") or {}
        assert wm.get("active_goal") is None, terminal


def test_error_or_failed_goal_leaves_a_one_turn_hint_for_the_clarification_fallback():
    """Rupture prévenue : après un goal en échec, le fallback générique
    (render_clarification) répondait comme si l'utilisateur était un
    inconnu — voir [[market-coach-turn-boundary-state]]. ERROR/FAILED
    laissent `last_terminated_goal` ; COMPLETED (succès, rien à excuser) ne
    le fait pas."""
    from ladini.graphs.agents.market_coach.nodes.cleaner import state_cleaner_node

    for terminal in ("ERROR", "FAILED"):
        patch = run(state_cleaner_node({
            "status": terminal,
            "current_goal": "PROCUREMENT_CREATE_REQUEST",
            "transaction_payload": {},
            "working_memory": {},
        }, None))
        assert patch.get("last_terminated_goal") == "PROCUREMENT_CREATE_REQUEST", terminal

    patch = run(state_cleaner_node({
        "status": "COMPLETED",
        "current_goal": "PROCUREMENT_CREATE_REQUEST",
        "transaction_payload": {},
        "working_memory": {},
    }, None))
    assert "last_terminated_goal" not in patch


# =====================================================================
# Anti-drift : les sources de vérité restent alignées
# =====================================================================

def test_goal_sets_and_registry_integrity():
    """Rupture prévenue : dérive silencieuse entre INTENT_CONFIG, le registre
    d'actions et les goal-sets — la classe de bugs que les Phases 1/2 ont
    éliminée. Ces validateurs lèvent à l'import ; on les exécute explicitement
    pour qu'un drift casse la CI ici, pas en prod."""
    from ladini.graphs.agents.market_coach.core.base import validate_config_drift
    from ladini.graphs.agents.market_coach.core.goals import _validate_goal_drift
    from ladini.graphs.agents.market_coach.registry import validate_integrity

    validate_integrity()
    validate_config_drift()
    _validate_goal_drift()


def test_both_graphs_compile_with_dead_runtime():
    """Rupture prévenue : régression de câblage (nœud/edge manquant) qui ne
    se verrait qu'au premier message réel. Les DEUX graphes doivent compiler
    même avec un runtime minimal — c'est le smoke ultime de déploiement."""
    from ladini.graphs.agents.market_coach.core.graph_builder import build_graph

    class _Dead:
        async def call_db(self, *a, **k):
            raise ConnectionError("chaos")

    for role in ("BUYER", "PRODUCER"):
        app = build_graph(role, mc_runtime=_Dead())
        assert app is not None
