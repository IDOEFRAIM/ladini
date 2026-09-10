"""Synchronisation `PreorderDraft` ↔ paiement (2026-09-03, clôture
escrow/IPN — fermeture du trou identifié dans la migration PREORDER
précédente : "Paydunya IPN ne met pas encore à jour PreorderDraft").

## Rôle EXACT

Ce module NE FAIT PAS de logique de paiement — `EscrowMixin.mark_escrow_paid`/
`initiate_escrow_payment`/`expire_pending_payments`
(`services/database/escrow.py`) restent LA seule autorité sur `Order`
(paiement/stock/statut), inchangées, gardes `SELECT...FOR UPDATE` +
idempotence `payment_status in {ESCROWED, PAID_OUT}` préservées intégralement.

Ce module fait UNIQUEMENT la synchronisation `PreorderDraft` ↔ `Order` une
fois qu'`EscrowMixin` a déjà tranché : charge le draft par `order_id`
(colonne dédiée, `preorder_draft_store.find_by_order_id`), adapte le
résultat (`adapt_payment_outcome`/`adapt_mcp_result`-style, SEUL point de
contact avec le format Paydunya/`mark_escrow_paid`), transitionne via CAS.

## PAYMENT ≠ CONFIRMATION (mandat RÈGLE ABSOLUE)

`apply_payment_outcome` n'est JAMAIS appelé par un tour de conversation —
uniquement par `workers/payments/paydunya_ipn_task.py` (paiement RÉEL
re-confirmé serveur-à-serveur) et `workers/crons/order_expiry.py`
(expiration TTL déjà tranchée). Aucun message "je confirme" utilisateur ne
peut faire transiter `AWAITING_PAYMENT` — seule une preuve externe le peut.

## Pas de réponse conversationnelle directe (mandat §21)

Ce module n'envoie JAMAIS de message WhatsApp lui-même — `mark_escrow_paid`
enfile déjà ses propres notifications Outbox pour le cas PAYÉ (inchangé,
non dupliqué ici). Pour FAILED/EXPIRED (gap réel comblé ici : ces deux cas
n'notifiaient PERSONNE avant), une notification Outbox minimale est
enfilée avec le MÊME mécanisme (`dedupe_key` unique, `ON CONFLICT DO
NOTHING` — idempotent par construction, jamais un envoi dupliqué)."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from ladini.core.database import get_sessionmaker
from ladini.core.telemetry import record_procurement_transaction_event
from ladini.graphs.agents.market_coach.domain.preorder_draft import (
    PaymentOutcomeKind,
    PreorderDraft,
    PreorderDraftStatus,
    PreorderOutcome,
    PreorderOutcomeKind,
    adapt_payment_outcome,
    finalize_after_payment,
    finalize_after_payment_expiry,
)
from ladini.services.database import preorder_draft_store
from ladini.services.database.draft_store_support import cas_finalize
from ladini.workers.runtime import worker_session

logger = logging.getLogger("Ladini.MarketCoach.PreorderPayment")

_OUTCOME_KIND_BY_STATUS = {
    PreorderDraftStatus.EXECUTED: PreorderOutcomeKind.PREORDER_EXECUTED,
    PreorderDraftStatus.PAYMENT_FAILED: PreorderOutcomeKind.PREORDER_PAYMENT_FAILED,
    PreorderDraftStatus.PAYMENT_EXPIRED: PreorderOutcomeKind.PREORDER_PAYMENT_EXPIRED,
    PreorderDraftStatus.EXECUTION_UNKNOWN: PreorderOutcomeKind.PREORDER_EXECUTION_UNKNOWN,
}


async def _notify_payment_failure(draft: PreorderDraft, *, template_key: str) -> None:
    """Best-effort — une notification manquée ne doit JAMAIS faire échouer
    la synchronisation d'état (qui, elle, EST critique)."""
    if not draft.buyer_phone:
        logger.warning(
            "PREORDER_PAYMENT_NOTIFY_SKIPPED_NO_PHONE | draft_id=%s | order_id=%s",
            draft.draft_id,
            draft.order_id,
        )
        return
    try:
        from ladini.workers.repositories import outbox_repo

        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            return
        async with sessionmaker() as session:
            await outbox_repo.enqueue(
                session,
                [
                    {
                        "channel": "WHATSAPP",
                        "recipient_phone": draft.buyer_phone,
                        "template_key": template_key,
                        "payload": {"order_number": (draft.order_id or "")[:8].upper()},
                        "dedupe_key": f"{template_key}:{draft.order_id}",
                    }
                ],
            )
            await session.commit()
    except Exception:
        logger.exception(
            "PREORDER_PAYMENT_NOTIFY_FAILED | draft_id=%s | order_id=%s", draft.draft_id, draft.order_id
        )


