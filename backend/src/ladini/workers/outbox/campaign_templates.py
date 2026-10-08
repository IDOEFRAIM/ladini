"""Rendus des messages de campagne de disponibilités (outbox). Fonctions pures, sans dépendance au dispatcher."""

from __future__ import annotations

from typing import Any, Dict

from ladini.core.formatting import fmt_num


def render_availability_campaign(p: Dict[str, Any]) -> str:
    """Le corps est rendu à la MISE EN FILE (offres figées) ; ici on le restitue tel quel, jamais un texte générique."""
    return str(p.get("body") or "")


def render_body(p: Dict[str, Any]) -> str:
    """Corps déjà rendu à la mise en file (campagne, question de consentement)."""
    return str(p.get("body") or "")


def render_availability_interest_producer(p: Dict[str, Any]) -> str:
    """Intérêt exploitable pour le producteur — jamais une commande, aucune donnée superflue (pas de téléphone)."""
    qty = fmt_num(p.get("quantity"))
    unit = str(p.get("unit") or "").lower()
    product = str(p.get("product") or "").strip()
    what = f"{qty} {unit} de {product}".strip() if qty and product else (product or "votre produit")
    lines = [f"Un acheteur est intéressé par {what}."]
    if str(p.get("buyer_label") or "").strip():
        lines.append(f"Acheteur : {str(p['buyer_label']).strip()}")
    if p.get("zone"):
        lines.append(f"Zone : {p['zone']}")
    if p.get("desired_date"):
        lines.append(f"Date souhaitée : {p['desired_date']}")
    lines.append("État : intérêt exprimé, pas encore une commande.")
    return "\n".join(lines)
