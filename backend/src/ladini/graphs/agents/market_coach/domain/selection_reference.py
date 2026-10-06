"""Résolution DÉTERMINISTE d'une RÉFÉRENCE naturelle vers une option visible d'un menu.

Principe (CONVERSATIONAL AUTONOMY) : un menu est un CONTEXTE, pas un protocole. Le modèle comprend le langage (« le quatrième »,
« Gilbert », « celui à 500 », « le moins cher », « celui de Ouaga ») et ne produit qu'une RÉFÉRENCE STRUCTURÉE — jamais un
identifiant. Ce module la compare, en Python, aux options RÉELLEMENT AFFICHÉES (le snapshot) :

    0 candidat   -> NOT_FOUND   (on le dit, sans réafficher tout le menu)
    1 candidat   -> EXACT       (sélection)
    N candidats  -> AMBIGUOUS   (clarification CIBLÉE avec les faits qui les distinguent)
    hors affichage-> NOT_VISIBLE (option jamais montrée : on ne la devine pas)

Jamais d'arbitraire : un critère subjectif (« le plus intéressant ») est AMBIGUOUS ; un critère objectif (moins cher, plus de stock)
n'est tranché que sur des options COMPARABLES (même unité), et une égalité reste AMBIGUOUS.

Ce n'est ni un routeur ni une taxonomie d'intentions : la relation au contexte (sélection / correction / interruption) reste celle
de B27 (`context_arbitration`) ; ce module résout seulement « QUELLE option ? ».
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, model_validator

from ladini.domain.burkina_regions import resolve_region


class Criterion(str, Enum):
    CHEAPEST = "CHEAPEST"
    HIGHEST_AVAILABILITY = "HIGHEST_AVAILABILITY"
    #: « le plus intéressant » : subjectif — jamais tranché par le système.
    SUBJECTIVE = "SUBJECTIVE"


class ReferenceType(str, Enum):
    ORDINAL = "ORDINAL"
    ATTRIBUTE = "ATTRIBUTE"
    PREFERENCE = "PREFERENCE"
    #: « montre les autres », « voir plus », « suite » : demande d'AFFICHER la suite de la liste (jamais une sélection).
    PAGINATION = "PAGINATION"
    #: « je préfère quelqu'un à Ouaga », « pas plus de 600 », « moins cher », « en sachet » : une CONTRAINTE de plus sur la
    #: recherche en cours (on restreint/trie les offres), jamais une sélection ni une sélection invalide.
    REFINEMENT = "REFINEMENT"
    #: « aucun ne me convient » : ni sélection ni interruption — on propose d'affiner ou de lancer une demande spéciale.
    NONE_OF_THESE = "NONE_OF_THESE"


class SelectionReference(BaseModel):
    """Référence STRUCTURÉE émise par le modèle (jamais un identifiant technique : Pydantic ignore tout champ non déclaré)."""

    reference_type: ReferenceType
    #: ORDINAL — rang dit (1-based) OU position relative.
    ordinal: Optional[int] = None
    position: Optional[Literal["LAST", "PENULTIMATE"]] = None
    #: ATTRIBUTE — tout ce que l'utilisateur a dit pour désigner l'option (cumulatif : tous doivent correspondre).
    producer_name: Optional[str] = None
    price: Optional[float] = None
    availability: Optional[float] = None
    region: Optional[str] = None
    packaging: Optional[str] = None
    #: Volume/quantité du conditionnement dit (« 500 ml » -> 0.5, « 1 litre » -> 1).
    volume: Optional[float] = None
    #: REFINEMENT — prix maximum dit (« pas plus de 600 »).
    max_price: Optional[float] = None
    #: PREFERENCE / REFINEMENT (tri : « moins cher », « plus de stock »)
    criterion: Optional[Criterion] = None

    model_config = {"frozen": True}

    @model_validator(mode="after")
    def _check(self) -> "SelectionReference":
        if self.reference_type == ReferenceType.ORDINAL:
            if (self.ordinal is None) == (self.position is None):
                raise ValueError("ORDINAL requiert exactement un de ordinal/position")
        elif self.reference_type == ReferenceType.PREFERENCE:
            if self.criterion is None:
                raise ValueError("PREFERENCE requiert criterion")
        elif self.reference_type in (ReferenceType.PAGINATION, ReferenceType.NONE_OF_THESE):
            return self
        elif self.reference_type == ReferenceType.REFINEMENT:
            if all(v is None for v in (self.region, self.packaging, self.volume, self.max_price, self.criterion, self.producer_name)):
                raise ValueError("REFINEMENT requiert au moins une contrainte")
        else:
            if all(v is None for v in (self.producer_name, self.price, self.availability, self.region, self.packaging, self.volume)):
                raise ValueError("ATTRIBUTE requiert au moins un attribut")
        return self


class Status(str, Enum):
    EXACT = "EXACT"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"
    NOT_VISIBLE = "NOT_VISIBLE"


@dataclass(frozen=True)
class VisibleOption:
    """Une option AFFICHÉE, avec les faits que l'utilisateur a pu lire (et donc citer)."""

    index: int
    entity_id: str
    name: str = ""
    region: str = ""
    price: Optional[float] = None
    unit: str = ""
    availability: Optional[float] = None
    #: Conditionnements proposés : (nom du conditionnement, volume en unité de base, prix).
    packagings: Tuple[Tuple[str, Optional[float], Optional[float]], ...] = ()
    price_label: str = ""
    comparable_price: bool = True


