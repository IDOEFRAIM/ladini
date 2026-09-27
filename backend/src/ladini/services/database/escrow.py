"""Escrow mixin (Paydunya) — séquestre de paiement pour sécuriser les commandes.

Toutes les méthodes s'appuient sur ``self.session`` (ContextVar unifié),
suivant le même contrat que ``BuyerMixin``/``ModerationMixin`` — aucune
ouverture de session ici (voir ``services/database/base.py::BaseMixin``).

Cycle de vie porté par ``Order.payment_status`` (colonne existante) :
    PENDING (facture générée, en attente de paiement)
      -> ESCROWED (Paydunya confirmé, OTP émis, fonds bloqués)
      -> PAID_OUT (code de livraison validé, fonds débloqués)
    CANCELLED / REFUNDED en sorties terminales (expiration, litige).
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ladini.core.formatting import fmt_num as _fmt_num
from ladini.core.settings import settings
from ladini.domain.analytics.emitter import BusinessEventEmitter
from ladini.domain.models import Order, OrderItem, Producer, Product, User
from ladini.domain.pricing_tiers import (
    resolve_stock_debit,
)
from ladini.services.payments.paydunya_client import PaydunyaClient, PaydunyaError

from .base import BaseMixin
from .errors import BusinessRuleException

logger = logging.getLogger("ladini.services.database.escrow")

MAX_PENDING_PAYMENT_ORDERS = 2


def _to_uuid(value: Any) -> Optional[uuid.UUID]:
    if value is None:
        return None
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _naive_utc(dt_value: Optional[datetime]) -> Optional[datetime]:
    """Datetime naïf UTC pour écriture DB (colonnes sans timezone)."""
    if not isinstance(dt_value, datetime):
        return None
    if dt_value.tzinfo is not None:
        return dt_value.astimezone(timezone.utc).replace(tzinfo=None)
    return dt_value


def _generate_otp() -> str:
    """4 chiffres, généré via ``secrets`` (CSPRNG) — jamais ``random`` pour un
    code qui protège un déblocage de fonds.

    4 chiffres = 10 000 possibilités seulement : c'est le VERROUILLAGE de
    ``verify_delivery_otp`` (``_OTP_MAX_ATTEMPTS``/``_OTP_LOCKOUT``), et non
    l'entropie du code, qui rend la force brute impraticable. Ne jamais
    supprimer l'un en supposant que l'autre suffit.
    """
    return f"{secrets.randbelow(10000):04d}"


# Anti-force-brute du code de livraison (audit sécurité 2026-09-10).
#
# `verify_delivery_otp` n'avait AUCUN compteur de tentatives. Avec un code de
# 4 chiffres, un producteur pouvait donc simplement deviner : chaque essai est
# un message WhatsApp, mais rien ne l'arrêtait — ni verrouillage, ni délai, ni
# trace. Pire, la requête cherchait le code parmi TOUTES les commandes ESCROWED
# du producteur à la fois : avec N livraisons en cours, un seul essai testait N
# codes d'un coup, donc l'espace de recherche effectif était divisé par N.
# L'effet d'un succès est le déblocage des fonds (PAID_OUT + DELIVERED) sans
# aucune confirmation de l'acheteur.
#
# 5 essais puis 15 minutes de blocage plafonne le débit à ~20 essais/heure :
# ~250 heures d'attaque continue pour une chance sur deux, contre quelques
# minutes auparavant. Le compteur est PERSISTÉ en base (pas en mémoire du
# process : il doit survivre à un redéploiement et être partagé entre tous
# les workers Celery).
_OTP_MAX_ATTEMPTS = 5
_OTP_LOCKOUT = timedelta(minutes=15)


class EscrowMixin(BaseMixin):
    """Séquestre de paiement Paydunya : facture -> confirmation -> OTP -> déblocage."""

    async def initiate_escrow_payment(
        self,
        buyer_phone: str,
        preorder_id: str,
        delivery_lat: Optional[float] = None,
        delivery_lon: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Génère une facture Paydunya pour une précommande DRAFT et réserve 24h.

        Ne débite PAS le stock (comme toute précommande DRAFT — voir
        ``confirm_preorder_draft``) : le stock n'est débité qu'à la confirmation
        réelle du paiement (``mark_escrow_paid``), jamais avant.

        ``delivery_lat``/``delivery_lon`` (optionnels) figent le point GPS de
        livraison sur la commande — miroir de ``confirm_preorder_draft``
        (BuyerMixin) pour le chemin escrow. Gap réel comblé le 2026-08-18 : ce
        threading manquait depuis l'intégration escrow ; resté sans impact
        tant qu'``ESCROW_PAYMENT_ENABLED`` était False (le chemin non-escrow,
        seul actif, l'appliquait déjà), mais serait devenu une régression
        silencieuse (précommandes escrow livrées sans point GPS) dès la
        réactivation du paiement Paydunya. Voir
        [[gps-delivery-burkina-faso-2026-08]].
        """
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        if delivery_lat is not None and delivery_lon is not None:
            from ladini.core.geofencing import is_within_burkina_faso

            if not is_within_burkina_faso(delivery_lat, delivery_lon):
                raise BusinessRuleException(
                    "Le point de livraison est hors du Burkina Faso.",
                    reason="out_of_country",
                )

        o_uuid = _to_uuid(preorder_id)
        if o_uuid is None:
            raise BusinessRuleException("Identifiant précommande invalide.")

        _, profile_obj = await self.get_buyer_profile(phone=buyer_phone)

        # Anti-fraude : pas plus de 2 commandes en attente de paiement à la fois.
        pending_count = int(
            await current_session.scalar(
                select(func.count())
                .select_from(Order)
                .where(
                    Order.buyer_id == profile_obj.id,
                    Order.payment_status == "PENDING",
                    Order.paydunya_invoice_token.isnot(None),
                )
            )
            or 0
        )
        if pending_count >= MAX_PENDING_PAYMENT_ORDERS:
            raise BusinessRuleException(
                "Vous avez déjà 2 commandes en attente de paiement. "
                "Payez ou laissez expirer l'une d'elles avant d'en créer une nouvelle.",
                reason="too_many_pending_payments",
            )

        order = await current_session.scalar(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == o_uuid, Order.buyer_id == profile_obj.id)
            .with_for_update()
        )
        if not order:
            raise BusinessRuleException("Précommande introuvable ou non autorisée.")
        if str(order.status or "").upper() != "DRAFT":
            raise BusinessRuleException(
                f"Cette précommande n'est pas en brouillon (statut={order.status}).",
                reason="not_draft",
            )

        amount = float(order.total_amount or 0.0)
        if amount <= 0:
            raise BusinessRuleException(
                "Montant de commande invalide pour un paiement."
            )

        callback_url = (
            f"{str(settings.PUBLIC_API_BASE_URL).rstrip('/')}/api/webhooks/paydunya-ipn"
        )
        client = PaydunyaClient()
        try:
            invoice = await client.create_invoice(
                order_id=str(order.id),
                amount=amount,
                description=f"Commande LADINI #{str(order.id)[:8].upper()}",
                callback_url=callback_url,
                custom_data={"buyer_phone": buyer_phone},
            )
        except PaydunyaError as exc:
            logger.error(
                "ESCROW_INVOICE_CREATE_FAILED | order_id=%s | %s", order.id, exc
            )
            raise BusinessRuleException(
                "Impossible de générer le lien de paiement pour le moment. Réessayez dans un instant."
            ) from exc

        ttl_hours = int(settings.PAYDUNYA_PAYMENT_TTL_HOURS or 24)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)

        order.paydunya_invoice_token = invoice["invoice_token"]
        order.payment_status = "PENDING"
        order.payment_method = "PAYDUNYA"
        order.payment_expires_at = _naive_utc(expires_at)
        order.locked_amount = amount
        if delivery_lat is not None and delivery_lon is not None:
            order.gps_lat = delivery_lat
            order.gps_lng = delivery_lon
        await current_session.flush()

        return {
            "status": "success",
            "order_id": str(order.id),
            "order_number": str(order.id)[:8].upper(),
            "checkout_url": invoice["checkout_url"],
            "amount": amount,
            "currency": order.currency or "XOF",
            "expires_at": expires_at.isoformat(),
            "ttl_hours": ttl_hours,
            "message": (
                f"Votre commande est réservée pendant {ttl_hours}h. "
                f"Payez {_fmt_num(amount)} {order.currency or 'XOF'} via ce lien sécurisé : "
                f"{invoice['checkout_url']}. Au-delà de ce délai, la commande sera annulée."
            ),
        }

    async def mark_escrow_paid(self, invoice_token: str) -> Dict[str, Any]:
        """Appelée par le webhook APRÈS re-confirmation serveur-à-serveur (jamais
        directement depuis le corps de l'IPN). Débite le stock (réutilise la
        logique de ``confirm_preorder_draft``), passe ``payment_status=ESCROWED``,
        génère l'OTP et enfile les 2 notifications (acheteur+producteur) via
        l'Outbox — jamais d'envoi Twilio direct ici : ce n'est pas un tour de
        conversation, rien n'attend de réponse HTTP synchrone.
        """
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        order = await current_session.scalar(
            select(Order)
            .options(selectinload(Order.items).joinedload(OrderItem.product))
            .where(Order.paydunya_invoice_token == invoice_token)
            .with_for_update()
        )
        if not order:
            raise BusinessRuleException(
                "Commande introuvable pour ce token de paiement.",
                reason="order_not_found",
            )

        # Idempotence : Paydunya peut livrer le même IPN plusieurs fois.
        if str(order.payment_status or "").upper() in {"ESCROWED", "PAID_OUT"}:
            return {
                "status": "success",
                "order_id": str(order.id),
                "already_processed": True,
            }

        if str(order.status or "").upper() != "DRAFT":
            raise BusinessRuleException(
                f"Commande dans un état inattendu pour confirmation de paiement (statut={order.status}).",
                reason="unexpected_status",
            )

        # Débit stock + passage CONFIRMED : réutilise exactement la logique
        # existante de confirm_preorder_draft (verrouillage FOR UPDATE par
        # produit, vérification de disponibilité, calcul du total).
        insufficient: List[Dict[str, Any]] = []
        running_total = 0.0
        for item in order.items or []:
            if not item.product_id:
                continue
            product = await current_session.scalar(
                select(Product).where(Product.id == item.product_id).with_for_update()
            )
            if not product:
                insufficient.append(
                    {"product_id": str(item.product_id), "reason": "product_not_found"}
                )
                continue
            requested = float(item.quantity or 0.0)
            # Voir domain/pricing_tiers.py — quantité en unité de BASE à
            # débiter, distincte de `requested` (nombre de paquets/palier)
            # dès qu'un `tier_id` est impliqué.
            stock_debit = resolve_stock_debit(item)
            available = float(product.quantity_for_sale or 0.0)
            if available < stock_debit:
                insufficient.append(
                    {
                        "product_id": str(product.id),
                        "name": product.name,
                        "requested": stock_debit,
                        "available": available,
                        "unit": (product.unit or "KG").upper(),
                    }
                )
                continue
            product.quantity_for_sale = available - stock_debit
            await BusinessEventEmitter(current_session).emit_product_quantity_changed(
                product, previous_quantity=available, source="order_debit_escrow"
            )
            running_total += float(item.price_at_sale or 0.0) * requested

        if insufficient:
            # Le paiement a déjà été pris ; le stock a fondu entre-temps. On ne
            # débite rien de plus et on n'annule pas l'argent bloqué en
            # silence — la commande reste ESCROWED pour traitement manuel
            # (remboursement/litige, cf. OrderDispute) plutôt que de perdre la
            # trace du paiement.
            logger.error(
                "ESCROW_STOCK_SHORTAGE_AFTER_PAYMENT | order_id=%s | insufficient=%s",
                order.id,
                insufficient,
            )

        otp = _generate_otp()
        order.delivery_otp = otp
        # Nouveau code = nouveau budget de tentatives (et lève un éventuel
        # verrou hérité) : le compteur suit le code, pas la commande.
        order.delivery_otp_attempts = 0
        order.delivery_otp_locked_until = None
        order.payment_status = "ESCROWED"
        order.status = "CONFIRMED"
        order.locked_amount = float(
            order.locked_amount or running_total or order.total_amount or 0.0
        )
        order.preorder_converted_at = _naive_utc(datetime.now(timezone.utc))
        if running_total:
            order.subtotal = running_total
            order.total_amount = running_total
        await current_session.flush()

        # Analytics : le paiement escrow sécurisé fait sortir la commande de DRAFT ET la fait passer
        # CONFIRMED (engagement ferme) dans la même transaction.
        emitter = BusinessEventEmitter(current_session)
        await emitter.emit_direct_order_created(order)
        await emitter.emit_direct_order_confirmed(order, actor_type="SYSTEM")

        # Notifications — Outbox (pattern existant), jamais d'envoi direct ici.
        from ladini.workers.outbox import templates as _tpl
        from ladini.workers.repositories import outbox_repo as _outbox_repo

        order_number = str(order.id)[:8].upper()
        entries: List[Dict[str, Any]] = [
            {
                "channel": "WHATSAPP",
                "recipient_phone": order.customer_phone,
                "template_key": _tpl.ESCROW_PAYMENT_RECEIVED_BUYER,
                "payload": {"otp": otp, "order_number": order_number},
                "dedupe_key": f"ESCROW_PAID_BUYER:{order.id}",
            }
        ]

        producer_phones: set[str] = set()
        for item in order.items or []:
            if item.product and item.product.producer_id:
                prod_row = (
                    await current_session.execute(
                        select(User.phone)
                        .join(Producer, Producer.user_id == User.id)
                        .where(Producer.id == item.product.producer_id)
                        .limit(1)
                    )
                ).first()
                if prod_row and prod_row[0]:
                    producer_phones.add(prod_row[0])

        for phone in producer_phones:
            entries.append(
                {
                    "channel": "WHATSAPP",
                    "recipient_phone": phone,
                    "template_key": _tpl.ESCROW_PAYMENT_SECURED_PRODUCER,
                    "payload": {
                        "order_number": order_number,
                        "amount": float(order.locked_amount or 0.0),
                        "currency": order.currency or "FCFA",
                    },
                    "dedupe_key": f"ESCROW_PAID_PRODUCER:{order.id}:{phone}",
                }
            )

        await _outbox_repo.enqueue(current_session, entries)

        return {
            "status": "success",
            "order_id": str(order.id),
            "order_number": order_number,
        }

    async def verify_delivery_otp(
        self, producer_phone: str, otp_code: str
    ) -> Dict[str, Any]:
        """Le producteur transmet le code reçu de l'acheteur pour débloquer ses fonds.

        Cherche UNIQUEMENT parmi les commandes ESCROWED dont un des articles
        appartient à ce producteur — jamais une recherche d'OTP globale (un
        code à 4 chiffres n'est unique que par commande, pas dans l'absolu).
        """
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        clean_code = "".join(ch for ch in str(otp_code or "") if ch.isdigit())
        if len(clean_code) != 4:
            raise BusinessRuleException(
                "Le code de livraison doit contenir exactement 4 chiffres."
            )

        prod_row = (
            await current_session.execute(
                select(Producer.id)
                .join(User, User.id == Producer.user_id)
                .where(User.phone == producer_phone)
                .limit(1)
            )
        ).first()
        if not prod_row:
            raise BusinessRuleException("Profil producteur introuvable.")
        producer_id = prod_row[0]

        now = datetime.now(timezone.utc)
        now_naive = _naive_utc(now)

        # Charge TOUTES les commandes ESCROWED de ce producteur (verrouillées),
        # au lieu de filtrer directement sur `delivery_otp == clean_code` : c'est
        # ce qui permet de COMPTABILISER un essai raté. Une requête qui ne
        # ramène rien ne dit pas contre combien de codes l'essai a porté, donc
        # ne peut rien incrémenter — c'était la raison structurelle de l'absence
        # de limite de tentatives (audit 2026-09-10).
        candidates = list(
            (
                await current_session.scalars(
                    select(Order)
                    .join(OrderItem, OrderItem.order_id == Order.id)
                    .join(Product, Product.id == OrderItem.product_id)
                    .where(
                        Product.producer_id == producer_id,
                        Order.payment_status == "ESCROWED",
                    )
                    .with_for_update(of=Order)
                )
            ).unique()
        )

        def _is_locked(candidate: Order) -> bool:
            locked_until = candidate.delivery_otp_locked_until
            return locked_until is not None and locked_until > now_naive

        unlocked = [c for c in candidates if not _is_locked(c)]

        if candidates and not unlocked:
            # Toutes verrouillées : ne PAS comparer le code (sinon le
            # verrouillage n'en serait pas un) et ne pas prolonger la peine.
            retry_at = min(
                c.delivery_otp_locked_until
                for c in candidates
                if c.delivery_otp_locked_until is not None
            )
            wait_min = max(1, int((retry_at - now_naive).total_seconds() // 60) + 1)
            logger.warning(
                "ESCROW_OTP_LOCKED | producer_id=%s | orders=%d | retry_in_min=%s",
                producer_id,
                len(candidates),
                wait_min,
            )
            raise BusinessRuleException(
                f"Trop de codes incorrects. Réessayez dans {wait_min} minute(s), "
                "ou demandez le code à l'acheteur.",
                reason="otp_locked",
            )

        order = next((c for c in unlocked if c.delivery_otp == clean_code), None)

        if not order:
            # Essai raté : il a porté contre chacune des commandes déverrouillées
            # (la requête d'origine les testait toutes simultanément), donc on
            # incrémente chacune d'elles — c'est la comptabilité fidèle de ce
            # qui vient d'être tenté.
            locked_now = 0
            for candidate in unlocked:
                candidate.delivery_otp_attempts = int(
                    candidate.delivery_otp_attempts or 0
                ) + 1
                if candidate.delivery_otp_attempts >= _OTP_MAX_ATTEMPTS:
                    candidate.delivery_otp_locked_until = _naive_utc(
                        now + _OTP_LOCKOUT
                    )
                    candidate.delivery_otp_attempts = 0
                    locked_now += 1
            await current_session.flush()

            # Ne JAMAIS journaliser `clean_code` : ce log serait un dictionnaire
            # des codes déjà essayés, et un code valide finirait par y passer.
            logger.warning(
                "ESCROW_OTP_MISMATCH | producer_id=%s | candidates=%d | locked_now=%d",
                producer_id,
                len(unlocked),
                locked_now,
            )
            if locked_now:
                raise BusinessRuleException(
                    "Trop de codes incorrects. La validation est bloquée "
                    f"{int(_OTP_LOCKOUT.total_seconds() // 60)} minutes — "
                    "redemandez le code à l'acheteur.",
                    reason="otp_locked",
                )
            raise BusinessRuleException(
                "Code invalide ou aucune commande en attente de livraison ne correspond.",
                reason="otp_mismatch",
            )

        order.payment_status = "PAID_OUT"
        order.delivery_status = "DELIVERED"
        order.confirmed_at = now_naive
        # Code consommé : le compteur repart à zéro et le code est retiré, pour
        # qu'il ne puisse pas être rejoué sur une commande future.
        order.delivery_otp_attempts = 0
        order.delivery_otp_locked_until = None
        order.delivery_otp = None
        await current_session.flush()

        await BusinessEventEmitter(current_session).emit_order_delivered(order)

        return {
            "status": "success",
            "order_id": str(order.id),
            "order_number": str(order.id)[:8].upper(),
            "amount": float(order.locked_amount or order.total_amount or 0.0),
            "currency": order.currency or "FCFA",
            "message": (
                "Code valide ! Livraison confirmée. Vos fonds sont débloqués et "
                "seront transférés sur votre compte."
            ),
        }

    async def list_producer_escrowed_orders(
        self, producer_phone: str
    ) -> Dict[str, Any]:
        """Lecture — commandes ESCROWED en attente de code pour ce producteur
        (utile si plusieurs livraisons sont en cours en même temps)."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        prod_row = (
            await current_session.execute(
                select(Producer.id)
                .join(User, User.id == Producer.user_id)
                .where(User.phone == producer_phone)
                .limit(1)
            )
        ).first()
        if not prod_row:
            return {"status": "success", "data": []}
        producer_id = prod_row[0]

        rows = (
            (
                await current_session.execute(
                    select(Order)
                    .join(OrderItem, OrderItem.order_id == Order.id)
                    .join(Product, Product.id == OrderItem.product_id)
                    .where(
                        Product.producer_id == producer_id,
                        Order.payment_status == "ESCROWED",
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )

        return {
            "status": "success",
            "data": [
                {
                    "order_id": str(o.id),
                    "order_number": str(o.id)[:8].upper(),
                    "amount": float(o.locked_amount or o.total_amount or 0.0),
                }
                for o in rows
            ],
        }

    async def mark_escrow_payment_failed(self, invoice_token: str) -> Dict[str, Any]:
        """(2026-09-03, clôture escrow/IPN) : Paydunya re-confirme une
        facture "cancelled" (rejet explicite, distinct d'une simple
        expiration TTL — voir ``expire_pending_payments``). Avant ce
        correctif, ce cas n'écrivait RIEN nulle part (``Order.payment_status``
        restait ``PENDING`` indéfiniment) — un trou de cohérence réel entre
        ``Order`` et l'intention utilisateur, jamais visible ni côté
        acheteur ni côté support.

        Même garde-fous que ``mark_escrow_paid`` : ``SELECT...FOR UPDATE``
        + idempotence explicite (rejouer le même IPN "cancelled" ne
        ré-annule rien de plus)."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        order = await current_session.scalar(
            select(Order)
            .where(Order.paydunya_invoice_token == invoice_token)
            .with_for_update()
        )
        if not order:
            raise BusinessRuleException(
                "Commande introuvable pour ce token de paiement.",
                reason="order_not_found",
            )

        if str(order.payment_status or "").upper() in {"ESCROWED", "PAID_OUT"}:
            # Le paiement a en réalité DÉJÀ été confirmé ailleurs (course
            # entre deux IPN contradictoires, ou rejeu tardif d'un
            # "cancelled" périmé) — ne JAMAIS régresser un paiement acquis.
            # Mandat RÈGLE ABSOLUE : ne transforme jamais un succès en échec
            # sans preuve — ici la preuve (ESCROWED/PAID_OUT) dit le
            # contraire du "cancelled" reçu, donc ce dernier est ignoré.
            return {"status": "success", "order_id": str(order.id), "already_processed": True}

        if str(order.payment_status or "").upper() == "CANCELLED":
            return {"status": "success", "order_id": str(order.id), "already_processed": True}

        order.payment_status = "CANCELLED"
        order.status = "CANCELLED"
        order.cancellation_role = "PAYMENT_PROVIDER_REJECTED"
        await current_session.flush()

        return {"status": "success", "order_id": str(order.id), "already_processed": False}

    async def expire_pending_payments(self) -> Dict[str, Any]:
        """Cron (``workers/crons/order_expiry.py``) : annule les commandes dont
        le délai de paiement est dépassé. Ne touche JAMAIS une commande déjà
        ESCROWED/PAID_OUT — uniquement ``payment_status == PENDING``."""
        current_session = self.session
        if not current_session:
            raise BusinessRuleException("Session indisponible.")

        now = _naive_utc(datetime.now(timezone.utc))
        rows = (
            (
                await current_session.execute(
                    select(Order)
                    .where(
                        Order.payment_status == "PENDING",
                        Order.paydunya_invoice_token.isnot(None),
                        Order.payment_expires_at.isnot(None),
                        Order.payment_expires_at < now,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )

        expired_ids: List[str] = []
        for order in rows:
            order.payment_status = "CANCELLED"
            order.status = "CANCELLED"
            order.cancellation_role = "SYSTEM_EXPIRY"
            expired_ids.append(str(order.id))

        if rows:
            await current_session.flush()

        return {
            "status": "success",
            "expired_count": len(expired_ids),
            "expired_order_ids": expired_ids,
        }


__all__ = ["EscrowMixin", "MAX_PENDING_PAYMENT_ORDERS"]