async def _finalize_and_persist(
    draft: PreorderDraft,
    finalized: PreorderDraft,
    *,
    reason: str,
    payment_status: Optional[str] = None,
) -> Optional[PreorderOutcome]:
    # (2026-09-03, hardening transverse) : `cas_finalize` — même primitive
    # partagée que les 2 services de réconciliation. Distinction préservée
    # (conflit RÉSOLU par relecture vs relecture elle-même indisponible) via
    # une comparaison d'identité sur le résultat, `cas_finalize` ne
    # collapsant PAS les deux cas dans le même log.
    finalized_candidate = finalized
    persisted, finalized = await cas_finalize(
        compare_and_swap=preorder_draft_store.compare_and_swap,
        load=preorder_draft_store.load,
        original=draft,
        finalized=finalized_candidate,
    )
    if not persisted:
        if finalized is not finalized_candidate:
            logger.warning(
                "PREORDER_PAYMENT_VERSION_CONFLICT | draft_id=%s | version=%s | reason=%s",
                draft.draft_id,
                draft.version,
                reason,
            )
        else:
            logger.error(
                "PREORDER_PAYMENT_PERSISTENCE_UNAVAILABLE | draft_id=%s | DEGRADED",
                draft.draft_id,
            )

    outcome_kind = _OUTCOME_KIND_BY_STATUS.get(finalized.status)
    if outcome_kind is None:
        logger.error("PREORDER_PAYMENT_UNEXPECTED_STATUS | status=%s", finalized.status)
        return None

    record_procurement_transaction_event(
        event_id=uuid.uuid4().hex,
        conversation_id=None,
        draft_id=draft.draft_id,
        draft_version_before=draft.version,
        draft_version_after=finalized.version,
        pending_kind=None,
        confirmation_target=None,
        interpreter_event=None,
        domain_action=reason,
        state_before=draft.status.value,
        state_after=finalized.status.value,
        execution_key=None,
        external_request_id=draft.order_id,
        mcp_status=None,
        execution_result=finalized.status.value,
        payment_status=payment_status,
        outcome=outcome_kind.value,
    )
    return PreorderOutcome(kind=outcome_kind, draft=finalized)


async def apply_payment_outcome(
    order_id: str,
    *,
    paydunya_status: Optional[str],
    mark_paid_result: Optional[Dict[str, Any]],
) -> Optional[PreorderOutcome]:
    """Appelée par `workers/payments/paydunya_ipn_task.py` APRÈS
    `mark_escrow_paid` (ou l'équivalent échec/annulation re-confirmé).
    Idempotent : un IPN rejoué sur un draft déjà `EXECUTED`/`PAYMENT_FAILED`
    est un NO-OP silencieux (le `status != AWAITING_PAYMENT` guard ci-dessous),
    jamais une double notification ni une double transition — même garantie
    que `mark_escrow_paid` au niveau `Order`, à un niveau différent
    (`PreorderDraft`).

    `None` en retour : soit aucun draft ne correspond à cet `order_id`
    (commande hors du tunnel PREORDER — PAS une erreur), soit le draft
    n'est PAS `AWAITING_PAYMENT` (déjà traité, ou jamais passé par ce
    chemin) — dans les deux cas, un NO-OP silencieux et sûr."""
    draft = await preorder_draft_store.find_by_order_id(order_id)
    if draft is None:
        logger.info("PREORDER_PAYMENT_NO_MATCHING_DRAFT | order_id=%s", order_id)
        return None
    if draft.status != PreorderDraftStatus.AWAITING_PAYMENT:
        logger.info(
            "PREORDER_PAYMENT_ALREADY_FINALIZED | draft_id=%s | order_id=%s | status=%s",
            draft.draft_id,
            order_id,
            draft.status.value,
        )
        return None

    outcome_kind = adapt_payment_outcome(paydunya_status, mark_paid_result)
    if outcome_kind == PaymentOutcomeKind.PENDING:
        # Pas une issue actionnable (mandat §8, IPN out-of-order) — le
        # draft reste AWAITING_PAYMENT, AUCUNE transition, AUCUN log
        # d'erreur (ce n'est pas une anomalie, juste "pas encore"). Seul
        # `adapt_payment_outcome` (domain/preorder_draft.py) sait lire un
        # statut Paydunya brut — ici on ne teste que l'enum qu'il retourne.
        return None

    finalized = finalize_after_payment(draft, outcome_kind)
    result = await _finalize_and_persist(
        draft, finalized, reason="apply_payment_outcome", payment_status=paydunya_status
    )

    if result is not None and result.draft.status == PreorderDraftStatus.PAYMENT_FAILED:
        from ladini.workers.outbox import templates as _tpl

        await _notify_payment_failure(result.draft, template_key=_tpl.ESCROW_PAYMENT_FAILED_BUYER)

    return result


