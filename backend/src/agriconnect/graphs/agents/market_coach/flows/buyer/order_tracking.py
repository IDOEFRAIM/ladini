"""Order Tracking — Intelligence conversationnelle de suivi de commande.

Ce module implémente le "suivi de commande conversationnel" pour le BuyerFlow :
  1. StatusCheck   — Analyse du statut + réponse humaine contextuelle.
  2. CancelAction  — Annulation conversationnelle directe (par ID ou dernière commande).
  3. ListOrders    — Dashboard paginé avec mapping dynamique + expiration 30 min.
  4. Proactivité   — Vérification d'état à l'ouverture de conversation.

Chaque réponse est formatée WhatsApp (emojis, sauts de ligne, messages courts).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agriconnect.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from agriconnect.graphs.agents.market_coach.services.mcp.gateway import OrderTrackingGateway
from agriconnect.graphs.agents.market_coach.utils import (
    MarketRuntime,
    ensure_dict,
    is_success_response,
)
from agriconnect.graphs.agents.market_coach.services.menu_snapshot import (
    menu_snapshot_store,
)

logger = logging.getLogger("AgriConnect.Market.BuyerFlow.OrderTracking")


# =====================================================================
# CONSTANTS
# =====================================================================

# Durée de validité d'un menu dynamique (30 minutes)
MENU_SESSION_TTL_SECONDS = 30 * 60

# Mapping émoji conversationnel par statut
STATUS_MAP = {
    "PENDING": ("⏳", "En attente de validation"),
    "DRAFT": ("📝", "Brouillon (non confirmée)"),
    "CONFIRMED": ("✅", "Confirmée — préparation en cours"),
    "PAID": ("💰", "Payée — en attente d'expédition"),
    "PROCESSING": ("🔄", "En cours de préparation"),
    "SHIPPED": ("🚛", "Expédiée — en route vers vous"),
    "IN_TRANSIT": ("🚛", "En transit"),
    "PICKED_UP": ("📍", "Arrivée au point de collecte"),
    "DELIVERED": ("📦", "Livrée avec succès"),
    "CANCELLED": ("❌", "Annulée"),
}

# Intents gérés par ce module
ORDER_TRACKING_GOALS = frozenset({
    "BUYER_CHECK_ORDER_STATUS",
    "BUYER_LIST_ORDERS",
    "BUYER_CANCEL_ORDER",
})


# =====================================================================
# HELPERS — Parsing & Formatting
# =====================================================================

_ORDER_ID_PATTERN = re.compile(
    r"(?:#|commande\s*#?|order\s*#?)\s*([A-Fa-f0-9]{6,36})",
    re.IGNORECASE,
)


def extract_order_ref(text: str) -> Optional[str]:
    """Extrait un identifiant de commande depuis le texte utilisateur.

    Supporte : #ABC123, commande #ABC123, order ABC123, UUID complet.
    """
    if not text:
        return None
    m = _ORDER_ID_PATTERN.search(text)
    if m:
        return m.group(1).strip()
    # Fallback: texte brut qui ressemble à un UUID partiel (8+ hex)
    hex_match = re.search(r"\b([A-Fa-f0-9]{8,36})\b", text)
    if hex_match:
        return hex_match.group(1).strip()
    return None


def _format_elapsed(dt: Optional[datetime]) -> str:
    """Retourne une durée humaine depuis `dt` jusqu'à maintenant."""
    if not dt:
        return ""
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        minutes = max(1, int(delta.total_seconds() // 60))
        return f"il y a {minutes} min"
    if hours < 24:
        return f"il y a {hours}h"
    days = hours // 24
    return f"il y a {days} jour{'s' if days > 1 else ''}"


def _status_emoji(status: str) -> str:
    """Retourne l'émoji correspondant au statut."""
    entry = STATUS_MAP.get(status.upper(), ("🔄", status))
    return entry[0]


def _status_label(status: str) -> str:
    """Retourne le label humain du statut."""
    entry = STATUS_MAP.get(status.upper(), ("🔄", status))
    return f"{entry[0]} {entry[1]}"


def _resolve_menu_selection(state: Dict[str, Any], selection: Any) -> Optional[str]:
    """Résout un index numérique via le menu snapshot stocké côté UI engine."""
    if selection in (None, "", [], {}):
        return None
    session_id = str(state.get("session_id") or state.get("user_phone") or "")
    if not session_id:
        return None
    working = state.get("working_memory") or {}
    snapshot_id = (
        working.get("menu_snapshot_id")
        or state.get("menu_snapshot_id")
    )
    if not snapshot_id:
        return None
    return menu_snapshot_store.resolve(session_id, snapshot_id, selection)


# =====================================================================
# 1. CONVERSATIONAL STATUS CHECK
# =====================================================================

def _build_status_response(order_data: Dict[str, Any]) -> str:
    """Génère une réponse conversationnelle contextuelle selon le statut.

    Au lieu de balancer un JSON, l'agent guide l'acheteur :
    - PENDING    → Expliquer + proposer modification/annulation.
    - SHIPPED    → Estimation humaine + rassurer.
    - DELIVERED  → Confirmer date + demander retour qualité.
    """
    status = str(order_data.get("status") or "PENDING").upper()
    order_number = order_data.get("order_number") or str(order_data.get("id", ""))[:8].upper()
    total = order_data.get("total_amount") or order_data.get("total") or 0
    currency = order_data.get("currency") or "FCFA"
    created_at = order_data.get("created_at") or order_data.get("date")
    items_summary = ""

    items = order_data.get("items") or []
    if items:
        if isinstance(items[0], dict):
            parts = []
            for it in items[:3]:
                name = it.get("name") or it.get("product_name") or "Article"
                qty = it.get("qty") or it.get("quantity") or 1
                parts.append(f"{name} (x{qty})")
            items_summary = ", ".join(parts)
            if len(items) > 3:
                items_summary += f" +{len(items) - 3} autres"

    header = f"📋 *Commande #{order_number}*"
    if items_summary:
        header += f"\n🛒 {items_summary}"
    header += f"\n💵 Total : *{total} {currency}*"

    if status == "PENDING":
        return (
            f"{header}\n\n"
            f"⏳ *Statut : En attente de validation*\n\n"
            f"La préparation n'a pas encore commencé. Le producteur va "
            f"bientôt prendre en charge votre commande.\n\n"
            f"💡 _Souhaitez-vous modifier ou annuler cette commande ?_\n"
            f"Répondez *annuler* ou *modifier*."
        )

    if status == "CONFIRMED":
        return (
            f"{header}\n\n"
            f"✅ *Statut : Confirmée*\n\n"
            f"Votre commande est en cours de préparation chez le producteur. "
            f"Vous serez notifié dès l'expédition.\n\n"
            f"⏱️ _Estimation : expédition sous 24-48h._"
        )

    if status in ("SHIPPED", "IN_TRANSIT"):
        return (
            f"{header}\n\n"
            f"🚛 *Statut : En route vers vous*\n\n"
            f"Votre commande est en cours de livraison ! "
            f"Le livreur devrait arriver chez vous dans environ *2 heures*.\n\n"
            f"📍 _Restez joignable pour la réception._"
        )

    if status == "PICKED_UP":
        return (
            f"{header}\n\n"
            f"📍 *Statut : Arrivée au point de collecte*\n\n"
            f"Votre commande vous attend au point de retrait. "
            f"Présentez-vous avec votre numéro de commande.\n\n"
            f"🆔 Référence : *#{order_number}*"
        )

    if status == "DELIVERED":
        elapsed = _format_elapsed(
            datetime.fromisoformat(created_at) if isinstance(created_at, str) else created_at
        ) if created_at else ""
        return (
            f"{header}\n\n"
            f"📦 *Statut : Livrée* {elapsed}\n\n"
            f"Votre commande a été livrée avec succès ! "
            f"Nous espérons que tout est conforme.\n\n"
            f"⭐ _Comment était la qualité des produits ?_\n"
            f"Répondez avec une note de 1 à 5 ou un commentaire."
        )

    if status == "CANCELLED":
        return (
            f"{header}\n\n"
            f"❌ *Statut : Annulée*\n\n"
            f"Cette commande a été annulée. Les stocks ont été restitués.\n\n"
            f"🔁 _Souhaitez-vous passer une nouvelle commande ?_"
        )

    if status in ("PAID", "PROCESSING"):
        return (
            f"{header}\n\n"
            f"{_status_label(status)}\n\n"
            f"Votre paiement est confirmé. La commande est en préparation.\n\n"
            f"📦 _Vous recevrez une notification à l'expédition._"
        )

    # Fallback générique
    return (
        f"{header}\n\n"
        f"{_status_label(status)}\n\n"
        f"_Tapez *aide* pour plus d'options._"
    )


async def check_order_status(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    """Node : vérifie le statut d'une commande et répond de façon conversationnelle.

    Résolution de l'order_id :
    1. Depuis le payload (order_id explicite ou extraction texte).
    2. Via les snapshots de menus (sélection numérique).
    3. Depuis l'order_focus (dernière commande consultée).
    4. Fallback : dernière commande active.
    """
    payload: Dict[str, Any] = state.get("transaction_payload") or {}
    phone = str(state.get("user_phone") or "")
    order_focus = (state.get("order_tracking_context") or {}).get("order_focus")
    normalized_text = str(state.get("normalized_text") or "")

    # Résolution de l'order_id
    order_id = (
        payload.get("order_id")
        or payload.get("selected_value")
        or extract_order_ref(normalized_text)
    )

    # Résolution via sélection numérique (menu snapshots)
    if not order_id:
        selection_idx = payload.get("selection_index")
        if selection_idx is not None:
            order_id = _resolve_menu_selection(state, selection_idx)

    # Fallback : dernière commande en focus
    if not order_id:
        order_id = order_focus

    if not order_id and not phone:
        return {
            "status": "ERROR",
            "response_strategy": "ERROR",
            "final_response": "Je n'ai pas pu identifier votre commande. Quel est votre numéro de commande ?",
            "ag_ui_component": None,
        }

    # Appel DB via MCP
    kwargs: Dict[str, Any] = {}
    if order_id:
        kwargs["order_id"] = str(order_id)
    else:
        kwargs["buyer_phone"] = phone

    result = await _safe_tracking_call(mc_runtime, "get_transaction_summary", **kwargs)

    if not is_success_response(result):
        # Commande introuvable → re-engagement
        return _order_not_found_response(order_id, phone)

    data = result.get("data") or {}
    resolved_order_id = data.get("order_id") or order_id

    # Construire la réponse conversationnelle
    response_text = _build_status_response(data)

    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": response_text,
        "order_tracking_context": {
            "order_focus": resolved_order_id,
            "last_status": data.get("status"),
            "last_interaction_ts": time.time(),
        },
        "ag_ui_component": None,
    }


# =====================================================================
# 2. DYNAMIC MENU — SNAPSHOT-BASED WITH 30-MIN EXPIRATION
# =====================================================================

async def list_orders(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    """Node : liste les commandes en format texte + snapshot menu éphémère."""
    phone = str(state.get("user_phone") or "")
    tracking_ctx = dict(state.get("order_tracking_context") or {})

    if not phone:
        return {
            "status": "ERROR",
            "response_strategy": "ERROR",
            "final_response": "Numéro de téléphone introuvable.",
            "ag_ui_component": None,
        }

    # Vérifier si le snapshot est expiré côté agent (30 min)
    cache_ts = tracking_ctx.get("menu_generated_at") or 0
    is_expired = (time.time() - cache_ts) > MENU_SESSION_TTL_SECONDS

    # Si l'utilisateur tape un numéro ET que le snapshot est encore valide
    payload: Dict[str, Any] = state.get("transaction_payload") or {}
    selection_idx = payload.get("selection_index")
    if selection_idx and not is_expired:
        resolved = _resolve_menu_selection(state, selection_idx)
        if resolved:
            # Contextualiser la sélection → status check
            synthetic_state = dict(state)
            synthetic_payload = dict(payload)
            synthetic_payload["order_id"] = resolved
            synthetic_state["transaction_payload"] = synthetic_payload
            return await check_order_status(synthetic_state, mc_runtime)

    # Rafraîchir la liste
    result = await _safe_tracking_call(mc_runtime, "get_buyer_orders_dashboard", phone=phone)

    if not is_success_response(result):
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": (
                "📦 *Vos Commandes :*\n\n"
                "Vous n'avez pas encore passé de commande sur AgriConnect.\n"
                "Tapez *marketplace* pour voir les produits disponibles ! 🛒"
            ),
            "ag_ui_component": None,
        }

    menu_text = result.get("formatted_menu") or "Vos commandes."
    raw_mapping = result.get("mapping") or {}
    minimal_mapping = {
        str(i): str(order_id)
        for i, order_id in enumerate(raw_mapping.values(), start=1)
        if order_id not in (None, "")
    }

    response = {
        "status": "WAITING_INPUT",
        "expected_input": "SELECTION",
        "response_strategy": "SELECTION_MENU",
        "final_response": menu_text + "\n\n_Répondez avec le numéro pour voir les détails._",
        "available_mapping": {},
        "expected_candidates": [],
        "order_tracking_context": {
            **tracking_ctx,
            "menu_generated_at": time.time(),
        },
        "ag_ui_component": None,
    }

    working_patch = {
        "available_mapping_kind": "order_list" if minimal_mapping else None,
    }
    response["working_memory"] = working_patch

    if minimal_mapping:
        response.update(
            {
                "available_mapping": minimal_mapping,
                "expected_candidates": [
                    f"Commande #{order_id[:8].upper()}"
                    for order_id in minimal_mapping.values()
                ],
            }
        )

    return response


# =====================================================================
# 3. CANCEL ACTION — Annulation conversationnelle directe
# =====================================================================

async def cancel_order(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    """Node : annulation conversationnelle d'une commande (ID texte ou menu)."""
    payload: Dict[str, Any] = state.get("transaction_payload") or {}
    phone = str(state.get("user_phone") or "")
    normalized_text = str(state.get("normalized_text") or "")
    tracking_ctx = dict(state.get("order_tracking_context") or {})

    # Résolution order_id
    order_id = (
        payload.get("order_id")
        or extract_order_ref(normalized_text)
    )

    # Via sélection numérique
    if not order_id and payload.get("selection_index") is not None:
        order_id = _resolve_menu_selection(state, payload["selection_index"])

    # Via focus
    if not order_id:
        order_id = tracking_ctx.get("order_focus")

    if not order_id:
        return {
            "status": "WAITING_INPUT",
            "expected_input": "ORDER_ID",
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": (
                "Quelle commande souhaitez-vous annuler ?\n\n"
                "💡 _Indiquez le numéro (ex: #ABC12345) ou tapez *mes commandes* "
                "pour voir la liste._"
            ),
            "ag_ui_component": None,
        }

    payload["order_id"] = str(order_id)

    cancel_reason = payload.get("cancel_reason") or payload.get("reason")
    if cancel_reason in (None, "", [], {}):
        return {
            "status": "WAITING_INPUT",
            "expected_input": "CANCELLATION_REASON",
            "response_strategy": "ASK_MISSING_FIELD",
            "final_response": (
                "Pour annuler cette commande, merci d'indiquer la raison.\n"
                "💡 Expliquez en quelques mots ce qui ne va pas (ex: délai trop long, erreur de produit)."
            ),
            "transaction_payload": payload,
            "order_tracking_context": {
                **tracking_ctx,
                "order_focus": order_id,
                "last_interaction_ts": time.time(),
            },
            "ag_ui_component": None,
        }

    # Appeler cancel_pending_order
    result = await _safe_tracking_call(
        mc_runtime, "cancel_pending_order",
        order_id=str(order_id),
        phone=phone,
        reason=str(cancel_reason).strip() or None,
    )

    status = str(result.get("status") or "").lower()
    msg = result.get("message") or ""

    if status == "success":
        cleaned_payload = {**payload}
        cleaned_payload.pop("cancel_reason", None)
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": (
                f"{msg}\n\n"
                f"🔁 _Souhaitez-vous passer une nouvelle commande ou "
                f"voir vos commandes actives ?_"
            ),
            "order_tracking_context": {
                **tracking_ctx,
                "order_focus": None,
                "last_interaction_ts": time.time(),
            },
            "transaction_payload": cleaned_payload,
            "ag_ui_component": None,
        }

    # Erreur métier → réponse de ré-engagement
    if "statut" in msg.lower() or "impossible" in msg.lower():
        return {
            "status": "COMPLETED",
            "response_strategy": "SUCCESS",
            "final_response": (
                f"⚠️ {msg}\n\n"
                f"Seules les commandes *en attente* (⏳) peuvent être annulées.\n"
                f"_Souhaitez-vous voir le statut actuel de cette commande ?_"
            ),
            "order_tracking_context": {
                **tracking_ctx,
                "order_focus": order_id,
                "last_interaction_ts": time.time(),
            },
            "ag_ui_component": None,
        }

    return _order_not_found_response(order_id, phone)


