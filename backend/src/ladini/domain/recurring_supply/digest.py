"""Rendu du digest d'approvisionnement restaurant + du détail d'un besoin — fonctions PURES, sans
DB, sans LLM, déterministes (mandat Phase 4 §20). Le moteur de matching (Phase 3) reste silencieux ;
ce module ne fait que TRADUIRE un état déjà calculé en texte WhatsApp.

Mandat §CONTRAINTE MAJEURE : une allocation est une disponibilité indicative, jamais une réservation
— aucun mot ici ne doit promettre "réservé"/"garanti"/"commande confirmée".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence


@dataclass(frozen=True)
class NeedAvailability:
    """Un besoin (occurrence) tel qu'affiché dans le digest — jamais d'ID/version/producer_id."""

    product: str
    requested_quantity: Decimal
    matched_quantity: Decimal
    unit: str

    @property
    def coverage_label(self) -> str:
        """`"40/40 kg"` plutôt qu'un pourcentage abstrait (mandat §15) — le pourcentage reste
        disponible séparément (`coverage_ratio`) pour l'observabilité/l'analytique."""
        return f"{_fmt(self.matched_quantity)}/{_fmt(self.requested_quantity)} {self.unit}"

    @property
    def coverage_ratio(self) -> float:
        if self.requested_quantity <= 0:
            return 0.0
        return min(float(self.matched_quantity) / float(self.requested_quantity), 1.0)

    @property
    def emoji(self) -> str:
        if self.matched_quantity <= 0:
            return "❌"
        if self.matched_quantity >= self.requested_quantity:
            return "✅"
        return "⚠️"


@dataclass(frozen=True)
class AllocationLine:
    """Une ligne fournisseur du détail — nom d'affichage uniquement (mandat §17 : jamais de
    téléphone, UUID ou donnée interne)."""

    producer_label: str
    quantity: Decimal
    unit_price: Decimal
    unit: str

    @property
    def line_total(self) -> Decimal:
        return self.quantity * self.unit_price


def _fmt(value: Decimal) -> str:
    """Entier si la valeur est ronde, sinon 2 décimales — jamais de zéros parasites (`40` pas
    `40.000`, comme le reste de l'UX déjà en place, cf. `core/formatting.py::fmt_num`)."""
    normalized = value.normalize() if value == value.to_integral_value() else value.quantize(Decimal("0.01"))
    s = format(normalized, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


# =====================================================================
# DIGEST — un message par (buyer, date), jamais un par besoin (mandat §2)
# =====================================================================


def build_digest_text(needs: Sequence[NeedAvailability]) -> str:
    """Pure, déterministe : mêmes `needs` (même ordre) → même texte, toujours. L'appelant est
    responsable de l'ordre (le service DB trie par libellé produit pour un rendu stable). `needs`
    n'est jamais vide en pratique (le service n'enqueue un digest que s'il y a au moins un besoin
    pertinent) mais reste géré explicitement pour que cette fonction n'ait aucune précondition cachée."""
    if not needs:
        return "🌾 Approvisionnement de demain\n\nAucun besoin récurrent pertinent pour demain."

    total_matched = Decimal(0)
    total_requested = Decimal(0)
    full_count = 0
    body_lines = []
    for need in needs:
        body_lines.append(f"{need.emoji} {need.product.capitalize()} : {need.coverage_label} disponibles")
        total_matched += need.matched_quantity
        total_requested += need.requested_quantity
        if need.matched_quantity >= need.requested_quantity and need.requested_quantity > 0:
            full_count += 1

    lines = [
        "🌾 Approvisionnement de demain",
        "",
        *body_lines,
        "",
        f"Disponibilité globale : {_fmt(total_matched)}/{_fmt(total_requested)}",
        f"{full_count} de vos {len(needs)} besoins ont une disponibilité complète.",
        "",
        "1. Voir les détails",
        "2. Mes besoins",
    ]
    return "\n".join(lines)


def digest_counts(needs: Sequence[NeedAvailability]) -> dict:
    """Compteurs pour la télémétrie (mandat §19) — jamais recalculés deux fois différemment."""
    full = sum(1 for n in needs if n.matched_quantity >= n.requested_quantity and n.requested_quantity > 0)
    partial = sum(1 for n in needs if 0 < n.matched_quantity < n.requested_quantity)
    none_ = sum(1 for n in needs if n.matched_quantity <= 0)
    return {"occurrence_count": len(needs), "full_count": full, "partial_count": partial, "unavailable_count": none_}


# =====================================================================
# DÉTAIL — un besoin, ses fournisseurs, jamais de bouton "confirmer" ici
# =====================================================================


def build_detail_text(
    *,
    product: str,
    requested_quantity: Decimal,
    unit: str,
    allocations: Sequence[AllocationLine],
    confirmable: bool = False,
) -> str:
    matched_quantity = sum((a.quantity for a in allocations), Decimal(0))
    lines = [f"🍅 {product.capitalize()} — demain", ""]
    lines.append(f"Besoin : {_fmt(requested_quantity)} {unit}")
    lines.append(f"Disponibilité trouvée : {_fmt(matched_quantity)} {unit}")
    lines.append("")

    if not allocations:
        lines.append("Aucune disponibilité pour le moment.")
    else:
        total = Decimal(0)
        for a in allocations:
            lines.append(a.producer_label)
            lines.append(f"{_fmt(a.quantity)} {a.unit} — {_fmt(a.unit_price)} FCFA/{a.unit}")
            lines.append("")
            total += a.line_total
        lines.append(f"Total estimé : {_fmt(total)} FCFA")

    lines.append("")
    if confirmable:
        # Phase 5 (VS4 pilote) : une allocation reste indicative jusqu'à CETTE confirmation —
        # le stock n'est débité et la commande créée qu'à l'action "Confirmer" (mandat CONTRAINTE
        # MAJEURE ci-dessus toujours respecté : rien n'est "réservé" avant ce geste explicite).
        lines.append("Voulez-vous confirmer cet approvisionnement ?")
        lines.append("")
        lines.append("1. Confirmer")
        lines.append("2. Pas cette fois")
        lines.append("3. Retour")
    else:
        lines.append("Cette disponibilité sera vérifiée lors de votre confirmation.")
        lines.append("")
        lines.append("1. Retour")
        lines.append("2. Mes besoins")
    return "\n".join(lines)


# =====================================================================
# SIGNATURE — dédup du digest (mandat §9/§10)
# =====================================================================


def digest_signature(occurrence_versions: Sequence[tuple]) -> str:
    """`[(occurrence_id, version), ...]` → hash court et stable, indépendant de l'ordre d'entrée
    (trié explicitement) — mêmes occurrences + mêmes versions -> MÊME signature -> MÊME
    `dedupe_key` -> `outbox_repo.enqueue` l'ignore silencieusement (ON CONFLICT DO NOTHING)."""
    parts = sorted(f"{occ_id}:{version}" for occ_id, version in occurrence_versions)
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def dedupe_key_for_digest(buyer_id, occurrence_date, signature: str) -> str:
    return f"recurring_supply_digest:{buyer_id}:{occurrence_date.isoformat()}:{signature}"


__all__ = [
    "NeedAvailability",
    "AllocationLine",
    "build_digest_text",
    "digest_counts",
    "build_detail_text",
    "digest_signature",
    "dedupe_key_for_digest",
]
