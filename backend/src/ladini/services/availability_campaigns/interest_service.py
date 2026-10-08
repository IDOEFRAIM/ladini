"""Intérêt exprimé en réponse à une campagne — enregistrement idempotent, comparaison offre présentée / offre vivante,
notification producteur. UN INTÉRÊT N'EST JAMAIS UNE COMMANDE : aucune commande, réservation ni paiement ici.

La suite (panier, précommande, confirmation) reste le parcours d'achat existant et ses protections ; ce module ne
fait qu'ATTRIBUER la demande à la campagne et la tracer.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ladini.core.formatting import fmt_num
from ladini.domain.burkina_regions import resolve_region
from ladini.services.availability_campaigns import campaign_service, consent_service
from ladini.services.database.common import normalize_phone

logger = logging.getLogger("Ladini.Campaigns.Interest")

PRODUCER_TEMPLATE = "AVAILABILITY_INTEREST_PRODUCER"


def _fold(value: Any) -> str:
    s = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()
    return " ".join("".join(ch if ch.isalnum() else " " for ch in s).split())


def match_offers(product_label: str, offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Offres PRÉSENTÉES qui correspondent au produit demandé (nom contenu dans l'autre, accents/casse ignorés)."""
    want = _fold(product_label)
    if not want:
        return []
    out = []
    for o in offers or []:
        have = _fold(o.get("name"))
        if have and (want in have or have in want):
            out.append(o)
    return out


def compare_offer(presented: Dict[str, Any], live: Optional[Dict[str, Any]], requested_qty: Optional[float]) -> Dict[str, Any]:
    """Écarts entre l'offre présentée dans la campagne et l'offre VIVANTE. Vide = identique. Jamais de valeur inventée."""
    changes: Dict[str, Any] = {}
    if live is None or not live.get("is_available", False) or float(live.get("quantity") or 0) <= 0:
        changes["unavailable"] = True
        return changes
    pq, lq = float(presented.get("quantity") or 0), float(live.get("quantity") or 0)
    if abs(pq - lq) > 1e-9:
        changes["quantity"] = {"presented": pq, "live": lq}
    if (presented.get("price_label") or None) != (live.get("price_label") or None):
        changes["price"] = {"presented": presented.get("price_label"), "live": live.get("price_label")}
    if requested_qty is not None and float(requested_qty) > lq:
        changes["requested_exceeds_live"] = {"requested": float(requested_qty), "live": lq}
    return changes


def buyer_note(product_label: str, changes: Dict[str, Any], unit: Optional[str]) -> Optional[str]:
    """Phrase courte pour l'acheteur quand l'offre vivante diffère de celle de la campagne (None si identique)."""
    if not changes:
        return None
    u = f" {str(unit).lower()}" if unit else ""
    if changes.get("unavailable"):
        return f"ℹ️ Depuis notre message, « {product_label} » n'est plus disponible chez ce producteur."
    bits = []
    if "quantity" in changes:
        bits.append(f"il reste {fmt_num(changes['quantity']['live'])}{u} (au lieu de {fmt_num(changes['quantity']['presented'])}{u})")
    if "price" in changes:
        live_price = changes["price"]["live"] or "prix à confirmer"
        bits.append(f"le prix est maintenant : {live_price}")
    if "requested_exceeds_live" in changes and "quantity" not in changes:
        bits.append(f"il reste seulement {fmt_num(changes['requested_exceeds_live']['live'])}{u}")
    if not bits:
        return None
    return f"ℹ️ Depuis notre message, ça a changé pour « {product_label} » : " + " ; ".join(bits) + "."


