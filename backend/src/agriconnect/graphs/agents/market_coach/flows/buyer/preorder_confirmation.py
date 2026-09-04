"""Orchestration node — cycle de vie du `PreorderDraft` (2026-09-03,
migration PREORDER, même standard que `procurement_confirmation.py`).

Différence structurelle assumée (voir `domain/preorder_draft.py`, docstring
module) : PREORDER n'a PAS de nœud exécuteur générique séparé
(`mcp_tool_executor`) — `confirm_preorder_draft` est appelé DIRECTEMENT ici,
dans le MÊME nœud que la décision de confirmation, parce que c'est ainsi
que le workflow réel fonctionnait déjà (`_execute_confirm`, l'ancien code,
appelait le gateway de façon synchrone). La discipline transactionnelle est
préservée quand même : `EXECUTING` est persisté (CAS) AVANT l'appel MCP,
jamais après — un crash entre les deux laisse un état honnête, récupérable
par la réconciliation (voir `services/reconciliation/`, réutilisable tel
quel pour PREORDER, section H du rapport final).

Sépare les MÊMES 5 responsabilités que PROCUREMENT :
1. Interprétation : déjà faite en amont — jamais de relecture de texte ici,
   sauf la note LLM de déviation (identique à PROCUREMENT).
2. Décision métier : `domain/preorder_draft.py::resolve_domain_action`.
3. État transactionnel : `apply_domain_action` — SEULE fonction qui
   transitionne `PreorderDraft`.
4. Exécution + adaptation : `PreorderGateway.confirm_draft` (RÉEL appel
   MCP, protégé par idempotency_key ET par la garde serveur
   `status != DRAFT` déjà existante) → `adapt_mcp_result` →
   `finalize_after_execution`.
5. Présentation PURE : `build_response_plan` → `apply_response_plan`
   (mécanique)."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from agriconnect.core.telemetry import record_procurement_transaction_event
from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    clear_pending_interaction,
    resolve_pending_interaction,
    set_pending_interaction,
)
from agriconnect.graphs.agents.market_coach.domain.preorder_draft import (
    PreorderDraft,
    PreorderDraftStatus,
    PreorderOutcome,
    PreorderOutcomeKind,
    PreorderResponsePlan,
    UpdatePreorderDraft,
    adapt_escrow_result,
    adapt_mcp_result,
    apply_domain_action,
    build_response_plan,
    cart_fingerprint,
    check_confirmation_target_invariant,
    creation_key,
    execution_key,
    finalize_after_execution,
    finalize_escrow_initiation,
    resolve_domain_action,
)
from agriconnect.graphs.agents.market_coach.flows.buyer.gps_delivery_gate import (
    enter_gps_stage,
    resolve_gps_stage,
)
from agriconnect.graphs.agents.market_coach.services.mcp.gateway import (
    EscrowGateway,
    PreorderGateway,
)
from agriconnect.graphs.agents.market_coach.utils import is_success_response, llm_deviation_reply
from agriconnect.services.database import preorder_draft_store
from agriconnect.services.database.draft_store_support import cas_finalize

logger = logging.getLogger("AgriConnect.MarketCoach.PreorderConfirmation")

_TERMINAL_OUTCOME_KIND_BY_STATUS = {
    PreorderDraftStatus.EXECUTED: PreorderOutcomeKind.PREORDER_EXECUTED,
    PreorderDraftStatus.FAILED: PreorderOutcomeKind.PREORDER_FAILED,
    PreorderDraftStatus.EXECUTION_UNKNOWN: PreorderOutcomeKind.PREORDER_EXECUTION_UNKNOWN,
    PreorderDraftStatus.AWAITING_PAYMENT: PreorderOutcomeKind.PREORDER_AWAITING_PAYMENT,
}


def _target_of(pending_interaction: Any) -> Optional[Dict[str, Any]]:
    if isinstance(pending_interaction, dict):
        target = pending_interaction.get("target")
        return target if isinstance(target, dict) else None
    return None


def _is_mutation(before: Optional[PreorderDraft], after: Optional[PreorderDraft]) -> bool:
    if before is None or after is None:
        return before is not after
    return before.version != after.version or before.status != after.status


async def _persist(
    draft: Optional[PreorderDraft], outcome: PreorderOutcome
) -> PreorderOutcome:
    """CAS best-effort — même discipline honnête que PROCUREMENT : un
    `compare_and_swap` échoué distingue un VRAI conflit (relecture réussit,
    version différente) d'une DB injoignable (relecture échoue aussi —
    mode dégradé, jamais un VERSION_CONFLICT inventé)."""
    if draft is None or not _is_mutation(draft, outcome.draft):
        return outcome
    persisted = await preorder_draft_store.compare_and_swap(
        draft.draft_id, expected_version=draft.version, new_draft=outcome.draft
    )
    if persisted:
        return outcome
    current = await preorder_draft_store.load(draft.draft_id)
    if current is not None:
        logger.warning(
            "PREORDER_VERSION_CONFLICT | draft_id=%s | expected_version=%s | current_version=%s",
            draft.draft_id,
            draft.version,
            current.version,
        )
        return PreorderOutcome(kind=PreorderOutcomeKind.VERSION_CONFLICT, draft=current)
    logger.error(
        "PREORDER_DRAFT_PERSISTENCE_UNAVAILABLE | draft_id=%s | version %s -> %s NON PERSISTÉE "
        "(DB injoignable) | DEGRADED: garantie CAS perdue pour ce tour",
        draft.draft_id,
        draft.version,
        outcome.draft.version if outcome.draft else None,
    )
    return outcome


async def _execute_and_finalize(
    executing_draft: PreorderDraft, mc_runtime: Any, buyer_phone: str
) -> PreorderOutcome:
    """`executing_draft` est DÉJÀ persisté EXECUTING (appelant) — l'appel
    MCP réel a lieu ICI, protégé par `idempotency_key=execution_key(...)`
    (dédup RÉELLE côté serveur depuis le chantier PROCUREMENT) ET, hors
    escrow, par la garde serveur `status != DRAFT` déjà existante (double
    protection, jamais retirée — mandat §14).

    Deux effets externes DIFFÉRENTS selon `settings.ESCROW_PAYMENT_ENABLED`
    (mandat §17 : jamais cachés derrière un `success=True` unique) —
    `initiate_escrow_payment` (réservation + lien Paydunya, débit stock
    DIFFÉRÉ à l'IPN, hors de ce nœud — voir
    `PreorderDraftStatus.AWAITING_PAYMENT`) vs `confirm_preorder_draft`
    (débit stock immédiat, protégé par `SELECT...FOR UPDATE` +
    `status != DRAFT` côté serveur)."""
    from agriconnect.core.settings import settings

    key = execution_key(executing_draft)

    if settings.ESCROW_PAYMENT_ENABLED:
        try:
            raw = await EscrowGateway(mc_runtime).initiate_escrow_payment(
                buyer_phone=buyer_phone,
                preorder_id=executing_draft.order_id,
                delivery_lat=executing_draft.delivery_lat,
                delivery_lon=executing_draft.delivery_lon,
                idempotency_key=key,
            )
            execution_status = "COMPLETED" if str(raw.get("status") or "").lower() != "error" else "ERROR"
            execution_result = raw
        except Exception as exc:
            logger.error(
                "PREORDER_EXECUTE_ESCROW_FAILED | draft_id=%s | %s", executing_draft.draft_id, exc
            )
            execution_status = None
            execution_result = None
        mcp_result = adapt_escrow_result(execution_status, execution_result)
        finalized = finalize_escrow_initiation(executing_draft, mcp_result)
    else:
        try:
            raw = await PreorderGateway(mc_runtime).confirm_draft(
                buyer_phone=buyer_phone,
                preorder_id=executing_draft.order_id,
                delivery_lat=executing_draft.delivery_lat,
                delivery_lon=executing_draft.delivery_lon,
                idempotency_key=key,
            )
            execution_status = "COMPLETED" if str(raw.get("status") or "").lower() != "error" else "ERROR"
            execution_result = raw
        except Exception as exc:
            logger.error(
                "PREORDER_EXECUTE_CONFIRM_FAILED | draft_id=%s | %s", executing_draft.draft_id, exc
            )
            execution_status = None
            execution_result = None
        mcp_result = adapt_mcp_result(execution_status, execution_result)
        finalized = finalize_after_execution(executing_draft, mcp_result)

    # (2026-09-03, hardening transverse) : `cas_finalize` — 4e occurrence du
    # même motif "CAS, et si un autre écrivain a gagné la course, relire ce
    # qu'il a RÉELLEMENT écrit" identifiée pendant l'audit transverse (les 3
    # autres : les 2 services de réconciliation + `preorder_payment.py`).
    finalized_candidate = finalized
    persisted, finalized = await cas_finalize(
        compare_and_swap=preorder_draft_store.compare_and_swap,
        load=preorder_draft_store.load,
        original=executing_draft,
        finalized=finalized_candidate,
    )
    if not persisted:
        if finalized is not finalized_candidate:
            logger.warning(
                "PREORDER_FINALIZATION_VERSION_CONFLICT | draft_id=%s | version=%s",
                executing_draft.draft_id,
                executing_draft.version,
            )
        else:
            logger.error(
                "PREORDER_FINALIZATION_PERSISTENCE_UNAVAILABLE | draft_id=%s | DEGRADED",
                executing_draft.draft_id,
            )

    outcome_kind = _TERMINAL_OUTCOME_KIND_BY_STATUS.get(finalized.status)
    if outcome_kind is None:
        logger.error("PREORDER_FINALIZER_UNEXPECTED_STATUS | status=%s", finalized.status)
        outcome_kind = PreorderOutcomeKind.PREORDER_EXECUTION_UNKNOWN

    record_procurement_transaction_event(
        event_id=uuid.uuid4().hex,
        conversation_id=buyer_phone or None,
        draft_id=executing_draft.draft_id,
        draft_version_before=executing_draft.version,
        draft_version_after=finalized.version,
        pending_kind=None,
        confirmation_target=None,
        interpreter_event=None,
        domain_action="preorder_execute_confirm",
        state_before=executing_draft.status.value,
        state_after=finalized.status.value,
        execution_key=key,
        external_request_id=finalized.order_id if hasattr(finalized, "order_id") else None,
        mcp_status=execution_status,
        execution_result="success" if mcp_result.success else ("ambiguous" if mcp_result.ambiguous else "error"),
        outcome=outcome_kind.value,
        error=mcp_result.error,
    )
    detail = None
    if outcome_kind == PreorderOutcomeKind.PREORDER_AWAITING_PAYMENT and mcp_result.checkout_url:
        # Le lien de paiement est éphémère (mandat §19 : jamais persisté
        # dans le draft, pas un fait métier durable) — porté par
        # `PreorderOutcome.detail`, lu UNIQUEMENT par `build_response_plan`
        # pour CE tour, jamais relu depuis le draft à un tour ultérieur.
        detail = (
            f"{mcp_result.checkout_url}|{mcp_result.ttl_hours or 24}"
        )
    return PreorderOutcome(kind=outcome_kind, draft=finalized, detail=detail)


async def resolve_preorder_confirmation(
    state: Dict[str, Any], mc_runtime: Any
) -> Dict[str, Any]:
    """Point d'entrée — CONFIRM/UPDATE/REJECT/CANCEL sur un draft déjà
    créé (voir `flows/buyer/preorder.py::_bootstrap_preorder_draft` pour la
    création v1, hors de ce module)."""
    interpreted_event = str(state.get("interpreted_event") or "").upper()
    pending_interaction = state.get("pending_interaction")
    pending_target = _target_of(pending_interaction)
    buyer_phone = str(state.get("user_phone") or "")

    cached = state.get("preorder_draft") or {}
    draft_id = cached.get("draft_id") or (pending_target or {}).get("draft_id")
    draft = await preorder_draft_store.load(draft_id) if draft_id else None
    if draft is None and cached:
        logger.warning("PREORDER_DRAFT_DB_UNAVAILABLE_FALLBACK_TO_CACHE | draft_id=%s", draft_id)
        draft = PreorderDraft.from_dict(cached)

    entry_violation = check_confirmation_target_invariant(pending_target, draft)
    if entry_violation:
        logger.warning("PREORDER_INVARIANT_VIOLATION | on_entry | %s", entry_violation)

    # GPS (mandat §20) : `resolve_gps_stage`/`enter_gps_stage` (réutilisées
    # TELLES QUELLES, aucune réinvention) — `gps_default` est une suggestion
    # ÉPHÉMÈRE (pas un fait métier durable, jamais persistée dans le draft),
    # portée par `preorder_workflow` au même titre qu'avant cette migration.
    resolved_location = None
    preorder_workflow_state = state.get("preorder_workflow") or {}
    kind = str((pending_interaction or {}).get("kind") or "")
    if kind == "PROVIDE_LOCATION":
        resolution = await resolve_gps_stage(
            mc_runtime,
            buyer_phone,
            location_shared=bool(state.get("location_shared")),
            is_yes=interpreted_event == "CONFIRM",
            gps_default=preorder_workflow_state.get("gps_default"),
            user_text=str(state.get("normalized_text") or ""),
            location_outcome=state.get("location_outcome"),
            location_lat=state.get("location_lat"),
            location_lon=state.get("location_lon"),
        )
        if resolution.resolved:
            resolved_location = (resolution.lat, resolution.lon)
        else:
            # Pas encore résolu — pose la MÊME cible, réponse adaptée
            # (message déjà construit par `resolve_gps_stage`), AUCUNE
            # mutation du draft.
            return {
                "status": "WAITING_INPUT",
                **set_pending_interaction(
                    InteractionKind.PROVIDE_LOCATION, context_ref="confirmation", target=pending_target
                ),
                "response_strategy": "ASK_MISSING_FIELD",
                "final_response": resolution.message,
                "preorder_workflow": preorder_workflow_state,
                "ag_ui_component": None,
            }

    action = resolve_domain_action(
        interpreted_event=interpreted_event,
        extracted_entities=state.get("extracted_entities") or {},
        pending_target=pending_target,
        resolved_location=resolved_location,
    )
    outcome = apply_domain_action(draft, action)
    outcome = await _persist(draft, outcome)

    if outcome.kind == PreorderOutcomeKind.CONFIRMED_READY_FOR_EXECUTION and outcome.draft is not None:
        outcome = await _execute_and_finalize(outcome.draft, mc_runtime, buyer_phone)

    if outcome.kind == PreorderOutcomeKind.NEEDS_LOCATION and outcome.draft is not None:
        # 1re fois que l'utilisateur confirme SANS point de livraison connu
        # — entre dans l'étape GPS (`enter_gps_stage`, réutilisée telle
        # quelle). Le draft reste DRAFT (inchangé), la cible de confirmation
        # est reposée pour la MÊME version (mandat §20 : le domaine n'a fait
        # AUCUNE mutation, seule la couche conversationnelle avance).
        gate = await enter_gps_stage(mc_runtime, buyer_phone)
        target = {"draft_id": outcome.draft.draft_id, "draft_version": outcome.draft.version}
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(
                InteractionKind.PROVIDE_LOCATION, context_ref="confirmation", target=target
            ),
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": gate["prompt"],
            "preorder_draft": outcome.draft.to_dict(),
            "preorder_workflow": {
                "phase": "PREORDER_DRAFTED",
                "preorder_id": outcome.draft.order_id,
                "total_amount": outcome.draft.total_amount,
                "gps_stage": True,
                "gps_default": gate["gps_default"],
            },
            "ag_ui_component": None,
        }

    # Le SEUL appel impur de la présentation (note LLM) — DÉCLENCHÉ
    # UNIQUEMENT sur `DRAFT_UNCHANGED` (message reçu pendant une
    # confirmation en attente, mais sans intention métier reconnue —
    # hésitation, question, remarque hors-sujet), même contrat que
    # `procurement_confirmation.py`. `REJECT` reste un signal explicite,
    # pas une déviation — accusé de réception scripté, pas de LLM.
    deviation_note: Optional[str] = None
    if outcome.kind == PreorderOutcomeKind.DRAFT_UNCHANGED and outcome.draft:
        summary = outcome.draft.render_summary()
        user_text = str(state.get("normalized_text") or state.get("user_query") or "").strip()
        if summary:
            deviation_note = await llm_deviation_reply(
                mc_runtime, user_text, f"un récapitulatif à confirmer :\n{summary}"
            )

    plan = build_response_plan(outcome, deviation_note=deviation_note)
    patch = apply_response_plan(plan)

    exit_target = _target_of(patch.get("pending_interaction"))
    exit_violation = check_confirmation_target_invariant(exit_target, outcome.draft)
    if exit_violation:
        logger.error("PREORDER_INVARIANT_VIOLATION | on_exit | %s", exit_violation)

    logger.info(
        "PREORDER_TRACE | event=%s | draft_id=%s | version_before=%s version_after=%s | "
        "domain_action=%s | domain_outcome=%s | status=%s",
        interpreted_event,
        draft.draft_id if draft else None,
        draft.version if draft else None,
        outcome.draft.version if outcome.draft else None,
        type(action).__name__,
        outcome.kind.value,
        patch.get("status"),
    )
    return patch


def apply_response_plan(plan: PreorderResponsePlan) -> Dict[str, Any]:
    """`PreorderResponsePlan` → patch d'état. MÉCANIQUE uniquement — voir
    `procurement_confirmation.py::apply_response_plan` (même contrat, même
    test de non-régression appliqué ici aussi)."""
    patch: Dict[str, Any] = {
        "preorder_draft": plan.draft.to_dict() if plan.draft else None,
        "status": plan.graph_status,
        "response_strategy": plan.response_strategy,
        "ag_ui_component": None,
        "execution_authorized": False,
        "is_certified": False,
    }
    if plan.final_response:
        patch["final_response"] = plan.final_response

    # (mandat §6) — projection de `preorder_workflow` DÉRIVÉE du draft, la
    # SEULE écriture de ce champ pour tout le cycle confirmation/exécution
    # (la création reste dans `preorder.py::_bootstrap_preorder_draft`, la
    # même règle de projection unique s'y applique). `active_cart` n'est
    # PAS réécrit ici — `plan.draft.items` reste la source canonique des
    # items dès qu'un draft existe (mandat §8) ; le rendu lit le draft, pas
    # `active_cart`, à partir de ce nœud.
    if plan.draft is not None:
        patch["preorder_workflow"] = {
            "phase": _phase_projection(plan),
            "preorder_id": plan.draft.order_id,
            "total_amount": plan.draft.total_amount,
        }

    if plan.terminal:
        patch["current_goal"] = None
        patch["goal_status"] = "COMPLETED"
        if not plan.preserve_cart_on_terminal:
            patch["active_cart"] = []
            patch["transaction_payload"] = {"__reset__": True}
        if plan.draft is not None and plan.draft.status in (
            PreorderDraftStatus.EXECUTED,
            PreorderDraftStatus.AWAITING_PAYMENT,
        ):
            patch["last_order_summary"] = {
                "order_id": plan.draft.order_id,
                "total_amount": plan.draft.total_amount,
                "currency": plan.draft.currency,
                "items": list(plan.draft.items),
            }

    if plan.pending_untouched:
        pass
    elif plan.pending_kind == "CONFIRM_ACTION":
        patch.update(
            set_pending_interaction(InteractionKind.CONFIRM_ACTION, context_ref="confirmation", target=plan.pending_target)
        )
    elif plan.pending_kind == "PROVIDE_LOCATION":
        patch.update(
            set_pending_interaction(InteractionKind.PROVIDE_LOCATION, context_ref="confirmation", target=plan.pending_target)
        )
    elif plan.pending_kind == "ENTER_FIELD":
        patch.update(set_pending_interaction(InteractionKind.ENTER_FIELD, field_name=plan.pending_field))
    elif plan.ready_for_execution or plan.terminal:
        patch.update(resolve_pending_interaction())
    else:
        patch.update(clear_pending_interaction("preorder_response_plan_no_target"))

    return patch


def _phase_projection(plan: PreorderResponsePlan) -> str:
    """`preorder_workflow["phase"]` DÉRIVÉ de `draft.status` — PROJECTION
    en lecture seule pour le routage existant (`buyer_context_resolver`,
    `graph_builder.py`, `cart_service.py`, `interpreter/routing.py`),
    JAMAIS écrit ailleurs pour un draft actif (mandat §6). Ces 3 valeurs
    sont les SEULES lues par le routage existant — inchangées pour ne
    toucher AUCUN de ces fichiers."""
    if plan.draft is None:
        return "CART"
    if plan.draft.status == PreorderDraftStatus.DRAFT:
        return "PREORDER_DRAFTED"
    if plan.draft.status in (PreorderDraftStatus.EXECUTED,):
        return "CONFIRMED"
    # EXECUTING/FAILED/EXECUTION_UNKNOWN/CANCELLED : le tour est de toute
    # façon `terminal`/`COMPLETED` à ce point (voir apply_response_plan),
    # donc `current_goal=None` — la valeur de `phase` ne pilote plus le
    # routage pour ce tour. "CART" par défaut = repli sûr documenté.
    return "CART"


async def bootstrap_preorder_draft(
    state: Dict[str, Any], mc_runtime: Any, *, items_payload: list, meta: Dict[str, Any]
) -> Dict[str, Any]:
    """CART → brouillon (mandat §15 : création idempotente) — LA SEULE
    autorité qui appelle `create_preorder_draft` et pose la ligne
    PostgreSQL `preorder_drafts`. Appelée par
    `flows/buyer/preorder.py::create_preorder` (phase CART), qui reste
    responsable de la collecte du panier lui-même (validation "panier
    vide" incluse) — ce module ne connaît que des `items` déjà résolus."""
    phone = str(state.get("user_phone") or "")
    fingerprint = cart_fingerprint(items_payload)
    idem_key = creation_key(phone, fingerprint)

    try:
        draft_res = await PreorderGateway(mc_runtime).create_draft(
            buyer_phone=phone,
            cart_items=items_payload,
            payment_method=state.get("preferred_payment_method") or "CASH",
            delivery_zone_id=state.get("zone_id"),
            idempotency_key=idem_key,
        )
    except Exception as exc:
        logger.error("bootstrap_preorder_draft: create_draft a échoué: %s", exc)
        return {
            "status": "ERROR",
            "response_strategy": "ERROR",
            "final_response": (
                "Impossible de préparer votre précommande pour le moment. "
                "Votre panier est conservé — réessayez dans un instant."
            ),
            "preorder_workflow": {"phase": "CART"},
            "ag_ui_component": None,
        }

    order_id = draft_res.get("preorder_id") or draft_res.get("id") or "DRAFT"
    if not is_success_response(draft_res) and str(draft_res.get("status") or "").lower() == "error":
        return {
            "status": "ERROR",
            "response_strategy": "ERROR",
            "final_response": draft_res.get("message") or "Impossible de créer le brouillon de précommande.",
            "preorder_workflow": {"phase": "CART"},
            "ag_ui_component": None,
        }

    # (2026-09-04, audit CART→CHECKOUT) : GAP RÉEL fermé ici — jusqu'ici, ce
    # module construisait `PreorderDraft.items`/`total_amount` à partir de
    # `items_payload`/`meta` (le panier FIGÉ au moment de chaque ajout,
    # potentiellement périmé : prix catalogue modifié entre-temps, palier mal
    # résolu) au lieu de la réponse RÉELLE de `create_preorder_draft`, qui
    # relit `Product.price`/`Product.pricing_tiers` EN BASE et écrit
    # `OrderItem` avec CES valeurs. Le récapitulatif affiché à l'acheteur
    # (`render_summary()`, `total_amount`) pouvait donc diverger de ce que la
    # commande allait RÉELLEMENT facturer/débiter — exactement la classe de
    # bug "affichage ≠ exécution" que toute cette architecture existe pour
    # éliminer. `draft_res["items"]`/`draft_res["total_amount"]` (ajoutés par
    # ce même correctif côté `create_preorder_draft`) sont désormais la
    # SEULE source pour ce que l'acheteur confirme — jamais un repli sur le
    # panier local sauf si le serveur, en mode dégradé, ne les a pas renvoyés
    # (ex: ancien serveur MCP non redéployé) : dans ce cas on retombe sur le
    # panier local plutôt que de planter, journalisé pour être visible.
    server_items = draft_res.get("items")
    if isinstance(server_items, list) and server_items:
        resolved_items = server_items
    else:
        logger.warning(
            "bootstrap_preorder_draft: create_draft n'a pas renvoyé d'items "
            "résolus serveur — repli sur le panier local (peut être périmé)"
        )
        resolved_items = items_payload
    server_total = draft_res.get("total_amount")
    resolved_total = server_total if server_total is not None else meta.get("total_amount")

    fields = {
        "order_id": str(order_id),
        "items": resolved_items,
        "total_amount": resolved_total,
        "currency": draft_res.get("currency") or meta.get("currency"),
        "buyer_phone": phone,
    }

    cached = state.get("preorder_draft") or {}
    existing = PreorderDraft.from_dict(cached) if cached else None
    if existing is not None:
        existing_authoritative = await preorder_draft_store.load(existing.draft_id)
        if existing_authoritative is not None:
            existing = existing_authoritative

    if existing is not None and existing.status == PreorderDraftStatus.DRAFT:
        # Panier ré-composé (cycle "ajouter d'autres produits") — NOUVELLE
        # version du MÊME draft (mandat §11), pas un nouveau draft_id.
        old_order_id = existing.order_id
        outcome = apply_domain_action(existing, UpdatePreorderDraft(fields=fields))
        outcome = await _persist(existing, outcome)
        draft = outcome.draft

        # (2026-09-04, audit Order(DRAFT) orphelin) : GAP RÉEL fermé ici —
        # `create_preorder_draft` (appelé plus haut dans cette fonction) a
        # déjà créé un NOUVEL `Order(status=DRAFT)` inconditionnellement ;
        # sans ce correctif, l'ANCIEN (`old_order_id`) restait DRAFT pour
        # toujours (comportement historique, documenté ici comme un vrai
        # défaut, pas un choix — voir le rapport d'audit dédié). L'ancien
        # n'a pas été REJETÉ par l'acheteur (contrairement à CANCEL) : il a
        # été REMPLACÉ — `target_status="SUPERSEDED"`, jamais "CANCELLED",
        # pour que support/observabilité puissent distinguer les deux
        # issues. Uniquement sur une mise à jour RÉELLEMENT persistée
        # (`kind != VERSION_CONFLICT` — voir `_persist` : un conflit renvoie
        # le draft GAGNANT d'un autre écrivain concurrent, dont l'`order_id`
        # n'est ni `old_order_id` ni le nôtre ; superseder `old_order_id`
        # dans ce cas précis serait une simple supposition, pas une
        # certitude — cas déjà rare, laissé identique au comportement
        # historique plutôt que deviner). Best-effort explicite, jamais
        # bloquant pour la suite du tour.
        if (
            old_order_id
            and outcome.kind != PreorderOutcomeKind.VERSION_CONFLICT
            and draft is not None
            and draft.order_id != old_order_id
        ):
            try:
                await PreorderGateway(mc_runtime).cancel_draft(
                    buyer_phone=phone,
                    preorder_id=old_order_id,
                    reason=f"superseded_by_draft_version_{draft.version}",
                    target_status="SUPERSEDED",
                )
            except Exception as exc:
                logger.error(
                    "PREORDER_ADD_MORE_OLD_ORDER_SYNC_FAILED | draft_id=%s | "
                    "old_order_id=%s | new_order_id=%s | DEGRADED: ancien Order "
                    "Postgres reste DRAFT | %s",
                    draft.draft_id, old_order_id, draft.order_id, exc,
                )
    else:
        draft = PreorderDraft.new(existing.draft_id if existing else uuid.uuid4().hex[:12], **fields)
        inserted = await preorder_draft_store.insert(draft, conversation_id=phone)
        if not inserted:
            logger.warning(
                "bootstrap_preorder_draft: échec insertion PostgreSQL (draft_id=%s) — mode dégradé",
                draft.draft_id,
            )
        outcome = PreorderOutcome(kind=PreorderOutcomeKind.DRAFT_UPDATED, draft=draft)

    plan = build_response_plan(outcome)
    patch = apply_response_plan(plan)

    # (2026-09-04, audit CART→CHECKOUT boundary closure) : GAP RÉEL fermé
    # ici — `create_preorder_draft` peut écarter silencieusement un article
    # du panier (palier périmé, ou désormais seuil minimum non atteint —
    # voir son propre correctif) sans que rien, jusqu'ici, n'en informe
    # l'acheteur : le récapitulatif affiché n'aurait alors JAMAIS mentionné
    # que ce panier de 3 articles n'en a produit que 2 — exactement la
    # classe de divergence affichage≠exécution que cette architecture
    # existe pour éliminer (mandat §17 : "display = server decision =
    # order snapshot" pour un article retiré). On préfixe un avertissement
    # explicite plutôt que de laisser l'écart invisible.
    unresolved = draft_res.get("unresolved_items")
    if isinstance(unresolved, list) and unresolved and patch.get("final_response"):
        warning = (
            f"⚠️ {len(unresolved)} article(s) de votre panier n'ont pas pu être "
            "inclus dans cette précommande (conditionnement ou seuil minimum "
            "devenu invalide entre-temps) — le récapitulatif ci-dessous ne "
            "porte que les articles réellement pris en compte.\n\n"
        )
        patch["final_response"] = warning + str(patch["final_response"])

    return patch


__all__ = ["resolve_preorder_confirmation", "apply_response_plan", "bootstrap_preorder_draft"]
