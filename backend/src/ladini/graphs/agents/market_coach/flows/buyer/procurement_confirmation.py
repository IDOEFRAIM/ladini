"""Orchestration node — cycle de vie du `ProcurementDraft` (mandat de
refonte transactionnelle, 2026-09-03 ; durcissement architectural, même
jour, suite).

Sépare EXPLICITEMENT, en code, cinq responsabilités désormais dans des
fonctions distinctes (mandat §3/§7) :

1. Interprétation : DÉJÀ FAITE en amont (`interpreter/routing.py`) — ce
   module ne relit JAMAIS `normalized_text` pour en déduire une décision
   métier (exception : la note LLM de déviation ci-dessous, qui accuse
   réception d'un message hors-schéma sans jamais re-classifier l'intention).
2. Décision métier : `domain/procurement_draft.py::resolve_domain_action`.
3. État transactionnel : `domain/procurement_draft.py::apply_domain_action`
   — la SEULE fonction qui transitionne `ProcurementDraft`.
4. Présentation PURE : `domain/procurement_draft.py::build_response_plan`
   — `ProcurementOutcome` → `ProcurementResponsePlan`, AUCUNE décision
   métier, AUCUNE lecture de `state`, toujours le même résultat pour le
   même `(outcome, deviation_note)`. La note LLM (seul appel impur du
   cycle présentation) est calculée AVANT cet appel, jamais dedans.
5. Application MÉCANIQUE : `apply_response_plan` ci-dessous — traduit un
   `ProcurementResponsePlan` déjà entièrement décidé en patch d'état
   (construction `pending_interaction` via les primitives canoniques,
   `.to_dict()`). Ne décide rien — un test de contrat
   (`TestResponsePlanIsMechanicallyApplied`) le vérifie.

Ce nœud est la SEULE autorité de mutation de `procurement_draft` une fois
le draft créé (voir `nodes/confirmation_gate.py`, qui délègue ici plutôt
que d'appliquer sa logique générique `confirmation_summary`/
`transaction_payload` à ce goal — et `nodes/memory.py::memory_update`, qui
n'écrit plus `transaction_payload` du tout pour ce goal une fois qu'un
draft existe, voir sa docstring inline)."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from ladini.core.telemetry import record_procurement_transaction_event
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    clear_pending_interaction,
    resolve_pending_interaction,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.core.state import entities_said_this_turn
from ladini.graphs.agents.market_coach.domain.procurement_draft import (
    ProcurementDraft,
    ProcurementOutcome,
    ProcurementOutcomeKind,
    ProcurementResponsePlan,
    apply_domain_action,
    build_response_plan,
    check_confirmation_target_invariant,
    resolve_domain_action,
)
from ladini.graphs.agents.market_coach.domain.procurement_draft import (
    ProcurementOutcomeKind as _Kind,
)
from ladini.graphs.agents.market_coach.utils import llm_deviation_reply
from ladini.services.database import procurement_draft_store
from ladini.services.database.draft_store_support import cas_finalize

logger = logging.getLogger("Ladini.MarketCoach.ProcurementConfirmation")


def _target_of(pending_interaction: Any) -> Optional[Dict[str, Any]]:
    if isinstance(pending_interaction, dict):
        target = pending_interaction.get("target")
        return target if isinstance(target, dict) else None
    return None


def _is_mutation(before: Optional[ProcurementDraft], after: Optional[ProcurementDraft]) -> bool:
    """Une PERSISTANCE est requise ssi le draft a réellement changé (version
    OU statut) — comparaison STRUCTURELLE, pas une liste de
    `ProcurementOutcomeKind` maintenue à la main en parallèle (exactement
    la classe de bug — deux sources qui peuvent diverger — que cette
    refonte élimine ailleurs)."""
    if before is None or after is None:
        return before is not after
    return before.version != after.version or before.status != after.status


async def resolve_procurement_confirmation(
    state: Dict[str, Any], mc_runtime: Any
) -> Dict[str, Any]:
    """Point d'entrée du nœud.

    (2026-09-03, persistance transactionnelle) : `state["procurement_draft"]`
    n'est plus lu comme AUTORITATIF — seulement pour retrouver le
    `draft_id` à charger depuis `services/database/procurement_draft_store.py`
    (PostgreSQL, canonique). LangGraph state devient une PROJECTION DE
    TRAVAIL, rafraîchie à la fin de CE tour avec ce que la DB a réellement
    accepté — jamais l'inverse."""
    interpreted_event = str(state.get("interpreted_event") or "").upper()
    extracted_entities = entities_said_this_turn(state)
    pending_target = _target_of(state.get("pending_interaction"))

    cached = state.get("procurement_draft") or {}
    draft_id = cached.get("draft_id") or (pending_target or {}).get("draft_id")
    draft = await procurement_draft_store.load(draft_id) if draft_id else None
    if draft is None and cached:
        # DB injoignable ou ligne pas encore visible (jamais censé arriver —
        # confirmation_gate.py insère AVANT de poser ce cache) : repli
        # dégradé sur le cache plutôt qu'un crash de tour, mais journalisé
        # bruyamment — ce n'est PAS le chemin normal.
        logger.warning(
            "PROCUREMENT_DRAFT_DB_UNAVAILABLE_FALLBACK_TO_CACHE | draft_id=%s", draft_id
        )
        draft = ProcurementDraft.from_dict(cached)

    entry_violation = check_confirmation_target_invariant(pending_target, draft)
    if entry_violation:
        logger.warning("PROCUREMENT_INVARIANT_VIOLATION | on_entry | %s", entry_violation)

    action = resolve_domain_action(
        interpreted_event=interpreted_event,
        extracted_entities=extracted_entities,
        pending_target=pending_target,
    )
    # 3. État transactionnel — décision PURE, encore en mémoire.
    outcome = apply_domain_action(draft, action)

    # 3bis. Persistance ATOMIQUE (mandat §1) — un vrai `UPDATE ... WHERE
    # version = :expected`, jamais un verrou Python. Seulement si le draft a
    # réellement changé (comparaison structurelle, voir `_is_mutation`).
    if draft is not None and _is_mutation(draft, outcome.draft):
        # (2026-09-03, hardening transverse) : `cas_finalize` — 5e occurrence
        # du même motif identifiée pendant l'audit (voir
        # `services/database/draft_store_support.py` pour la liste des 4
        # autres). Les 2 causes d'échec restent DISTINGUÉES ci-dessous
        # (mandat : pas de try/except qui les confond) via une comparaison
        # d'identité sur le résultat, `cas_finalize` lui-même restant neutre
        # sur laquelle des deux s'est produite.
        pre_cas_draft = outcome.draft
        persisted, reconciled_draft = await cas_finalize(
            compare_and_swap=procurement_draft_store.compare_and_swap,
            load=procurement_draft_store.load,
            original=draft,
            finalized=pre_cas_draft,
        )
        if not persisted:
            if reconciled_draft is not pre_cas_draft:
                # VERSION_CONFLICT réel : la ligne existe, un autre
                # écrivain a gagné la course entre notre lecture et notre
                # écriture. Relit la version RÉELLEMENT persistée — ne
                # présente JAMAIS `outcome.draft` (celui qui a perdu).
                outcome = ProcurementOutcome(
                    kind=ProcurementOutcomeKind.VERSION_CONFLICT, draft=reconciled_draft
                )
                logger.warning(
                    "PROCUREMENT_VERSION_CONFLICT | draft_id=%s | expected_version=%s | "
                    "current_version=%s",
                    draft.draft_id,
                    draft.version,
                    reconciled_draft.version,
                )
            else:
                # DB injoignable — on ne PEUT PAS relire l'état réel, donc
                # on NE PEUT PAS non plus affirmer qu'un autre écrivain a
                # gagné une course : ce serait inventer un VERSION_CONFLICT
                # qui n'a jamais eu lieu. On garde `outcome` tel que calculé
                # en mémoire par `apply_domain_action` et on avance en mode
                # dégradé (même discipline que le repli d'entrée ci-dessus)
                # — la garantie transactionnelle CAS est perdue pour CE
                # tour, journalisé bruyamment, jamais silencieux.
                logger.error(
                    "PROCUREMENT_DRAFT_PERSISTENCE_UNAVAILABLE | draft_id=%s | "
                    "version %s -> %s NON PERSISTÉE (DB injoignable) | "
                    "DEGRADED: garantie CAS perdue pour ce tour",
                    draft.draft_id,
                    draft.version,
                    outcome.draft.version if outcome.draft else None,
                )

    # Le SEUL appel impur de la présentation (note LLM) — calculé AVANT
    # build_response_plan, jamais à l'intérieur (mandat §3 : la fonction de
    # présentation reste pure).
    deviation_note: Optional[str] = None
    if outcome.kind == _Kind.DRAFT_UNCHANGED and outcome.draft:
        summary = outcome.draft.render_summary()
        user_text = str(
            state.get("normalized_text") or state.get("user_query") or ""
        ).strip()
        if summary:
            deviation_note = await llm_deviation_reply(
                mc_runtime, user_text, f"un récapitulatif à confirmer :\n{summary}"
            )

    # 4. Présentation PURE.
    plan = build_response_plan(outcome, deviation_note=deviation_note)
    # 5. Application mécanique — rafraîchit la PROJECTION LangGraph avec ce
    # que la DB a réellement accepté.
    patch = apply_response_plan(plan)

    exit_target = _target_of(patch.get("pending_interaction"))
    exit_violation = check_confirmation_target_invariant(exit_target, outcome.draft)
    if exit_violation:
        logger.error("PROCUREMENT_INVARIANT_VIOLATION | on_exit | %s", exit_violation)

    # Log local (grep/debug) — PAS l'instrumentation exigée par le mandat,
    # voir `record_procurement_transaction_event` juste en dessous pour la
    # trace Langfuse RÉELLE, corrélée au trace_id de ce tour.
    logger.info(
        "PROCUREMENT_TRACE | event=%s | draft_id=%s | version_before=%s "
        "version_after=%s | target_before=%s | domain_action=%s | "
        "domain_outcome=%s | target_after=%s | status=%s",
        interpreted_event,
        draft.draft_id if draft else None,
        draft.version if draft else None,
        outcome.draft.version if outcome.draft else None,
        pending_target,
        type(action).__name__,
        outcome.kind.value,
        exit_target,
        patch.get("status"),
    )
    record_procurement_transaction_event(
        event_id=uuid.uuid4().hex,
        conversation_id=str(state.get("user_phone") or state.get("session_id") or "") or None,
        message_id=state.get("message_sid"),
        draft_id=draft.draft_id if draft else None,
        draft_version_before=draft.version if draft else None,
        draft_version_after=outcome.draft.version if outcome.draft else None,
        pending_kind=(
            (state.get("pending_interaction") or {}).get("kind")
            if isinstance(state.get("pending_interaction"), dict)
            else None
        ),
        confirmation_target=pending_target,
        interpreter_event=interpreted_event,
        domain_action=type(action).__name__,
        state_before=draft.status.value if draft else None,
        state_after=outcome.draft.status.value if outcome.draft else None,
        outcome=outcome.kind.value,
    )
    return patch