async def live_offer(session: AsyncSession, product_id: str) -> Optional[Dict[str, Any]]:
    """Offre vivante (quantité, disponibilité, libellé de prix FIABLE) pour un produit présenté."""
    from sqlalchemy import select

    from ladini.domain.catalog.models import Product
    from ladini.domain.commercial_pricing_snapshot import (
        PricingReliability,
        product_pricing_view,
    )
    from ladini.domain.identity.models import Producer

    row = (
        await session.execute(
            select(Product, Producer).join(Producer, Producer.id == Product.producer_id).where(Product.id == product_id)
        )
    ).first()
    if row is None:
        return None
    product, producer = row
    view = product_pricing_view(product)
    reliable = view.reliability == PricingReliability.CERTIFIED or bool(view.tiers_label)
    return {
        "is_available": bool(product.is_available), "quantity": float(product.quantity_for_sale or 0),
        "price_label": view.pricing_label if reliable else None, "unit": product.unit,
        "producer_status": producer.status, "producer_user_id": str(producer.user_id) if producer.user_id else None,
        "producer_id": str(producer.id),
    }


async def record_interest(
    session: AsyncSession,
    phone: str,
    *,
    product_label: str,
    quantity: Optional[float] = None,
    unit: Optional[str] = None,
    message_ref: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Rattache la demande à la dernière campagne reçue (dans sa fenêtre) et enregistre l'intérêt, une seule fois
    par (destinataire, message entrant, produit). Retourne `attributed=False` quand rien ne relie ce message à une campagne."""
    now = now or datetime.utcnow()
    label = str(product_label or "").strip()
    norm = normalize_phone(phone, required=False)
    if not label or not norm:
        return {"status": "success", "attributed": False, "reason": "missing_input"}
    ctx = await campaign_service.mark_replied(session, norm, now=now)
    if ctx is None:
        return {"status": "success", "attributed": False, "reason": "no_recent_campaign"}
    matches = match_offers(label, ctx["offers"] or [])
    if not matches:
        return {"status": "success", "attributed": False, "reason": "no_offer_match", "campaign_id": ctx["campaign_id"]}

    ambiguous = len({m["product_id"] for m in matches}) > 1
    presented: Optional[Dict[str, Any]] = None
    live: Optional[Dict[str, Any]] = None
    changes: Dict[str, Any] = {}
    if ambiguous:
        changes["ambiguous_offers"] = sorted({m["product_id"] for m in matches})
    else:
        presented = matches[0]
        live = await live_offer(session, presented["product_id"])
        changes = compare_offer(presented, live, quantity)
    ref = consent_service.ref_hash(message_ref) or consent_service.ref_hash(f"{norm}|{label}|{quantity}") or "none"
    user_id = (await session.execute(text("select id from auth.users where phone=:p limit 1"), {"p": norm})).scalar()

    ins = (
        await session.execute(
            text(
                "insert into intelligence.availability_interests (campaign_id, recipient_id, phone, user_id, product_id, "
                "product_label, quantity, unit, kind, message_ref, changes) values (:c, :r, :p, :u, :pid, :lbl, :q, :unit, "
                "'INTEREST', :ref, cast(:chg as jsonb)) on conflict (recipient_id, message_ref, product_label) do nothing "
                "returning id"
            ),
            {"c": ctx["campaign_id"], "r": ctx["recipient_id"], "p": norm, "u": str(user_id) if user_id else None,
             "pid": presented["product_id"] if presented else None, "lbl": label[:120],
             "q": float(quantity) if quantity is not None else None, "unit": (str(unit).lower() if unit else None),
             "ref": ref, "chg": json.dumps(changes, default=str)},
        )
    ).first()
    created = ins is not None
    interest_id = str(ins[0]) if ins is not None else str(
        (
            await session.execute(
                text("select id from intelligence.availability_interests where recipient_id=:r and message_ref=:ref "
                     "and product_label=:lbl"),
                {"r": ctx["recipient_id"], "ref": ref, "lbl": label[:120]},
            )
        ).scalar()
    )
    notified = False
    if created and not ambiguous:
        try:
            async with session.begin_nested():  # un échec d'enqueue ne défait JAMAIS l'intérêt déjà enregistré
                notified = await notify_producer(session, interest_id, now=now)
        except Exception:  # noqa: BLE001 - repris par `notify_open_interests`
            logger.warning("CAMPAIGN_INTEREST_NOTIFY_FAILED | interest=%s", interest_id, exc_info=True)
    return {
        "status": "success", "attributed": True, "created": created, "interest_id": interest_id,
        "campaign_id": ctx["campaign_id"], "ambiguous": ambiguous, "changes": changes,
        "producer_notified": notified,
        "note": None if ambiguous else buyer_note(label, changes, unit or (live or {}).get("unit")),
    }


async def notify_producer(session: AsyncSession, interest_id: str, *, now: Optional[datetime] = None) -> bool:
    """Met en file UNE notification producteur par intérêt (`dedupe_key` = id d'intérêt : un retry n'en crée jamais deux).
    Pas de notification si l'offre n'est plus disponible ou le producteur non approuvé (rien d'exploitable à lui dire)."""
    from ladini.services.availability_campaigns.eligibility import (
        DEFAULT_APPROVED_PRODUCER_STATUSES,
    )

    row = (
        await session.execute(
            text(
                "select i.id, i.product_id, i.product_label, i.quantity, i.unit, i.user_id, i.status, i.phone "
                "from intelligence.availability_interests i where i.id = :id for update"
            ),
            {"id": interest_id},
        )
    ).mappings().first()
    if row is None or row["status"] != "OPEN" or row["product_id"] is None:
        return False
    live = await live_offer(session, str(row["product_id"]))
    if live is None or not live["is_available"] or live["quantity"] <= 0 or not live["producer_user_id"] \
            or str(live["producer_status"] or "").upper() not in DEFAULT_APPROVED_PRODUCER_STATUSES:
        return False
    producer_phone = (
        await session.execute(text("select phone from auth.users where id=:u"), {"u": live["producer_user_id"]})
    ).scalar()
    if not producer_phone:
        return False
    buyer_label, zone = None, None
    if row["user_id"]:
        u = (
            await session.execute(
                text("select name, declared_location from auth.users where id=:u"), {"u": str(row["user_id"])}
            )
        ).first()
        if u:
            buyer_label = (u[0] or "").strip() or None
            res = resolve_region(u[1])
            zone = res.region.name if res.status == "RESOLVED" and res.region is not None else None
    payload = {
        "product": row["product_label"], "quantity": float(row["quantity"]) if row["quantity"] is not None else None,
        "unit": row["unit"], "buyer_label": buyer_label or "un acheteur LADINI", "zone": zone, "interest_id": interest_id,
    }
    await session.execute(
            text(
                "insert into intelligence.notification_outbox (channel, recipient_user_id, recipient_phone, template_key, "
                "payload, dedupe_key) values ('WHATSAPP', :u, :p, :t, cast(:pl as jsonb), :d) "
                "on conflict (dedupe_key) do nothing"
            ),
            {"u": live["producer_user_id"], "p": producer_phone, "t": PRODUCER_TEMPLATE,
             "pl": json.dumps(payload, default=str), "d": f"availability_interest:{interest_id}"},
    )
    await session.execute(
        text("update intelligence.availability_interests set status='PRODUCER_NOTIFIED', producer_notified_at=:now, "
             "updated_at=now() where id=:id and status='OPEN'"),
        {"id": interest_id, "now": now or datetime.utcnow()},
    )
    return True


async def notify_open_interests(session: AsyncSession, *, limit: int = 100) -> int:
    """Reprise : intérêts enregistrés dont la notification producteur n'a pas pu être mise en file (OPEN)."""
    ids = [
        str(r[0])
        for r in (
            await session.execute(
                text("select id from intelligence.availability_interests where status='OPEN' and product_id is not null "
                     "order by created_at limit :n for update skip locked"),
                {"n": limit},
            )
        ).all()
    ]
    n = 0
    for i in ids:
        try:
            async with session.begin_nested():
                if await notify_producer(session, i):
                    n += 1
        except Exception:  # noqa: BLE001
            logger.warning("CAMPAIGN_INTEREST_RETRY_FAILED | interest=%s", i, exc_info=True)
    return n
