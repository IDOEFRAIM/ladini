"""Templates de messages + quick-actions « 1 clic » (frictionless).

Le rendu se fait au moment de l'ENVOI (dispatcher), à partir du ``template_key``
et du ``payload`` stockés dans l'outbox. Moins le destinataire fait d'étapes,
plus le taux de réponse est élevé : chaque template embarque une action rapide.
"""

from __future__ import annotations

from typing import Any, Dict

from ladini.core.formatting import fmt_num as _fmt_num

AUCTION_INVITE_PRODUCER = "AUCTION_INVITE_PRODUCER"
NEW_PRODUCT_ALERT_BUYER = "NEW_PRODUCT_ALERT_BUYER"
# (Phase 4, approvisionnement récurrent) : UN digest par (acheteur, date) — jamais une notification
# par besoin/producteur (voir `workers/automation/recurring_supply_digest_service.py`). Le corps est
# déjà entièrement rendu par `domain/recurring_supply/digest.py::build_digest_text` (fonction pure,
# testée séparément) au moment de l'ENQUEUE — `payload["body"]` porte directement le texte final,
# contrairement aux autres templates qui rendent depuis des champs structurés à l'ENVOI. Choix
# délibéré : le digest dépend d'un calcul (agrégation, tri, somme) déjà fait et testé côté service,
# le refaire ici dupliquerait cette logique sans aucun bénéfice.
RECURRING_SUPPLY_DIGEST_BUYER = "RECURRING_SUPPLY_DIGEST_BUYER"
# VS5 pilote (livraison/réception) : envoyé quand le producteur signale
# `MARK_DELIVERED` (`RecurringSupplyMixin.mark_order_delivery_status`) —
# demande la réception, jamais une simple information passive (mandat
# ÉTAPE 6 : "Votre commande est-elle arrivée correctement ?").
RECURRING_SUPPLY_ORDER_DELIVERED_BUYER = "RECURRING_SUPPLY_ORDER_DELIVERED_BUYER"
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
# (2026-09-04, clôture F1 — paiement à la livraison) : le producteur
# déclare, en un seul geste, avoir livré ET été payé en espèces
# (`ProducerMgmtMixin.confirm_delivery_and_payment`) — l'acheteur n'est
# jamais notifié de cette clôture autrement (contrairement au chemin
# escrow, il n'a rien payé en ligne à confirmer avant).
ORDER_COMPLETED_AT_DELIVERY_BUYER = "ORDER_COMPLETED_AT_DELIVERY_BUYER"
# (2026-09-04, F2 — notification producteur préorder direct) : miroir de
# `ESCROW_PAYMENT_SECURED_PRODUCER` pour le chemin SANS escrow
# (`ESCROW_PAYMENT_ENABLED=False`, `BuyerMixin.confirm_preorder_draft`) —
# jamais "paiement reçu" (rien n'a été payé en ligne), uniquement "nouvelle
# commande confirmée, payable à la livraison".
PREORDER_CONFIRMED_PRODUCER = "PREORDER_CONFIRMED_PRODUCER"
# (2026-09-04, F3 — producteur perdant une enchère) : voir
# `AuctionMixin.select_winning_bid`.
AUCTION_LOST_PRODUCER = "AUCTION_LOST_PRODUCER"
# (2026-09-04, audit produit post-F1-F4 — fermeture du gap
# `BUYER_CANCEL_ORDER` structurellement inatteignable) : le producteur
# n'était jusqu'ici jamais informé quand un acheteur annule une commande
# déjà `CONFIRMED` (`BuyerMixin.cancel_pending_order`) — il ne l'apprenait
# qu'en tentant, plus tard, une clôture livraison/paiement qui échouerait
# silencieusement de son point de vue.
ORDER_CANCELLED_BY_BUYER_PRODUCER = "ORDER_CANCELLED_BY_BUYER_PRODUCER"
# (2026-09-04, Phase 5 — décision produit #1) : miroir du template
# ci-dessus dans l'autre sens. Le producteur ne pouvait pas se rétracter
# après `CONFIRMED` ; désormais si, et l'acheteur doit l'apprendre
# autrement qu'en attendant une livraison qui ne viendra pas. Ne parle
# JAMAIS de remboursement : le paiement a lieu à la livraison, rien n'a
# été encaissé.
ORDER_CANCELLED_BY_PRODUCER_BUYER = "ORDER_CANCELLED_BY_PRODUCER_BUYER"
# (2026-09-13, confirmation explicite producteur) : une précommande directe
# (paiement à la livraison) n'est plus `CONFIRMED` dès sa création — elle
# entre en `PENDING_PRODUCER_CONFIRMATION` et le producteur doit
# explicitement confirmer pouvoir l'honorer
# (`ProducerMgmtMixin.confirm_order_by_producer`) avant que l'acheteur ne
# soit rassuré que sa commande est réellement prise en charge. Miroir de
# `ORDER_CANCELLED_BY_PRODUCER_BUYER` dans le sens positif.
ORDER_CONFIRMED_BY_PRODUCER_BUYER = "ORDER_CONFIRMED_BY_PRODUCER_BUYER"


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
    # Mandat B2c.4 : `pricing_label` (certifié) prime — sinon un lot TOTAL_LOT s'afficherait
    # "X FCFA/unité" dans cette alerte proactive.
    pricing_label = p.get("pricing_label")
    if pricing_label:
        price_line = f" à *{pricing_label}*"
    else:
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