# =====================================================================
# 4. PROACTIVE GREETING — Vérification à l'ouverture de conversation
# =====================================================================

async def proactive_order_check(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Optional[Dict[str, Any]]:
    """Vérifie si l'acheteur a une commande en cours après inactivité.

    Retourne None si pas de commande pertinente (le flow continue normalement).
    Sinon retourne un patch d'état avec le message proactif.
    """
    phone = str(state.get("user_phone") or "")
    if not phone:
        return None

    # Vérifier l'inactivité (pas d'interaction récente)
    tracking_ctx = state.get("order_tracking_context") or {}
    last_ts = tracking_ctx.get("last_interaction_ts") or 0
    elapsed_since_last = time.time() - last_ts

    # Seulement si >1h d'inactivité
    if elapsed_since_last < 3600:
        return None

    result = await _safe_tracking_call(
        mc_runtime, "get_buyer_orders_dashboard", phone=phone
    )

    if not is_success_response(result):
        return None

    mapping = result.get("mapping") or {}
    if not mapping:
        return None

    # Prendre la première commande active
    first_order_id = list(mapping.values())[0] if mapping else None
    if not first_order_id:
        return None

    # Récupérer le détail
    detail = await _safe_tracking_call(
        mc_runtime, "get_transaction_summary", order_id=first_order_id
    )

    if not is_success_response(detail):
        return None

    data = detail.get("data") or {}
    status = str(data.get("status") or "").upper()
    order_number = data.get("order_number") or str(first_order_id)[:8].upper()
    status_label = _status_label(status)

    greeting = (
        f"👋 Bonjour ! Votre commande *#{order_number}* est actuellement :\n"
        f"{status_label}\n\n"
        f"_Souhaitez-vous des détails ? Tapez *oui* ou *statut*._"
    )

    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": greeting,
        "order_tracking_context": {
            "order_focus": first_order_id,
            "last_interaction_ts": time.time(),
            "menu_generated_at": time.time(),
        },
        "ag_ui_component": None,
    }


