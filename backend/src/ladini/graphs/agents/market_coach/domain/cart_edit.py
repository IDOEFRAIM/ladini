"""Modification d'une LIGNE du panier — la sémantique d'édition que le domaine possédait pas encore.

    LA CONVERSATION INTERPRÈTE L'ÉDITION ; LE DOMAINE LA POSSÈDE.

Le modèle comprend « je voulais dire 20 pas 10 » (champ = quantité, valeur = 20, cible = la ligne lait). Il n'exécute RIEN : ce module (pur, sans I/O)
identifie la ligne visée, valide la valeur selon la sémantique du domaine, calcule la nouvelle ligne et le nouveau total. Le stock est re-vérifié par
l'appelant (`CartDomainService.edit_cart_line`) AVANT `commit_edit`. Le panier n'est jamais reconstruit : la ligne garde son identité (producteur, palier,
notification, métadonnées) ; seuls les champs dépendants de la valeur changent.

Invariants :
- cible = `line_id` canonique (hash produit/producteur/palier) ; jamais « le produit lait » comme seule autorité ; jamais « la première ligne » par défaut ;
- QUANTITÉ ≠ NOMBRE DE PAQUETS : sur une ligne à palier, une mesure (« 10 litres ») n'est JAMAIS convertie en silence en paquets — on demande ;
- 0, négatif, NaN, infini, unité incompatible : refusés ; retirer une ligne est une commande explicite (`REMOVE`) ;
- version (CAS) : si `expected_version` ne correspond plus à la version courante du panier -> CONFLICT, aucune mutation, aucun écrasement silencieux ;
- même valeur déjà en place -> UNCHANGED (idempotent : un webhook rejoué ne produit pas un second effet) ;
- le total vient TOUJOURS d'ici (prix × quantité), jamais du modèle.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, model_validator

from ladini.domain.quantity_unit import convert_quantity, normalize_unit
from ladini.graphs.agents.market_coach.domain.selection_reference import name_matches

#: Champs que le DOMAINE autorise à modifier. Tout autre champ (statut, prix, producteur…) est refusé : « mets le statut à PAID » n'est pas une édition.
EDITABLE_FIELDS = ("QUANTITY", "PACKAGE_COUNT", "REMOVE")


class CartField(str, Enum):
    QUANTITY = "QUANTITY"
    PACKAGE_COUNT = "PACKAGE_COUNT"
    REMOVE = "REMOVE"


class CartEditSpec(BaseModel):
    """Édition STRUCTURÉE émise par le modèle : ce qui change et quelle ligne est visée — jamais un identifiant, jamais du SQL."""

    field: CartField
    value: Optional[float] = None
    unit: Optional[str] = None
    #: Désignation de la ligne par ce que l'utilisateur a LU dans le panier.
    product: Optional[str] = None
    producer: Optional[str] = None
    #: Numéro de ligne affiché dans le panier (« la deuxième »).
    ordinal: Optional[int] = None

    model_config = {"frozen": True}

    @model_validator(mode="before")
    @classmethod
    def _tolerant(cls, data: Any) -> Any:
        """Lecteur tolérant (un vrai modèle écrit `"field": "quantity"` ou `"remove"` en minuscules)."""
        if isinstance(data, dict) and isinstance(data.get("field"), str):
            return {**data, "field": data["field"].strip().upper()}
        return data

    @model_validator(mode="after")
    def _check(self) -> "CartEditSpec":
        if self.field == CartField.REMOVE:
            return self
        if self.value is None:
            raise ValueError(f"{self.field.value} requiert value")
        return self


class EditStatus(str, Enum):
    APPLIED = "APPLIED"
    UNCHANGED = "UNCHANGED"
    CONFLICT = "CONFLICT"
    REJECTED = "REJECTED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class LineResolution:
    status: EditStatus
    line_id: Optional[str] = None
    candidates: Tuple[str, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class EditPlan:
    """Ce que l'édition ferait — SANS l'appliquer (le stock reste à vérifier)."""

    status: EditStatus
    line_id: Optional[str] = None
    field: Optional[str] = None
    old_value: Optional[float] = None
    new_value: Optional[float] = None
    new_line: Optional[Dict[str, Any]] = None
    #: Quantité en UNITÉ DE BASE à valider côté stock (None pour REMOVE / refus).
    base_quantity: Optional[float] = None
    message: str = ""


@dataclass(frozen=True)
class EditOutcome:
    status: EditStatus
    cart: List[Dict[str, Any]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    line_id: Optional[str] = None
    field: Optional[str] = None
    old_value: Optional[float] = None
    new_value: Optional[float] = None
    message: str = ""

    @property
    def mutated(self) -> bool:
        return self.status == EditStatus.APPLIED


# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# identité
# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────


def line_identity(line: Mapping[str, Any]) -> str:
    """Identité CANONIQUE d'une ligne : produit + producteur + palier (jamais le nom affiché). Stable d'un tour à l'autre."""
    raw = "|".join(str(line.get(k) or "") for k in ("product_id", "producer_id", "tier_id"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]  # noqa: S324 - identifiant, pas une primitive de sécurité


def with_line_ids(cart: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Copie du panier où chaque ligne porte son `line_id` (les lignes antérieures à cette phase n'en avaient pas)."""
    return [{**dict(line), "line_id": str(line.get("line_id") or line_identity(line))} for line in cart if isinstance(line, Mapping)]


def _is_tiered(line: Mapping[str, Any]) -> bool:
    return bool(line.get("tier_id"))


def describe_line(line: Mapping[str, Any]) -> str:
    """« 10 sachets de lait » / « 10 kg de tomates » — pour une clarification courte."""
    qty = line.get("quantity")
    unit = str(line.get("packaging") or line.get("unit") or "").lower()
    return f"{qty:g} {unit} de {line.get('name')}".replace("  ", " ") if isinstance(qty, (int, float)) else f"{line.get('name')}"


# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# résolution de la cible
# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────


def resolve_line(spec: CartEditSpec, cart: Sequence[Mapping[str, Any]]) -> LineResolution:
    """Quelle ligne ? Une seule -> EXACT ; aucune -> NOT_FOUND ; plusieurs (ou aucun repère sur un panier multi-lignes) -> AMBIGUOUS. Jamais la première par défaut."""
    lines = with_line_ids(cart)
    if not lines:
        return LineResolution(EditStatus.NOT_FOUND, message="Ton panier est vide : il n'y a rien à modifier.")

    pool = lines
    if spec.ordinal is not None:
        if not 1 <= spec.ordinal <= len(lines):
            return LineResolution(EditStatus.NOT_FOUND, message=f"Il n'y a pas de ligne {spec.ordinal} dans ton panier.")
        pool = [lines[spec.ordinal - 1]]
    if spec.product:
        pool = [ln for ln in pool if name_matches(spec.product, str(ln.get("name") or ""))]
    if spec.producer:
        pool = [ln for ln in pool if name_matches(spec.producer, str(ln.get("vendor_name") or ""))]

    if not pool:
        return LineResolution(EditStatus.NOT_FOUND, message="Je ne trouve pas cet article dans ton panier.")
    if len(pool) == 1:
        return LineResolution(EditStatus.APPLIED, line_id=str(pool[0]["line_id"]))
    options = " ou ".join(describe_line(ln) for ln in pool[:3])
    return LineResolution(
        EditStatus.AMBIGUOUS,
        candidates=tuple(str(ln["line_id"]) for ln in pool),
        message=f"Tu veux modifier {options} ?" if spec.value is None else f"Tu veux mettre {spec.value:g} pour {options} ?",
    )


# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# planification (pure)
# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────


def _reject(line_id: Optional[str], message: str) -> EditPlan:
    return EditPlan(EditStatus.REJECTED, line_id=line_id, message=message)


def plan_edit(spec: CartEditSpec, cart: Sequence[Mapping[str, Any]]) -> EditPlan:
    """Valide l'édition contre la sémantique du domaine et calcule la nouvelle ligne. N'écrit rien."""
    resolution = resolve_line(spec, cart)
    if resolution.status != EditStatus.APPLIED or resolution.line_id is None:
        return EditPlan(resolution.status, message=resolution.message)
    lines = with_line_ids(cart)
    line = next(ln for ln in lines if ln["line_id"] == resolution.line_id)
    lid = resolution.line_id

    if spec.field == CartField.REMOVE:
        return EditPlan(EditStatus.APPLIED, line_id=lid, field="REMOVE", old_value=_num(line.get("quantity")), new_line=None, message="")

    value = float(spec.value) if spec.value is not None else math.nan
    if not math.isfinite(value):
        return _reject(lid, "Cette quantité n'est pas valide.")
    if value <= 0:
        return _reject(lid, "La quantité doit être supérieure à zéro. Pour retirer cette ligne, dis « retire ».")

    tiered = _is_tiered(line)
    old_qty = _num(line.get("quantity"))

    if tiered:
        packaging = str(line.get("packaging") or "paquet")
        measure_unit = normalize_unit(spec.unit) if spec.unit else None
        content_unit = normalize_unit(str(line.get("unit") or ""))
        # Une MESURE (« 10 litres ») n'est pas un nombre de paquets : jamais de conversion silencieuse (règle du domaine « pas de tetris »).
        if spec.field == CartField.QUANTITY and measure_unit and content_unit and measure_unit == content_unit:
            return _reject(
                lid,
                f"« {value:g} {spec.unit} » est une quantité totale — ce conditionnement se commande par {packaging} de "
                f"{_fmt(line.get('tier_quantity'))} {line.get('unit')}. Combien de {packaging} veux-tu ?",
            )
        if not float(value).is_integer():
            return _reject(lid, f"Indique un nombre ENTIER de {packaging} (ex : 2, pas {value:g}).")
        count = int(value)
        price = _num(line.get("price")) or 0.0
        tier_q = _num(line.get("tier_quantity")) or 0.0
        new_line = {**line, "quantity": count, "base_unit_quantity": round(count * tier_q, 6), "line_total": round(price * count, 2)}
        base = new_line["base_unit_quantity"]
        new_value: float = float(count)
    else:
        if spec.field == CartField.PACKAGE_COUNT:
            return _reject(lid, "Cet article n'est pas vendu par paquets : indique une quantité (ex : 10 litres).")
        line_unit = normalize_unit(str(line.get("unit") or "")) or str(line.get("unit") or "")
        qty = value
        if spec.unit:
            wanted = normalize_unit(spec.unit)
            if wanted and line_unit and wanted != line_unit:
                converted = convert_quantity(value, wanted, line_unit)
                if converted is None:
                    return _reject(lid, f"Cet article est vendu en {str(line_unit).lower()}, pas en {wanted.lower()}. Indique la quantité en {str(line_unit).lower()}.")
                qty = converted
        price = _num(line.get("price")) or 0.0
        new_line = {**line, "quantity": qty, "line_total": round(price * qty, 2)}
        base = qty
        new_value = qty

    if old_qty is not None and abs(old_qty - new_value) < 1e-9:
        return EditPlan(EditStatus.UNCHANGED, line_id=lid, field=spec.field.value, old_value=old_qty, new_value=new_value, new_line=line, message="")
    return EditPlan(EditStatus.APPLIED, line_id=lid, field=spec.field.value, old_value=old_qty, new_value=new_value, new_line=new_line, base_quantity=base)


# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# application (pure) — versionnée
# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────


def cart_meta(cart: Sequence[Mapping[str, Any]], *, version: int) -> Dict[str, Any]:
    total = sum(float(ln.get("line_total") or 0.0) for ln in cart)
    return {"total_amount": round(total, 2), "currency": "XOF", "items_count": len(cart), "version": version}


def commit_edit(
    cart: Sequence[Mapping[str, Any]],
    plan: EditPlan,
    *,
    current_version: int,
    expected_version: Optional[int] = None,
) -> EditOutcome:
    """Applique `plan` au panier. CAS : `expected_version` (la version que l'utilisateur VOYAIT) doit être la version courante, sinon CONFLICT sans mutation."""
    lines = with_line_ids(cart)
    meta_now = cart_meta(lines, version=current_version)
    if plan.status in (EditStatus.NOT_FOUND, EditStatus.AMBIGUOUS, EditStatus.REJECTED):
        return EditOutcome(plan.status, cart=lines, meta=meta_now, line_id=plan.line_id, message=plan.message)
    if expected_version is not None and expected_version != current_version:
        return EditOutcome(
            EditStatus.CONFLICT, cart=lines, meta=meta_now, line_id=plan.line_id,
            message="Ton panier a changé entre-temps : voici sa version actuelle, redis-moi ce que tu veux modifier.",
        )
    if plan.status == EditStatus.UNCHANGED:
        return EditOutcome(EditStatus.UNCHANGED, cart=lines, meta=meta_now, line_id=plan.line_id, field=plan.field, old_value=plan.old_value, new_value=plan.new_value)

    if plan.field == "REMOVE":
        updated = [ln for ln in lines if ln["line_id"] != plan.line_id]
    else:
        updated = [plan.new_line if ln["line_id"] == plan.line_id and plan.new_line is not None else ln for ln in lines]
    return EditOutcome(
        EditStatus.APPLIED, cart=updated, meta=cart_meta(updated, version=current_version + 1),
        line_id=plan.line_id, field=plan.field, old_value=plan.old_value, new_value=plan.new_value,
    )


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt(value: Any) -> str:
    number = _num(value)
    return "" if number is None else f"{number:g}"


__all__ = [
    "EDITABLE_FIELDS",
    "CartEditSpec",
    "CartField",
    "EditOutcome",
    "EditPlan",
    "EditStatus",
    "LineResolution",
    "cart_meta",
    "commit_edit",
    "describe_line",
    "line_identity",
    "plan_edit",
    "resolve_line",
    "with_line_ids",
]