def _render_order_completed_at_delivery_buyer(p: Dict[str, Any]) -> str:
    order_number = str(p.get("order_number") or "")
    amount = _fmt_num(p.get("amount"))
    currency = str(p.get("currency") or "FCFA")
    return (
        f"✅ *Commande #{order_number} livrée et payée !*\n\n"
        f"Le producteur a confirmé avoir remis votre commande et reçu "
        f"*{amount} {currency}* à la livraison. Transaction clôturée — merci "
        "de votre confiance !"
    )


def _render_preorder_confirmed_producer(p: Dict[str, Any]) -> str:
    order_number = str(p.get("order_number") or "")
    amount = _fmt_num(p.get("amount"))
    currency = str(p.get("currency") or "FCFA")
    items_summary = str(p.get("items_summary") or "").strip()
    items_line = f"🛒 {items_summary}\n" if items_summary else ""
    # (2026-09-11) Le producteur voit maintenant CE QUI a été commandé en
    # premier — avant, seuls le code de commande et le montant étaient
    # affichés ("Nouvelle commande confirmée ! #65280745 — 1000000 CFA"),
    # ce qui ne dit rien de ce que l'acheteur veut réellement recevoir. Le
    # code reste présent (référence pour "mes commandes"), mais en second.
    #
    # (2026-09-13, confirmation explicite producteur) : cette commande n'est
    # PLUS automatiquement acceptée à ce stade (`PENDING_PRODUCER_
    # CONFIRMATION`, pas `CONFIRMED`) — le texte invite désormais à une
    # action explicite plutôt qu'à simplement "préparer".
    return (
        f"🛒 *Nouvelle commande — confirmation requise*\n"
        f"{items_line}"
        f"💵 Total : *{amount} {currency}* (paiement à la livraison)\n"
        f"🆔 Référence : #{order_number}\n\n"
        "❓ Pouvez-vous honorer cette commande ?\n"
        "👉 Tapez *confirmer* pour l'accepter (l'acheteur en sera informé), "
        "ou *annuler* si vous ne pouvez pas."
    )


def _render_auction_lost_producer(p: Dict[str, Any]) -> str:
    product = p.get("product") or "ce produit"
    return (
        f"📋 Votre offre pour *{product}* n'a pas été retenue cette fois — "
        "un autre producteur a été choisi.\n\n"
        "💡 Tapez *mes appels d'offres* pour voir d'autres opportunités."
    )