async def apply_payment_expiry(order_id: str) -> Optional[PreorderOutcome]:
    """Appelée par `workers/crons/order_expiry.py`, une fois par
    `order_id` retourné par `expire_pending_payments` (déjà tranché — ce
    module ne fait QUE synchroniser le draft). Même idempotence que
    `apply_payment_outcome`."""
    draft = await preorder_draft_store.find_by_order_id(order_id)
    if draft is None:
        return None
    if draft.status != PreorderDraftStatus.AWAITING_PAYMENT:
        return None

    finalized = finalize_after_payment_expiry(draft)
    result = await _finalize_and_persist(
        draft, finalized, reason="apply_payment_expiry", payment_status="expired"
    )

    if result is not None and result.draft.status == PreorderDraftStatus.PAYMENT_EXPIRED:
        from ladini.workers.outbox import templates as _tpl

        await _notify_payment_failure(result.draft, template_key=_tpl.ESCROW_PAYMENT_EXPIRED_BUYER)

    return result


async def reconcile_invoice(invoice_token: str) -> Dict[str, Any]:
    """Cœur PARTAGÉ de la re-confirmation Paydunya — réutilisé par
    `workers/payments/paydunya_ipn_task.py` (temps réel, déclenché par le
    webhook) ET par `PreorderReconciliationService` (périodique, pour un
    draft `AWAITING_PAYMENT` resté bloqué — mandat §26 : ne pas dupliquer
    cette logique de branchement à deux endroits).

    Re-confirme le statut RÉEL directement auprès de Paydunya
    (`PaydunyaClient.confirm_invoice`, jamais un statut fourni par
    l'appelant), applique `mark_escrow_paid`/`mark_escrow_payment_failed`
    (idempotents, gardes `SELECT...FOR UPDATE` préservées), puis synchronise
    `PreorderDraft` via `apply_payment_outcome`. `Order` reste la source
    canonique, mise à jour EN PREMIER, `PreorderDraft` seulement ensuite
    (mandat §2)."""
    from ladini.services.database.d import AgriDatabaseService
    from ladini.services.payments.paydunya_client import PaydunyaClient, PaydunyaError

    client = PaydunyaClient()
    try:
        confirmed = await client.confirm_invoice(invoice_token)
    except PaydunyaError as exc:
        logger.error("PREORDER_PAYMENT_RECONCILE_CONFIRM_FAILED | token=%s | %s", invoice_token, exc)
        return {"status": "error", "reason": "confirm_failed"}

    status = confirmed.get("status")
    if status == "completed":
        async with worker_session():
            result = await AgriDatabaseService().mark_escrow_paid(invoice_token)
        if result.get("status") == "success" and result.get("order_id"):
            await apply_payment_outcome(
                str(result["order_id"]), paydunya_status="completed", mark_paid_result=result
            )
        return result

    if status == "cancelled":
        async with worker_session():
            result = await AgriDatabaseService().mark_escrow_payment_failed(invoice_token)
        if result.get("order_id"):
            await apply_payment_outcome(
                str(result["order_id"]), paydunya_status="cancelled", mark_paid_result=result
            )
        return result

    return {"status": "ignored", "paydunya_status": status}


__all__ = ["apply_payment_outcome", "apply_payment_expiry", "reconcile_invoice"]
