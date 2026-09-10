"""Finalisation post-exécution — `ProcurementDraft` EXECUTING → EXECUTED/
FAILED/EXECUTION_UNKNOWN (2026-09-03, clôture du pipeline transactionnel).

Nœud de graphe INSÉRÉ entre `nodes/executor.py::mcp_tool_executor`
(générique, inchangé — le dispatcher/exécuteur ne connaît AUCUN état
métier procurement, mandat §2) et `response_strategy`. No-op immédiat pour
tout tour qui ne concerne pas un draft `EXECUTING` — coût négligeable pour
les 15+ autres goals qui traversent le même graphe.

Séparation stricte (mandat §2) :

    mcp_tool_executor (générique, transport MCP)
        ↓ execution_status / execution_result (bruts)
    adapt_mcp_result()               [domain/procurement_draft.py — adaptateur]
        ↓ ProcurementExecutionResult  [format neutre, découplé du MCP]
    finalize_after_execution()       [domain/procurement_draft.py — transition]
        ↓ ProcurementDraft (EXECUTED/FAILED/EXECUTION_UNKNOWN)
    build_response_plan()            [domain/procurement_draft.py — présentation pure]
        ↓ ProcurementResponsePlan
    apply_response_plan (mécanique) ↓ patch d'état

Ce nœud lui-même ne fait QUE l'orchestration ci-dessus — aucune décision
métier, aucun appel MCP, aucune construction de texte affiché : identique
en esprit à `flows/buyer/procurement_confirmation.py`."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict

from ladini.core.telemetry import record_procurement_transaction_event
from ladini.graphs.agents.market_coach.domain.procurement_draft import (
    ProcurementDraft,
    ProcurementDraftStatus,
    ProcurementOutcome,
    ProcurementOutcomeKind,
    adapt_mcp_result,
    build_response_plan,
    finalize_after_execution,
)
from ladini.graphs.agents.market_coach.domain.procurement_draft import (
    execution_key as _execution_key,
)
from ladini.graphs.agents.market_coach.flows.buyer.procurement_confirmation import (
    apply_response_plan,
)
from ladini.services.database import procurement_draft_store
from ladini.services.database.draft_store_support import cas_finalize

logger = logging.getLogger("Ladini.MarketCoach.ProcurementExecutionFinalizer")

_OUTCOME_KIND_BY_TERMINAL_STATUS = {
    ProcurementDraftStatus.EXECUTED: ProcurementOutcomeKind.PROCUREMENT_EXECUTED,
    ProcurementDraftStatus.FAILED: ProcurementOutcomeKind.PROCUREMENT_FAILED,
    ProcurementDraftStatus.EXECUTION_UNKNOWN: ProcurementOutcomeKind.PROCUREMENT_EXECUTION_UNKNOWN,
}


async def finalize_procurement_execution(
    state: Dict[str, Any], mc_runtime: Any
) -> Dict[str, Any]:
    cached = ProcurementDraft.from_dict(state.get("procurement_draft"))
    if cached is None or cached.status != ProcurementDraftStatus.EXECUTING:
        # No-op pour tout autre goal, ou si ce tour n'a rien à finaliser
        # (ex: la confirmation a échoué AVANT d'atteindre l'exécuteur —
        # confirmation_gate/procurement_confirmation.py ont déjà produit la
        # réponse appropriée, ce nœud n'a rien à ajouter).
        return {}

    # (2026-09-03, persistance transactionnelle) : relit l'AUTORITATIF avant
    # de finaliser — même discipline que procurement_confirmation.py. Un
    # worker qui aurait redémarré entre la confirmation et l'exécution doit
    # finaliser la version RÉELLEMENT persistée, jamais un cache périmé.
    draft = await procurement_draft_store.load(cached.draft_id) or cached

    execution_status = state.get("status")
    execution_result = state.get("execution_result")
    result = adapt_mcp_result(execution_status, execution_result)
    finalized_draft = finalize_after_execution(draft, result)

    # (2026-09-03, hardening transverse) : `cas_finalize` — 6e occurrence du
    # même motif identifiée pendant l'audit (voir
    # `services/database/draft_store_support.py`).
    finalized_candidate = finalized_draft
    persisted, finalized_draft = await cas_finalize(
        compare_and_swap=procurement_draft_store.compare_and_swap,
        load=procurement_draft_store.load,
        original=draft,
        finalized=finalized_candidate,
    )
    if not persisted:
        if finalized_draft is not finalized_candidate:
            # Un AUTRE processus a déjà finalisé cette même transition
            # (EXECUTING -> statut terminal) entre notre lecture et notre
            # écriture — relit ce qu'il a réellement écrit plutôt que
            # d'imposer notre propre verdict par-dessus.
            logger.warning(
                "PROCUREMENT_FINALIZATION_VERSION_CONFLICT | draft_id=%s | version=%s",
                draft.draft_id,
                draft.version,
            )
        else:
            # DB injoignable — pas un vrai conflit (on ne peut pas relire
            # l'état réel pour l'affirmer). Poursuit avec `finalized_draft`
            # calculé en mémoire, en mode dégradé, journalisé bruyamment —
            # jamais silencieux (même discipline que procurement_confirmation.py).
            logger.error(
                "PROCUREMENT_FINALIZATION_PERSISTENCE_UNAVAILABLE | draft_id=%s | "
                "version %s -> %s NON PERSISTÉE (DB injoignable) | "
                "DEGRADED: garantie CAS perdue pour cette finalisation",
                draft.draft_id,
                draft.version,
                finalized_draft.version,
            )

    outcome_kind = _OUTCOME_KIND_BY_TERMINAL_STATUS.get(finalized_draft.status)
    if outcome_kind is None:
        # Filet — `finalize_after_execution` ne peut produire que les 3
        # statuts ci-dessus (ou un statut déjà terminal relu après conflit,
        # ex: un autre worker a déjà conclu EXECUTED) ; couvert par la même
        # table de retry que `apply_domain_action` pour rester cohérent.
        logger.error(
            "PROCUREMENT_FINALIZER_UNEXPECTED_STATUS | status=%s", finalized_draft.status
        )
        return {}

    outcome = ProcurementOutcome(kind=outcome_kind, draft=finalized_draft)
    plan = build_response_plan(outcome)
    patch = apply_response_plan(plan)

    logger.info(
        "PROCUREMENT_FINALIZATION_TRACE | draft_id=%s | version=%s | "
        "execution_status_in=%s | success=%s | ambiguous=%s | "
        "external_id=%s | final_status=%s | persisted=%s",
        draft.draft_id,
        draft.version,
        execution_status,
        result.success,
        result.ambiguous,
        result.external_id,
        finalized_draft.status.value,
        persisted,
    )
    record_procurement_transaction_event(
        event_id=uuid.uuid4().hex,
        conversation_id=str(state.get("user_phone") or state.get("session_id") or "") or None,
        message_id=state.get("message_sid"),
        draft_id=draft.draft_id,
        draft_version_before=draft.version,
        draft_version_after=finalized_draft.version,
        pending_kind=None,  # finalisation : aucune interaction en attente à ce stade
        confirmation_target=None,
        interpreter_event=None,  # pas un événement utilisateur — issue MCP
        domain_action="finalize_after_execution",
        state_before=draft.status.value,
        state_after=finalized_draft.status.value,
        execution_key=_execution_key(draft),
        external_request_id=result.external_id,
        mcp_status=execution_status,
        execution_result="success" if result.success else ("ambiguous" if result.ambiguous else "error"),
        outcome=outcome_kind.value,
        error=result.error,
    )
    return patch


__all__ = ["finalize_procurement_execution"]