def _render_order_cancelled_by_buyer_producer(p: Dict[str, Any]) -> str:
    order_number = str(p.get("order_number") or "")
    return (
        f"❌ *Commande annulée* — #{order_number}\n\n"
        "L'acheteur a annulé cette commande avant livraison. Aucune action "
        "de votre part n'est nécessaire."
    )


def _render_order_cancelled_by_producer_buyer(p: Dict[str, Any]) -> str:
    order_number = str(p.get("order_number") or "")
    reason = str(p.get("reason") or "").strip()
    reason_line = f"\n📝 Motif indiqué : {reason}" if reason else ""
    return (
        f"❌ *Commande annulée par le producteur* — #{order_number}"
        f"{reason_line}\n\n"
        "Le producteur ne peut finalement pas honorer cette commande. "
        "Vous n'avez rien payé : le règlement se fait à la livraison.\n"
        "🔎 Tapez *chercher <produit>* pour trouver un autre vendeur."
    )


def _render_recurring_supply_order_delivered_buyer(p: Dict[str, Any]) -> str:
    order_number = str(p.get("order_number") or "")
    return (
        f"🚚 Votre commande #{order_number} a été livrée.\n\n"
        "Votre commande est-elle arrivée correctement ?\n\n"
        "1. ✅ Tout est bon\n"
        "2. ⚠️ Il y a un problème"
    )


def _render_order_confirmed_by_producer_buyer(p: Dict[str, Any]) -> str:
    order_number = str(p.get("order_number") or "")
    return (
        f"✅ *Commande confirmée par le producteur* — #{order_number}\n\n"
        "Le producteur a confirmé pouvoir honorer votre commande et va la "
        "préparer. Vous payez à la livraison.\n"
        "🔎 Tapez *mes commandes* pour suivre son statut."
    )


def _render_recurring_supply_digest_buyer(p: Dict[str, Any]) -> str:
    return str(p.get("body") or "Vous avez une nouvelle disponibilité d'approvisionnement Ladini.")


_RENDERERS = {
    AUCTION_INVITE_PRODUCER: _render_auction_invite,
    RECURRING_SUPPLY_DIGEST_BUYER: _render_recurring_supply_digest_buyer,
    RECURRING_SUPPLY_ORDER_DELIVERED_BUYER: _render_recurring_supply_order_delivered_buyer,
    NEW_PRODUCT_ALERT_BUYER: _render_new_product_alert,
    AUCTION_WON_PRODUCER: _render_auction_won,
    PREORDER_RESERVED_PRODUCER: _render_preorder_reserved,
    ESCROW_PAYMENT_RECEIVED_BUYER: _render_escrow_payment_received_buyer,
    ESCROW_PAYMENT_SECURED_PRODUCER: _render_escrow_payment_secured_producer,
    ESCROW_PAYMENT_FAILED_BUYER: _render_escrow_payment_failed_buyer,
    ESCROW_PAYMENT_EXPIRED_BUYER: _render_escrow_payment_expired_buyer,
    ORDER_COMPLETED_AT_DELIVERY_BUYER: _render_order_completed_at_delivery_buyer,
    PREORDER_CONFIRMED_PRODUCER: _render_preorder_confirmed_producer,
    AUCTION_LOST_PRODUCER: _render_auction_lost_producer,
    ORDER_CANCELLED_BY_BUYER_PRODUCER: _render_order_cancelled_by_buyer_producer,
    ORDER_CANCELLED_BY_PRODUCER_BUYER: _render_order_cancelled_by_producer_buyer,
    ORDER_CONFIRMED_BY_PRODUCER_BUYER: _render_order_confirmed_by_producer_buyer,
}


def render(template_key: str, payload: Dict[str, Any]) -> str:
    """Rend le corps du message. Fallback neutre si le template est inconnu."""
    renderer = _RENDERERS.get(template_key)
    if renderer is None:
        return str(
            (payload or {}).get("fallback")
            or "Vous avez une nouvelle notification Ladini."
        )
    return renderer(payload or {})