@dataclass(frozen=True)
class Resolution:
    status: Status
    indices: Tuple[int, ...] = ()
    reason: str = ""
    #: Clarification ciblée prête à afficher (AMBIGUOUS / NOT_FOUND / NOT_VISIBLE).
    message: str = ""
    criterion: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def index(self) -> Optional[int]:
        return self.indices[0] if self.status == Status.EXACT and self.indices else None


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _fmt_price(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:g}"


def _tokens(value: str) -> List[str]:
    return [t for t in _norm(value).split() if t]


def _name_matches(query: str, name: str) -> bool:
    q, n = _tokens(query), _tokens(name)
    return bool(q) and all(any(nt == qt or nt.startswith(qt) for nt in n) for qt in q)


def _region_slug(text: str) -> Optional[str]:
    hit = resolve_region(text)
    if hit.status == "RESOLVED" and hit.region is not None:
        return str(hit.region.slug)
    return None


def _region_matches(query: str, region: str) -> bool:
    if not region:
        return False
    qs, rs = _region_slug(query), _region_slug(region)
    if qs and rs:
        return qs == rs
    return _norm(query) == _norm(region) or (bool(_norm(query)) and _norm(query) in _norm(region))


def _price_matches(price: float, option: VisibleOption) -> bool:
    if option.price is not None and abs(option.price - price) < 1e-6:
        return True
    return any(p is not None and abs(p - price) < 1e-6 for _, _, p in option.packagings)


def _packaging_matches(query: Optional[str], volume: Optional[float], option: VisibleOption) -> bool:
    for name, vol, _price in option.packagings:
        if query and _norm(query) not in _norm(name):
            continue
        if volume is not None and (vol is None or abs(vol - volume) > 1e-6):
            continue
        return True
    return False


def _matches(ref: SelectionReference, option: VisibleOption) -> bool:
    if ref.producer_name and not _name_matches(ref.producer_name, option.name):
        return False
    if ref.price is not None and not _price_matches(ref.price, option):
        return False
    if ref.availability is not None and not (option.availability is not None and abs(option.availability - ref.availability) < 1e-6):
        return False
    if ref.region and not _region_matches(ref.region, option.region):
        return False
    if (ref.packaging or ref.volume is not None) and not _packaging_matches(ref.packaging, ref.volume, option):
        return False
    return True


def describe(option: VisibleOption, distinguishing: Sequence[str] = ("price", "region", "availability", "packaging")) -> str:
    """Description COURTE d'une option : son nom puis les seuls faits qui la distinguent des autres candidats."""
    parts: List[str] = []
    if "price" in distinguishing and (option.price_label or option.price is not None):
        parts.append(option.price_label or f"{_fmt_price(option.price)} FCFA/{option.unit}".rstrip("/"))
    if "region" in distinguishing and option.region:
        parts.append(option.region)
    if "availability" in distinguishing and option.availability is not None:
        parts.append(f"{option.availability:g} dispo")
    if "packaging" in distinguishing and option.packagings:
        parts.append(", ".join(sorted({n for n, _, _ in option.packagings if n})) or "")
    head = option.name or f"option {option.index}"
    tail = " — ".join(p for p in parts if p)
    return f"{head} ({tail})" if tail else head


def _distinguishing(options: Sequence[VisibleOption]) -> List[str]:
    keys: List[str] = []
    if len({(o.price_label or o.price) for o in options}) > 1:
        keys.append("price")
    if len({o.region for o in options}) > 1:
        keys.append("region")
    if len({o.availability for o in options}) > 1:
        keys.append("availability")
    if len({tuple(sorted(n for n, _, _ in o.packagings)) for o in options}) > 1:
        keys.append("packaging")
    return keys or ["price"]


