"""Templates de messages + quick-actions « 1 clic » (frictionless).

Le rendu se fait au moment de l'ENVOI (dispatcher), à partir du ``template_key``
et du ``payload`` stockés dans l'outbox. Moins le destinataire fait d'étapes,
plus le taux de réponse est élevé : chaque template embarque une action rapide.
"""

from __future__ import annotations

from typing import Any, Dict

from agriconnect.core.formatting import fmt_num as _fmt_num

AUCTION_INVITE_PRODUCER = "AUCTION_INVITE_PRODUCER"
NEW_PRODUCT_ALERT_BUYER = "NEW_PRODUCT_ALERT_BUYER"
AUCTION_WON_PRODUCER = "AUCTION_WON_PRODUCER"
PREORDER_RESERVED_PRODUCER = "PREORDER_RESERVED_PRODUCER"
ESCROW_PAYMENT_RECEIVED_BUYER = "ESCROW_PAYMENT_RECEIVED_BUYER"
ESCROW_PAYMENT_SECURED_PRODUCER = "ESCROW_PAYMENT_SECURED_PRODUCER"
# (2026-09-03, clôture escrow/IPN PREORDER) : jusqu'ici, un paiement
# Paydunya rejeté ou expiré (TTL) annulait la commande SANS notifier
# l'acheteur — gap réel comblé ici, même convention que les 2 templates
# escrow existants.
ESCROW_PAYMENT_FAILED_BUYER = "ESCROW_PAYMENT_FAILED_BUYER"
ESCROW_PAYMENT_EXPIRED_BUYER = "ESCROW_PAYMENT_EXPIRED_BUYER"


def _render_auction_invite(p: Dict[str, Any]) -> str:
    product = p.get("product") or "un produit"
    qty = _fmt_num(p.get("quantity"))
    unit = str(p.get("unit") or "").upper()
    price = _fmt_num(p.get("max_price"))
    ref = str(p.get("ref") or "")
    qty_line = f"*{qty} {unit}*".strip() if qty else "une quantité"
    return (
        "📢 *Nouvelle opportunité près de chez vous !*\n\n"
        f"Un acheteur recherche {qty_line} de *{product}* "
        f"(prix max *{price} FCFA/{unit}*).\n\n"
        "💰 *Proposez votre prix en 1 clic :*\n"
        f"👉 Répondez *OFFRE {ref} <votre prix>*\n"
        f"_Exemple : OFFRE {ref} {price or '5000'}_"
    )


def _render_new_product_alert(p: Dict[str, Any]) -> str:
    product = p.get("product") or "un nouveau produit"
    producer = p.get("producer_name") or "un producteur local"
    price = _fmt_num(p.get("price"))
    unit = str(p.get("unit") or "").upper()
    ref = str(p.get("ref") or "")
    price_line = f" à *{price} FCFA/{unit}*" if price else ""
    return (
        "🌾 *Nouveau produit disponible près de chez vous !*\n\n"
        f"*{product}*{price_line} chez {producer}.\n\n"
        "🛒 *Commandez en 1 clic :*\n"
        f"👉 Répondez *ACHETER {ref}* pour lancer votre commande."
    )


def _render_auction_won(p: Dict[str, Any]) -> str:
    product = p.get("product") or "votre produit"
    qty = _fmt_num(p.get("quantity"))
    unit = str(p.get("unit") or "").upper()
    total = _fmt_num(p.get("total"))
    qty_line = f"{qty} {unit}" if qty else ""
    return (
        "🎉 *Bonne nouvelle !*\n\n"
        f"Votre offre pour {qty_line} de *{product}* a été retenue !\n"
        f"💵 Montant du marché : *{total} FCFA*\n\n"
        "Veuillez contacter l'acheteur pour coordonner les détails de la livraison.\n"
        "📊 Tapez *mes offres* pour voir le détail."
    )


def _render_preorder_reserved(p: Dict[str, Any]) -> str:
    product = p.get("product") or "votre production"
    qty = _fmt_num(p.get("quantity"))
    unit = str(p.get("unit") or "").upper()
    reserved = _fmt_num(p.get("reserved_total"))
    available = _fmt_num(p.get("available"))
    eta = p.get("eta")
    eta_line = f"\n📅 Attendu pour le *{eta}*." if eta else ""
    cap_line = ""
    if reserved and available:
        cap_line = f"\n📊 Réservé au total : *{reserved}/{available} {unit}*."
    return (
        "🔔 *Nouvelle précommande !*\n\n"
        f"Un acheteur a réservé *{qty} {unit}* de *{product}*.{eta_line}{cap_line}\n\n"
        "📊 Tapez *mes réservations* pour voir le détail et contacter l'acheteur."
    )


def _render_escrow_payment_received_buyer(p: Dict[str, Any]) -> str:
    otp = str(p.get("otp") or "----")
    order_number = str(p.get("order_number") or "")
    return (
        f"✅ *Paiement reçu !* Commande #{order_number} sécurisée.\n\n"
        f"🔐 Votre code de livraison secret est le *{otp}*.\n\n"
        "⚠️ Ne le donnez au producteur que LORSQUE vous aurez la marchandise "
        "en main — c'est ce code qui débloque son paiement."
    )


def _render_escrow_payment_secured_producer(p: Dict[str, Any]) -> str:
    order_number = str(p.get("order_number") or "")
    amount = _fmt_num(p.get("amount"))
    currency = str(p.get("currency") or "FCFA")
    return (
        f"✅ *L'acheteur a payé !* Commande #{order_number} — "
        f"*{amount} {currency}* bloqués et sécurisés.\n\n"
        "📦 Veuillez livrer la commande. Une fois livré, demandez le code à "
        "4 chiffres à l'acheteur et envoyez-le moi ici pour recevoir vos fonds."
    )


def _render_escrow_payment_failed_buyer(p: Dict[str, Any]) -> str:
    order_number = str(p.get("order_number") or "")
    return (
        f"❌ *Paiement non abouti* pour la commande #{order_number} — "
        "aucune somme n'a été débitée. Vous pouvez recommencer votre précommande."
    )


def _render_escrow_payment_expired_buyer(p: Dict[str, Any]) -> str:
    order_number = str(p.get("order_number") or "")
    return (
        f"⏱️ Le délai de paiement de la commande #{order_number} est dépassé — "
        "elle a été annulée, aucune somme n'a été débitée."
    )


_RENDERERS = {
    AUCTION_INVITE_PRODUCER: _render_auction_invite,
    NEW_PRODUCT_ALERT_BUYER: _render_new_product_alert,
    AUCTION_WON_PRODUCER: _render_auction_won,
    PREORDER_RESERVED_PRODUCER: _render_preorder_reserved,
    ESCROW_PAYMENT_RECEIVED_BUYER: _render_escrow_payment_received_buyer,
    ESCROW_PAYMENT_SECURED_PRODUCER: _render_escrow_payment_secured_producer,
    ESCROW_PAYMENT_FAILED_BUYER: _render_escrow_payment_failed_buyer,
    ESCROW_PAYMENT_EXPIRED_BUYER: _render_escrow_payment_expired_buyer,
}


def render(template_key: str, payload: Dict[str, Any]) -> str:
    """Rend le corps du message. Fallback neutre si le template est inconnu."""
    renderer = _RENDERERS.get(template_key)
    if renderer is None:
        return str(
            (payload or {}).get("fallback")
            or "Vous avez une nouvelle notification AgriConnect."
        )
    return renderer(payload or {})