def apply_response_plan(plan: ProcurementResponsePlan) -> Dict[str, Any]:
    """`ProcurementResponsePlan` → patch d'état. MÉCANIQUE uniquement —
    chaque champ du plan a déjà été décidé par `build_response_plan` ; cette
    fonction ne fait que l'exprimer dans le vocabulaire du graphe
    (`pending_interaction`, `procurement_draft`, `status`...). Aucun `if`
    ici ne teste un `ProcurementOutcomeKind` — la preuve que cette fonction
    ne réintroduit pas de décision métier est qu'elle n'importe même pas
    `ProcurementOutcomeKind`."""
    patch: Dict[str, Any] = {
        "procurement_draft": plan.draft.to_dict() if plan.draft else None,
        "status": plan.graph_status,
        "response_strategy": plan.response_strategy,
        "ag_ui_component": None,
        # Défensif, TOUJOURS posé (jamais omis) — un `execution_authorized`
        # forgé en amont ne doit JAMAIS survivre à ce nœud sur une issue qui
        # n'autorise pas l'exécution (chaos I1/I2). Le SEUL endroit qui les
        # repasse à True est la branche `ready_for_execution` ci-dessous.
        "execution_authorized": False,
        "is_certified": False,
    }
    if plan.final_response:
        patch["final_response"] = plan.final_response

    if plan.terminal_goal_reset:
        patch["current_goal"] = None
        patch["goal_status"] = "COMPLETED"

    if plan.pending_untouched:
        pass  # aucune clé `pending_interaction` dans le patch — inchangé.
    elif plan.pending_kind == "CONFIRM_ACTION":
        patch.update(
            set_pending_interaction(
                InteractionKind.CONFIRM_ACTION,
                context_ref="confirmation",
                target=plan.pending_target,
            )
        )
    elif plan.pending_kind == "ENTER_FIELD":
        patch.update(set_pending_interaction(InteractionKind.ENTER_FIELD, field_name=plan.pending_field))
    elif plan.ready_for_execution or plan.graph_status == "COMPLETED":
        patch.update(resolve_pending_interaction())
    else:
        patch.update(clear_pending_interaction("procurement_response_plan_no_target"))

    if plan.ready_for_execution and plan.draft is not None:
        patch["transaction_payload"] = plan.draft.execution_payload()
        patch["is_certified"] = True
        patch["execution_authorized"] = True

    return patch


__all__ = ["resolve_procurement_confirmation", "apply_response_plan"]
