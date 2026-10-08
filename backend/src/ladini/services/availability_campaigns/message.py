"""Rendu PUR (sans I/O) du message de campagne à partir des offres FIGÉES à la validation.

Court : une intro, 3 à 5 offres, une action simple, la consigne d'arrêt. Aucune explication longue.
Le prix n'apparaît que s'il est fiable (`price_label` rempli par l'éligibilité) ; sinon « prix à confirmer ».
Aucune valeur n'est calculée ni déduite ici : tout vient du snapshot de l'offre.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional

from ladini.core.formatting import fmt_num
from ladini.services.availability_campaigns.eligibility import (
    MAX_OFFERS_PER_MESSAGE,
    OfferCandidate,
    available_quantity,
)

_MONTHS = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)
PRICE_TO_CONFIRM = "prix à confirmer"
STOP_LINE = "Répondez STOP pour ne plus recevoir ces messages."
#: Version du texte de campagne (utile à la preuve de consentement et à l'audit).
TEMPLATE_VERSION = 1


def fmt_date_fr(d: Optional[date | datetime]) -> str:
    if d is None:
        return ""
    if isinstance(d, datetime):
        d = d.date()
    return f"{d.day} {_MONTHS[d.month - 1]}"


def offer_snapshot(c: OfferCandidate) -> Dict[str, Any]:
    """Représentation FIGÉE d'une offre (stockée dans `preview` et par destinataire) — JSON pur."""
    return {
        "product_id": c.product_id,
        "producer_id": c.producer_id,
        "name": c.name,
        "unit": str(c.unit).lower(),
        "quantity": available_quantity(c),
        "region": c.region,
        "quality_class": c.quality_class,
        "price_label": c.price_label,
        "price_status": c.price_status,
        "harvest_date": c.harvest_date.date().isoformat() if c.harvest_date else None,
        "seen_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def render_offer_line(index: int, offer: Dict[str, Any]) -> str:
    name = str(offer.get("name") or "").strip()
    qty = fmt_num(offer.get("quantity"))
    unit = str(offer.get("unit") or "").strip()
    head = f"{index}. {name} — {qty} {unit}".rstrip()
    where = str(offer.get("region") or "").strip()
    if where:
        head += f" ({where})"
    price = str(offer.get("price_label") or "").strip() or PRICE_TO_CONFIRM
    return f"{head} · {price}"


def render_campaign_message(
    offers: Iterable[Dict[str, Any]],
    *,
    delivery_date: Optional[date] = None,
    seen_on: Optional[date] = None,
) -> str:
    items: List[Dict[str, Any]] = list(offers)[:MAX_OFFERS_PER_MESSAGE]
    if not items:
        raise ValueError("render_campaign_message : aucune offre (une campagne vide n'est jamais envoyée)")
    lines = ["Bonjour, voici des disponibilités du moment sur LADINI :"]
    lines += [render_offer_line(i, o) for i, o in enumerate(items, 1)]
    tail = []
    if seen_on is not None:
        tail.append(f"Disponibilités déclarées au {fmt_date_fr(seen_on)}, à reconfirmer à la commande.")
    if delivery_date is not None:
        tail.append(f"Livraison possible à partir du {fmt_date_fr(delivery_date)}.")
    if tail:
        lines.append(" ".join(tail))
    lines.append("Cela vous intéresse ? Répondez par exemple « 100 kg de tomate ».")
    lines.append(STOP_LINE)
    return "\n".join(lines)


def content_hash(message: str, offers: Iterable[Dict[str, Any]]) -> str:
    """Empreinte stable du contenu validé : une modification après validation change le hash (détectable)."""
    blob = json.dumps({"m": message, "o": list(offers)}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