# =====================================================================
# 5. ORCHESTRATOR — Point d'entrée unique pour le buyer_context_resolver
# =====================================================================

async def order_tracking_resolver(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    """Orchestrateur du suivi de commande. Dispatche selon le goal.

    Goals supportés :
    - BUYER_CHECK_ORDER_STATUS → check_order_status
    - BUYER_LIST_ORDERS        → list_orders
    - BUYER_CANCEL_ORDER       → cancel_order
    """
    goal = str(state.get("current_goal") or "").upper().strip()

    if goal == "BUYER_CHECK_ORDER_STATUS":
        return await check_order_status(state, mc_runtime)
    elif goal == "BUYER_LIST_ORDERS":
        return await list_orders(state, mc_runtime)
    elif goal == "BUYER_CANCEL_ORDER":
        return await cancel_order(state, mc_runtime)

    # Fallback → list_orders (le plus sûr)
    return await list_orders(state, mc_runtime)


# =====================================================================
# INTERNAL HELPERS
# =====================================================================

async def _safe_tracking_call(
    mc_runtime: MarketRuntime, tool_name: str, **kwargs: Any
) -> Dict[str, Any]:
    """Wrapper résilient pour les appels MCP de tracking — delegates to gateway."""
    try:
        gw = OrderTrackingGateway(mc_runtime)
        return await gw._call(tool_name, **kwargs)
    except Exception as exc:
        logger.exception("_safe_tracking_call | tool=%s | error: %s", tool_name, exc)
        return {"status": "error", "message": "Service temporairement indisponible."}


def _order_not_found_response(order_id: Optional[str], phone: str) -> Dict[str, Any]:
    """Réponse conversationnelle quand une commande est introuvable.

    Transforme l'erreur en question de ré-engagement (jamais de message technique).
    """
    ref = f" *#{order_id[:8].upper()}*" if order_id else ""
    return {
        "status": "COMPLETED",
        "response_strategy": "SUCCESS",
        "final_response": (
            f"🔍 Je n'ai pas trouvé de commande{ref} dans votre historique.\n\n"
            f"Voulez-vous voir la liste de vos commandes actives ?\n"
            f"👉 Tapez *mes commandes*"
        ),
        "order_tracking_context": {
            "order_focus": None,
            "last_interaction_ts": time.time(),
        },
        "ag_ui_component": None,
        "pending_menu": MenuRequest(
            title="Commande introuvable",
            options=[
                MenuOption(index="1", label="📦 Voir mes commandes", value="BUYER_LIST_ORDERS"),
                MenuOption(index="2", label="🛒 Nouvelle commande", value="BUYER_ADD_TO_CART"),
            ],
            kind="order_recovery",
        ),
    }


__all__ = [
    "ORDER_TRACKING_GOALS",
    "MENU_SESSION_TTL_SECONDS",
    "STATUS_MAP",
    "extract_order_ref",
    "check_order_status",
    "list_orders",
    "cancel_order",
    "proactive_order_check",
    "order_tracking_resolver",
]