def clarification(options: Sequence[VisibleOption], indices: Sequence[int], *, limit: int = 3) -> str:
    """« Tu parles de A (500 FCFA/L) ou de B (400 FCFA, sachet) ? » — jamais le menu complet."""
    chosen = [o for o in options if o.index in set(indices)][:limit]
    if len(chosen) < 2:
        return ""
    keys = _distinguishing(chosen)
    labelled = [f"{describe(o, keys)} [n°{o.index}]" for o in chosen]
    return "Tu parles de " + " ou de ".join(labelled) + " ?"


def _ordinal(ref: SelectionReference, shown: Sequence[VisibleOption], total: int) -> Resolution:  # noqa: C901
    if ref.position == "LAST":
        return Resolution(Status.EXACT, (shown[-1].index,)) if shown else Resolution(Status.NOT_FOUND, reason="no_options")
    if ref.position == "PENULTIMATE":
        if len(shown) < 2:
            return Resolution(Status.NOT_FOUND, reason="no_penultimate")
        return Resolution(Status.EXACT, (shown[-2].index,))
    n = int(ref.ordinal or 0)
    if any(o.index == n for o in shown):
        return Resolution(Status.EXACT, (n,))
    if shown and n > shown[-1].index and n <= total:
        return Resolution(Status.NOT_VISIBLE, reason="not_displayed", message=f"Je n'ai pas encore affiché l'option {n} — dis « montre les autres » pour la voir.")
    return Resolution(Status.NOT_FOUND, reason="ordinal_out_of_range", message=f"Il n'y a pas d'option {n} dans cette liste.")


def _preference(ref: SelectionReference, shown: Sequence[VisibleOption]) -> Resolution:
    crit = ref.criterion
    if crit == Criterion.SUBJECTIVE:
        return Resolution(
            Status.AMBIGUOUS, tuple(o.index for o in shown), "subjective", criterion=crit.value,
            message="« Intéressant » dépend de toi : tu préfères le moins cher, celui qui a le plus de stock, ou le plus proche ?",
        )
    if crit == Criterion.CHEAPEST:
        pool = [o for o in shown if o.price is not None and o.comparable_price]
        key, label = (lambda o: o.price), "cheapest"
    else:
        pool = [o for o in shown if o.availability is not None]
        key, label = (lambda o: -(o.availability or 0.0)), "highest_availability"
    if not pool:
        return Resolution(Status.NOT_FOUND, reason=f"{label}_no_data", criterion=crit.value if crit else None,
                          message="Je n'ai pas assez d'informations pour comparer ces offres sur ce critère.")
    if len({o.unit for o in pool}) > 1:
        return Resolution(Status.AMBIGUOUS, tuple(o.index for o in pool), f"{label}_incomparable_units", criterion=crit.value if crit else None,
                          message="Ces offres n'ont pas la même unité de prix, je ne peux pas les comparer directement. Dis-moi laquelle tu préfères.")
    best = min(key(o) for o in pool)
    winners = [o for o in pool if abs(key(o) - best) < 1e-9]
    if len(winners) == 1:
        return Resolution(Status.EXACT, (winners[0].index,), criterion=crit.value if crit else None)
    return Resolution(Status.AMBIGUOUS, tuple(o.index for o in winners), f"{label}_tie", criterion=crit.value if crit else None,
                      message=clarification(shown, [o.index for o in winners]))


def resolve_reference(
    ref: SelectionReference,
    options: Sequence[VisibleOption],
    *,
    displayed_count: Optional[int] = None,
    hidden_count: int = 0,
) -> Resolution:
    """Compare `ref` aux options AFFICHÉES (`options[:displayed_count]`). Pure, sans effet, sans LLM.

    `hidden_count` : offres existantes mais JAMAIS montrées (shortlist) — elles ne sont pas des candidats, mais une référence
    qui les vise (« le sixième ») reçoit « pas encore affiché » au lieu d'un « introuvable » trompeur."""
    ordered = sorted(options, key=lambda o: o.index)
    cut = len(ordered) if displayed_count is None else max(0, min(displayed_count, len(ordered)))
    shown = ordered[:cut]
    if not shown:
        return Resolution(Status.NOT_FOUND, reason="no_options")
    if ref.reference_type in (ReferenceType.PAGINATION, ReferenceType.NONE_OF_THESE, ReferenceType.REFINEMENT):
        return Resolution(Status.NOT_FOUND, reason="not_a_selection")
    if ref.reference_type == ReferenceType.ORDINAL:
        return _ordinal(ref, shown, len(ordered) + max(0, hidden_count))
    if ref.reference_type == ReferenceType.PREFERENCE:
        return _preference(ref, shown)

    hits = [o for o in shown if _matches(ref, o)]
    if len(hits) == 1:
        return Resolution(Status.EXACT, (hits[0].index,))
    if len(hits) > 1:
        return Resolution(Status.AMBIGUOUS, tuple(o.index for o in hits), "several_match", message=clarification(shown, [o.index for o in hits]))
    hidden = [o for o in ordered[cut:] if _matches(ref, o)]
    if hidden:
        return Resolution(Status.NOT_VISIBLE, tuple(o.index for o in hidden), "match_not_displayed",
                          message="Je n'ai pas encore affiché cette offre — dis « montre les autres » pour la voir.")
    return Resolution(Status.NOT_FOUND, reason="no_match", message="Je ne vois pas d'offre qui corresponde parmi celles affichées.")


