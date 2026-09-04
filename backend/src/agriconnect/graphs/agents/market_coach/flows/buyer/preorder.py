"""Buyer preorder workflow — draft → preflight recap → confirmation.

(2026-09-03, migration transactionnelle PREORDER) : la logique métier du
brouillon (création idempotente, confirmation, GPS, exécution, machine à
état) vit désormais dans `domain/preorder_draft.py` +
`flows/buyer/preorder_confirmation.py` — voir
`docs/PREORDER_TRANSACTIONAL_MIGRATION_2026-09-03.md`. Ce module reste le
point d'ENTRÉE du tunnel (compatibilité de routage avec `flow.py`,
`graph_builder.py`) et gère ce qui n'appartient PAS au draft :
collecte du panier avant qu'un draft n'existe, traduction d'un choix de
menu numéroté (`resolved_id`) en signal pour le nouveau domaine (JAMAIS un
contrôleur du workflow lui-même — mandat §7), et le cycle "ajouter d'autres
produits" (retour au panier, hors du draft)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)
from agriconnect.graphs.agents.market_coach.domain.preorder_draft import (
    CancelPreorderDraft,
    PreorderDraft,
    PreorderOutcome,
    PreorderOutcomeKind,
    apply_domain_action,
)
from agriconnect.graphs.agents.market_coach.flows.buyer.preorder_confirmation import (
    apply_response_plan,
    bootstrap_preorder_draft,
    resolve_preorder_confirmation,
)
from agriconnect.graphs.agents.market_coach.domain.preorder_draft import build_response_plan
from agriconnect.graphs.agents.market_coach.services.domain.cart_service import (
    SOURCE_TYPE_LABELS,
    CartDomainService,
)
from agriconnect.graphs.agents.market_coach.services.mcp.gateway import PreorderGateway
from agriconnect.graphs.agents.market_coach.utils import MarketRuntime
from agriconnect.services.database import preorder_draft_store

from .helpers import (
    PREORDER_ACTION_OPTIONS,
    clear_active_goal,
    logger,
    preorder_choice_from_index,
    render_interactive_menu,
)

# =====================================================================
# PRE-FLIGHT RECAP (rendu texte pur — inchangé, hors du problème d'autorité
# multiple : prend cart/meta en ARGUMENTS, ne lit aucun état global)
# =====================================================================


def build_preflight_recap(cart: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    """Build the pre-flight recap before final confirmation."""
    lines = ["📋 *Récapitulatif de votre précommande :*\n"]
    filtered_items: List[Dict[str, Any]] = []
    for item in cart:
        try:
            qty_val = float(item.get("quantity")) or None
        except (TypeError, ValueError):
            qty_val = None
        if qty_val is None or qty_val <= 0:
            continue
        filtered_items.append({"_quantity_val": qty_val, **item})

    for i, item in enumerate(filtered_items, start=1):
        source_label = SOURCE_TYPE_LABELS.get(
            str(item.get("source_type") or "DIRECT").upper(), "Catalogue"
        )
        vendor = item.get("vendor_name") or "—"
        qty = item.get("_quantity_val") or item.get("quantity")
        try:
            price_val = float(item.get("price") or 0.0)
        except (TypeError, ValueError):
            price_val = 0.0
        line_total = item.get("line_total")
        if line_total in (None, ""):
            line_total = round(float(qty or 0.0) * price_val, 2)
        lines.append(
            f"*{i}. {item.get('name')}*\n"
            f"   Quantité : {qty} {item.get('unit')}\n"
            f"   Prix unitaire : {item.get('price')} FCFA\n"
            f"   Sous-total : *{line_total} FCFA*\n"
            f"   Producteur : {vendor}\n"
            f"   Source : {source_label}"
        )
    lines.append(f"\n💰 *TOTAL : {meta.get('total_amount')} {meta.get('currency')}*")
    lines.append("\n_Que souhaitez-vous faire ?_")
    lines.append(render_interactive_menu(PREORDER_ACTION_OPTIONS))
    lines.append(
        "\n👉 Répondez avec le *numéro* (1, 2 ou 3) ou tapez *confirmer* / *annuler*."
    )
    return "\n".join(lines)


# =====================================================================
# PREORDER PHASE TRANSITION HELPER
# =====================================================================


async def update_preorder_phase(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Optional[Dict[str, Any]]:
    """Force deterministic preorder phase transitions.

    (2026-09-03) Simplifié : `create_preorder` sait désormais faire
    "bootstrap PUIS confirmer" en UN seul appel quand le goal l'exige
    (voir sa docstring) — l'ancienne fabrication d'un état SYNTHÉTIQUE pour
    appeler `create_preorder` deux fois de suite a disparu (moins de code,
    pas un contournement en plus)."""
    from .helpers import CART_GOALS

    goal = str(state.get("current_goal") or "").upper().strip()
    preorder_flow: Dict[str, Any] = dict(state.get("preorder_workflow") or {})
    phase = str(preorder_flow.get("phase") or "CART").upper().strip()

    def _phase_patch(new_phase: str) -> Dict[str, Any]:
        return {"preorder_workflow": {"phase": new_phase}}

    if goal in CART_GOALS and phase != "CART":
        logger.info("update_preorder_phase: normalising back to CART phase")
        return _phase_patch("CART")

    if goal in {"BUYER_PREORDER_INIT", "BUYER_PREORDER_CONFIRM"}:
        return await create_preorder(state, mc_runtime)

    return None


# =====================================================================
# MAIN PREORDER WORKFLOW
# =====================================================================


async def create_preorder(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    """Point d'entrée unique du tunnel précommande.

    1. Traduit un signal de menu/goal (`resolved_id`, mandat §7 : SIGNAL
       traduit ICI, jamais un contrôleur dispersé dans plusieurs branches)
       en CANCEL/ADD_MORE explicites ou en `interpreted_event="CONFIRM"`.
    2. CANCEL / ADD_MORE : orchestration pure, hors du draft (ADD_MORE ne
       touche jamais le draft — mandat §8, `PreorderDraft.items` reste la
       référence tant qu'aucun nouveau panier n'a été soumis).
    3. Draft déjà actif (`state["preorder_draft"]` posé) : délègue
       ENTIÈREMENT à `resolve_preorder_confirmation` — CONFIRM/UPDATE/
       REJECT/GPS, SEULE autorité de mutation à partir de là.
    4. Aucun draft actif : `bootstrap_preorder_draft` (création idempotente,
       mandat §15). Si la MÊME sollicitation demandait aussi une
       confirmation immédiate (ex: "confirme" envoyé directement depuis le
       panier, sans étape de récap intermédiaire), enchaîne SANS état
       synthétique : le draft vient d'être créé, sa cible de confirmation
       est connue, `resolve_preorder_confirmation` est appelé sur cet état
       fraîchement projeté.
    """
    goal = str(state.get("current_goal") or "").upper()
    payload: Dict[str, Any] = dict(state.get("transaction_payload") or {})
    phone = str(state.get("user_phone") or "")
    working_snapshot = (state.get("working_memory") or {}).get("last_active_cart")
    cart_source = state.get("active_cart") or working_snapshot or []
    cart: List[Dict[str, Any]] = list(cart_source)

    # --- Traduction du signal (mandat §7) — PAS un contrôleur : une seule
    # ligne de résolution, jamais rechecké ensuite pour décider une forme
    # de réponse différente ailleurs dans ce fichier. ---
    resolved_id = payload.get("resolved_id")
    if not resolved_id:
        mapped_choice = preorder_choice_from_index(payload.get("selection_index"))
        if mapped_choice:
            resolved_id = mapped_choice
    if goal == "BUYER_PREORDER_CONFIRM" and not resolved_id:
        resolved_id = "PREORDER_CONFIRM"
    if goal == "BUYER_CART_RESET" and not resolved_id:
        resolved_id = "PREORDER_CANCEL"
    resolved_id = str(resolved_id or "").upper()

    cached_draft_dict = state.get("preorder_draft")

    # --- CANCEL ---
    if resolved_id == "PREORDER_CANCEL":
        return await _cancel_preorder(state, cached_draft_dict, mc_runtime)

    # --- ADD MORE (retour au panier — n'AFFECTE PAS le draft, mandat §8) ---
    if resolved_id == "PREORDER_ADD_MORE":
        logger.info("create_preorder: add more requested — back to CART")
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.ENTER_FIELD, field_name="product"),
            "response_strategy": "ASK_MISSING_FIELD",
            "missing_fields": ["product"],
            "last_missing_field": "product",
            "preorder_workflow": {"phase": "CART"},
            "current_goal": "BUYER_ADD_TO_CART",
            "transaction_payload": {"__reset__": True},
            "ag_ui_component": None,
        }

    # --- Draft déjà actif : délègue entièrement (mandat §3/§12) ---
    if cached_draft_dict:
        confirm_state = dict(state)
        if resolved_id == "PREORDER_CONFIRM":
            confirm_state["interpreted_event"] = "CONFIRM"
        elif resolved_id == "PREORDER_CANCEL":  # défensif, déjà traité ci-dessus
            confirm_state["interpreted_event"] = "REJECT"
        return await resolve_preorder_confirmation(confirm_state, mc_runtime)

    # --- Aucun draft actif : collecte panier + bootstrap ---
    if not cart:
        return {
            "status": "WAITING_INPUT",
            **set_pending_interaction(InteractionKind.ENTER_FIELD, field_name="product"),
            "response_strategy": "ASK_MISSING_FIELD",
            "missing_fields": ["product"],
            "last_missing_field": "product",
            "preorder_workflow": {"phase": "CART"},
            "current_goal": "BUYER_ADD_TO_CART",
            "ag_ui_component": None,
        }

    items_payload = [
        {
            "product_id": item.get("product_id"),
            "name": item.get("name"),
            "quantity": item.get("quantity"),
            "unit": item.get("unit"),
            "price": item.get("price"),
            "producer_id": item.get("producer_id"),
            # (2026-09-04, audit CART→CHECKOUT) : `tier_id` DOIT être transmis
            # — sans lui, `create_preorder_draft` ne peut pas savoir que
            # `quantity` ci-dessus est un NOMBRE DE PAQUETS (palier) plutôt
            # qu'une quantité en unité de base, et traiterait TOUT article
            # comme un produit à tarif unique (voir
            # `services/database/buyer.py::create_preorder_draft`, section
            # palier). Le serveur re-résout le palier lui-même à partir de ce
            # seul id — jamais confiance au `price`/`unit` du panier
            # au-delà de cet identifiant.
            "tier_id": item.get("tier_id"),
        }
        for item in cart
        if item.get("status") != "DRAFT"
    ]
    meta = CartDomainService.recompute_cart_meta(cart)
    bootstrap_patch = await bootstrap_preorder_draft(
        state, mc_runtime, items_payload=items_payload, meta=meta
    )
    bootstrap_patch.setdefault("active_cart", cart)

    if resolved_id != "PREORDER_CONFIRM" or bootstrap_patch.get("status") == "ERROR":
        return bootstrap_patch

    new_draft_dict = bootstrap_patch.get("preorder_draft")
    if not new_draft_dict:
        return bootstrap_patch

    confirm_state = dict(state)
    confirm_state.update(bootstrap_patch)
    confirm_state["preorder_draft"] = new_draft_dict
    confirm_state["interpreted_event"] = "CONFIRM"
    confirm_state["pending_interaction"] = bootstrap_patch.get("pending_interaction")
    return await resolve_preorder_confirmation(confirm_state, mc_runtime)


async def _cancel_preorder(
    state: Dict[str, Any],
    cached_draft_dict: Optional[Dict[str, Any]],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    """Annulation explicite — si un draft est actif, transite RÉELLEMENT
    vers `CANCELLED` (CAS PostgreSQL), pas seulement un reset local
    (mandat §6 : `preorder_workflow` n'est plus l'autorité, `draft.status`
    l'est)."""
    if cached_draft_dict:
        draft_id = cached_draft_dict.get("draft_id")
        draft = await preorder_draft_store.load(draft_id) if draft_id else None
        if draft is None:
            draft = PreorderDraft.from_dict(cached_draft_dict)
        if draft is not None:
            outcome = apply_domain_action(draft, CancelPreorderDraft())
            if outcome.draft is not None and outcome.draft.version != draft.version:
                await preorder_draft_store.compare_and_swap(
                    draft.draft_id, expected_version=draft.version, new_draft=outcome.draft
                )
            # (2026-09-04, audit Order(DRAFT) orphelin) : ferme l'`Order`
            # Postgres sous-jacent en MÊME temps que le `PreorderDraft`
            # applicatif — UNIQUEMENT sur la VRAIE première transition
            # (`outcome.kind == CANCELLED`, même garde que le CAS ci-dessus :
            # un double-CANCEL, cf. `apply_domain_action`, renvoie
            # `DRAFT_FINALIZED` sans changer de version — idempotent, aucun
            # second appel MCP). Best-effort explicite : un échec ici ne doit
            # jamais faire échouer la confirmation d'annulation déjà actée
            # côté `PreorderDraft` — journalisé bruyamment (même discipline
            # DEGRADED que `cas_finalize` ailleurs dans ce module) pour rester
            # visible, jamais silencieux.
            if outcome.kind == PreorderOutcomeKind.CANCELLED and draft.order_id:
                try:
                    await PreorderGateway(mc_runtime).cancel_draft(
                        buyer_phone=str(state.get("user_phone") or ""),
                        preorder_id=draft.order_id,
                        reason="buyer_cancelled_preorder_draft",
                    )
                except Exception as exc:
                    logger.error(
                        "PREORDER_CANCEL_ORDER_SYNC_FAILED | draft_id=%s | order_id=%s | "
                        "DEGRADED: Order Postgres reste DRAFT malgré PreorderDraft=CANCELLED | %s",
                        draft.draft_id, draft.order_id, exc,
                    )
            plan = build_response_plan(outcome)
            patch = apply_response_plan(plan)
            patch.setdefault("current_goal", "BUYER_VIEW_CART")
            patch.setdefault("working_memory", clear_active_goal(state))
            patch.setdefault("active_form", None)
            return patch

    logger.info("create_preorder: cancel requested — returning to CART")
    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": "↩️ Précommande annulée. Votre panier est toujours disponible.",
        "preorder_workflow": {"__reset__": True, "phase": "CART"},
        "preorder_draft": None,
        "current_goal": "BUYER_VIEW_CART",
        "transaction_payload": {"__reset__": True},
        "working_memory": clear_active_goal(state),
        "active_form": None,
        "ag_ui_component": None,
    }


__all__ = [
    "build_preflight_recap",
    "update_preorder_phase",
    "create_preorder",
]