def refine_options(ref: SelectionReference, options: Sequence[VisibleOption]) -> List[VisibleOption]:
    """REFINEMENT : restreint (région, conditionnement, prix max, nom) puis trie (moins cher / plus de stock) les offres COURANTES.

    Pur et déterministe ; la liste retournée peut être vide (aucune offre ne correspond) — l'appelant le dit, sans rien muter."""
    kept = [
        o for o in options
        if _matches(ref, o) and (ref.max_price is None or (o.price is not None and o.price <= ref.max_price + 1e-9))
    ]
    if ref.criterion == Criterion.CHEAPEST:
        kept.sort(key=lambda o: (o.price is None, o.price or 0.0))
    elif ref.criterion == Criterion.HIGHEST_AVAILABILITY:
        kept.sort(key=lambda o: -(o.availability or 0.0))
    return kept


# --------------------------------------------------------------------------------------------------------------------
# Construction des options VISIBLES depuis les snapshots existants (jamais depuis une nouvelle recherche)
# --------------------------------------------------------------------------------------------------------------------


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def option_from_vendor(vendor: Mapping[str, Any], index: int) -> VisibleOption:
    """Une ligne du snapshot `vendor_selection_context.vendors` -> faits visibles (nom, région, prix, stock, conditionnements)."""
    packagings: List[Tuple[str, Optional[float], Optional[float]]] = []
    for tier in vendor.get("pricing_tiers") or []:
        if isinstance(tier, Mapping):
            packagings.append((str(tier.get("packaging") or ""), _as_float(tier.get("base_unit_quantity") or tier.get("quantity")), _as_float(tier.get("price"))))
    basis = str(vendor.get("price_basis") or "").upper()
    return VisibleOption(
        index=index,
        entity_id=str(vendor.get("offer_id") or vendor.get("product_id") or ""),
        name=str(vendor.get("vendor_name") or ""),
        region=str(vendor.get("zone") or ""),
        price=_as_float(vendor.get("price")),
        unit=str(vendor.get("unit") or "").upper(),
        availability=_as_float(vendor.get("available_qty")),
        packagings=tuple(packagings),
        price_label=str(vendor.get("pricing_label") or ""),
        comparable_price=basis not in {"TOTAL_LOT", "LOT"},
    )


def options_from_vendors(vendors: Sequence[Mapping[str, Any]]) -> List[VisibleOption]:
    return [option_from_vendor(v, int(v.get("display_index") or i)) for i, v in enumerate(vendors, start=1) if isinstance(v, Mapping)]


def options_from_tiers(tiers: Sequence[Mapping[str, Any]]) -> List[VisibleOption]:
    """Conditionnements d'UNE offre : le nom visible est le conditionnement, le « volume » est la quantité de base."""
    out: List[VisibleOption] = []
    for i, tier in enumerate(tiers, start=1):
        if not isinstance(tier, Mapping):
            continue
        volume = _as_float(tier.get("base_unit_quantity") or tier.get("quantity"))
        price = _as_float(tier.get("price"))
        packaging = str(tier.get("packaging") or "")
        out.append(VisibleOption(
            index=i, entity_id=str(tier.get("tier_id") or ""), name=packaging, price=price, unit=str(tier.get("unit") or "").upper(),
            packagings=((packaging, volume, price),),
            price_label=f"{_fmt_price(price)} FCFA" if price is not None else "",
        ))
    return out


__all__ = [
    "Criterion",
    "ReferenceType",
    "Resolution",
    "SelectionReference",
    "Status",
    "VisibleOption",
    "clarification",
    "describe",
    "option_from_vendor",
    "options_from_tiers",
    "options_from_vendors",
    "refine_options",
    "resolve_reference",
]
