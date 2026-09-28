"""Certification du PRIX d'un bid conversationnel (Phase B2b).

« 450000 la tonne » ne doit JAMAIS finir en `offered_price = 450000` sans base, et « 4,5 millions
pour tout » ne doit JAMAIS devenir « 4,5 millions par unité ». Ce module est l'unique passage
message-producteur -> prix certifiable d'un bid : il extrait le montant, la BASE (par unité /
conditionnement / lot entier), sa PROVENANCE et, si besoin, le conditionnement — puis le domaine
(`CommercialPricingSnapshot`) valide. Le LLM peut SUGGÉRER (`llm_hints`, jamais autoritaire) ; le texte
et le contexte de question décident ; au moindre doute : `NEEDS_BASIS` -> on demande, on n'écrit pas.

Réutilise, sans les redupliquer, les primitives du flux de vente B1 (`commercial_offer_flow` :
normalisation, indice de total, motif « montant + base + unité », `Provenance`, `PriceBasis`).

Module pur : aucune E/S.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional

from ladini.core.formatting import fmt_num
from ladini.domain.commercial_offer import PriceBasis, Provenance, unit_display
from ladini.domain.commercial_offer_flow import (
    UNIT_AFTER_PRICE_RE,
    has_total_cue,
    normalize_text,
)
from ladini.domain.commercial_pricing_snapshot import (
    CommercialPricingSnapshot,
    PricingSnapshotError,
    build_bid_pricing_snapshot,
    to_decimal,
)
from ladini.domain.pricing_tiers import unit_family
from ladini.domain.quantity_unit import normalize_unit


class BidPriceStatus(str, Enum):
    RESOLVED = "RESOLVED"  # montant + base + provenance exécutable : certifiable
    NEEDS_BASIS = "NEEDS_BASIS"  # montant sans base fiable -> DEMANDER, n'écrire rien
    NEEDS_PACKAGE_SIZE = "NEEDS_PACKAGE_SIZE"  # « la caisse » sans contenu -> DEMANDER
    INVALID = "INVALID"  # incompatible avec l'enchère -> refuser (ex. 500/LITRE pour du KG)
    NO_PRICE = "NO_PRICE"  # aucun montant lisible


#: mots de conditionnement acceptés pour un prix de bid (les appels d'offres portent
#: `preferred_packaging` : le prix au conditionnement est donc supporté, à contenu EXPLICITE).
BID_PACKAGE_WORDS = (
    "caisse", "caisses", "sac", "sacs", "sachet", "sachets", "bidon", "bidons", "carton", "cartons",
    "panier", "paniers", "casier", "casiers", "seau", "seaux",
)

#: contenu d'un conditionnement -> (facteur, unité canonique de base). Local : jamais ajouté au registre global.
_CONTENT_UNITS: Dict[str, tuple] = {
    "kg": (Decimal(1), "KG"), "kilo": (Decimal(1), "KG"), "kilos": (Decimal(1), "KG"),
    "g": (Decimal("0.001"), "KG"), "gramme": (Decimal("0.001"), "KG"), "grammes": (Decimal("0.001"), "KG"),
    "t": (Decimal(1000), "KG"), "tonne": (Decimal(1000), "KG"), "tonnes": (Decimal(1000), "KG"),
    "l": (Decimal(1), "LITRE"), "litre": (Decimal(1), "LITRE"), "litres": (Decimal(1), "LITRE"),
    "ml": (Decimal("0.001"), "LITRE"), "cl": (Decimal("0.01"), "LITRE"), "dl": (Decimal("0.1"), "LITRE"),
}

_AMOUNT_RE = re.compile(
    r"(?<![\d.,])(?P<num>\d{1,3}(?: \d{3})+(?:,\d+)?|\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?)"
    r"(?:\s*(?P<mult>millions?|mille|k)\b)?"
)
_CURRENCY_AFTER_RE = re.compile(r"^\s*(?:fcfa|cfa|francs?|f)\b")
_WORD_AFTER_RE = re.compile(r"^\s*([a-z]+)")
_PACKAGE_EXPR_RE = re.compile(
    r"^\s*(?:fcfa|cfa|francs?|f)?\s*(?:/|par\b|chaque\b|le\b|la\b|l\b|au\b|pour un\b|pour une\b)\s*"
    r"(?:un\s+|une\s+)?(?P<pkg>" + "|".join(sorted(BID_PACKAGE_WORDS, key=len, reverse=True)) + r")\b"
    r"\s*(?:de|d|=|fait|contient)?\s*"
    r"(?:(?P<cnum>\d+(?:[.,]\d+)?)\s*(?P<cunit>" + "|".join(sorted(_CONTENT_UNITS, key=len, reverse=True)) + r")\b)?"
)


@dataclass(frozen=True)
class _Amount:
    value: Decimal
    start: int
    end: int


def _decimal_of(num: str, mult: Optional[str]) -> Optional[Decimal]:
    try:
        if " " in num:
            cleaned = num.replace(" ", "").replace(",", ".")
        elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", num):
            cleaned = num.replace(".", "").replace(",", ".")
        else:
            cleaned = num.replace(",", ".")
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if mult:
        value *= Decimal(1_000_000) if mult.startswith("million") else Decimal(1000)
    return value


def find_amounts(text: Any) -> List[_Amount]:
    """Montants du message (« 450 000 », « 4,5 millions », « 450k »), position comprise."""
    ntext = normalize_text(text)
    out: List[_Amount] = []
    for m in _AMOUNT_RE.finditer(ntext):
        value = _decimal_of(m.group("num"), m.group("mult"))
        if value is not None and value > 0:
            out.append(_Amount(value, m.start(), m.end()))
    return out


def _fmt(value: Decimal) -> str:
    return fmt_num(float(value))


@dataclass(frozen=True)
class BidPriceContext:
    """Ce que la question POSÉE fixe déjà (jamais ce que l'agent suppose).

    - `PER_AUCTION_UNIT` : « Quel prix proposez-vous *par tonne* ? » -> un montant nu est « par tonne » ;
    - `KEEP_BASIS`       : correction d'un prix dont le récap affichait sa base -> un montant nu la garde ;
    - `NONE`             : aucune base fixée -> un montant nu est AMBIGU (on demande)."""

    kind: str = "NONE"
    basis: Optional[PriceBasis] = None
    price_unit: Optional[str] = None
    package_type: Optional[str] = None
    package_content_amount: Optional[Decimal] = None
    package_content_unit: Optional[str] = None

    @classmethod
    def per_auction_unit(cls, auction_unit: str) -> "BidPriceContext":
        return cls("PER_AUCTION_UNIT", PriceBasis.PER_BASE_UNIT, str(auction_unit).upper())

    @classmethod
    def keep(cls, snapshot: CommercialPricingSnapshot) -> "BidPriceContext":
        return cls(
            "KEEP_BASIS", snapshot.price_basis, snapshot.price_unit, snapshot.package_type,
            snapshot.package_content_amount, snapshot.package_content_unit,
        )


@dataclass(frozen=True)
class BidPriceParse:
    status: BidPriceStatus
    amount: Optional[Decimal] = None
    basis: Optional[PriceBasis] = None
    price_unit: Optional[str] = None
    package_type: Optional[str] = None
    package_content_amount: Optional[Decimal] = None
    package_content_unit: Optional[str] = None
    source: Provenance = Provenance.UNKNOWN
    #: question à poser (NEEDS_*) ou motif du refus (INVALID)
    message: Optional[str] = None
    #: base suggérée par le LLM — journalisée, JAMAIS autoritaire
    llm_hint_basis: Optional[str] = None

    @property
    def is_resolved(self) -> bool:
        return self.status == BidPriceStatus.RESOLVED

    def to_state(self) -> Dict[str, Any]:
        """Forme JSON (working_memory) — montants en chaînes."""
        def s(v: Optional[Decimal]) -> Optional[str]:
            return None if v is None else format(v.normalize(), "f")

        return {
            "status": self.status.value,
            "amount": s(self.amount),
            "basis": self.basis.value if self.basis else None,
            "price_unit": self.price_unit,
            "package_type": self.package_type,
            "package_content_amount": s(self.package_content_amount),
            "package_content_unit": self.package_content_unit,
            "source": self.source.value,
        }

    @classmethod
    def from_state(cls, data: Any) -> Optional["BidPriceParse"]:
        if not isinstance(data, Mapping) or not data.get("amount"):
            return None
        try:
            return cls(
                status=BidPriceStatus(str(data.get("status"))),
                amount=to_decimal(data.get("amount")),
                basis=PriceBasis(str(data["basis"])) if data.get("basis") else None,
                price_unit=data.get("price_unit"),
                package_type=data.get("package_type"),
                package_content_amount=(
                    to_decimal(data["package_content_amount"]) if data.get("package_content_amount") else None
                ),
                package_content_unit=data.get("package_content_unit"),
                source=Provenance(str(data.get("source") or "UNKNOWN")),
            )
        except (ValueError, PricingSnapshotError):
            return None

    def snapshot(self, auction_quantity: Any, auction_unit: str) -> CommercialPricingSnapshot:
        """Snapshot certifié — refuse tout ce qui n'est pas RÉSOLU, de provenance exécutable, et compatible."""
        if not self.is_resolved or self.amount is None or self.basis is None:
            raise PricingSnapshotError("prix de bid non résolu: la base doit être connue avant toute écriture")
        if not self.source.is_execution_safe:
            raise PricingSnapshotError(f"base du prix non certifiée ({self.source.value})")
        snap = build_bid_pricing_snapshot(
            amount=self.amount,
            basis=self.basis,
            price_unit=self.price_unit,
            auction_quantity=auction_quantity,
            auction_unit=auction_unit,
            package_type=self.package_type,
            package_content_amount=self.package_content_amount,
            package_content_unit=self.package_content_unit,
            source=self.source.value,
        )
        snap.total_for(auction_quantity, auction_unit)  # base <-> unité de l'enchère, divisibilité d'un conditionnement
        return snap


# ---------------------------------------------------------------------------
# Textes de question
# ---------------------------------------------------------------------------


def price_per_unit_question(auction_unit: str) -> str:
    label = unit_display(auction_unit)
    return (
        f"Quel prix proposez-vous *par {label}* ? (en FCFA)\n"
        f"_Pour un prix pour tout le lot, dites par exemple « 4 500 000 pour tout »._"
    )


def basis_question(amount: Decimal, auction_unit: str, auction_quantity: Any) -> str:
    label = unit_display(auction_unit)
    return (
        f"{_fmt(amount)} FCFA, c'est *par {label}* ou *pour l'ensemble des "
        f"{fmt_num(auction_quantity)} {unit_display(auction_unit, float(to_decimal(auction_quantity)))}* ?"
    )


def package_size_question(package_type: str) -> str:
    return f"Quelle quantité contient une *{package_type.lower()}* ? Par exemple 25 kg."


# ---------------------------------------------------------------------------
# Analyse
# ---------------------------------------------------------------------------


def _pick_price_amount(ntext: str, amounts: List[_Amount]) -> Optional[_Amount]:
    """Le montant qui est LE prix : celui porteur d'une devise ou d'une base (« 450000 la tonne »),
    sinon l'unique montant qui n'est pas une quantité (« 10 tonnes »). Plusieurs -> ambigu (None)."""
    marked: List[_Amount] = []
    plain: List[_Amount] = []
    for a in amounts:
        rest = ntext[a.end:]
        if _CURRENCY_AFTER_RE.match(rest) or UNIT_AFTER_PRICE_RE.match(rest) or _PACKAGE_EXPR_RE.match(rest):
            marked.append(a)
            continue
        word = _WORD_AFTER_RE.match(rest)
        if word and normalize_unit(word.group(1)) is not None:
            continue  # « 10 tonnes » : une QUANTITÉ, pas un prix
        plain.append(a)
    if len(marked) == 1:
        return marked[0]
    if not marked and len(plain) == 1:
        return plain[0]
    return None


def _content_of(match: "re.Match[str]") -> Optional[tuple]:
    if not match.group("cnum") or not match.group("cunit"):
        return None
    factor, unit = _CONTENT_UNITS[match.group("cunit")]
    try:
        amount = Decimal(match.group("cnum").replace(",", ".")) * factor
    except InvalidOperation:
        return None
    return (amount, unit) if amount > 0 else None


def parse_bid_price(
    text: Any,
    *,
    auction_unit: str,
    auction_quantity: Any,
    context: Optional[BidPriceContext] = None,
    llm_hints: Optional[Mapping[str, Any]] = None,
) -> BidPriceParse:
    """Message du producteur -> prix de bid. Ne renvoie `RESOLVED` que si la base est EXPLICITE dans le
    texte ou FIXÉE par le contexte de question ; sinon `NEEDS_BASIS` (jamais de devinette)."""
    ntext = normalize_text(text)
    hint = str((llm_hints or {}).get("price_basis") or "").upper() or None
    amounts = find_amounts(ntext)
    picked = _pick_price_amount(ntext, amounts)
    if picked is None:
        if not amounts:
            return BidPriceParse(BidPriceStatus.NO_PRICE, llm_hint_basis=hint)
        first = amounts[0]
        return BidPriceParse(
            BidPriceStatus.NEEDS_BASIS, amount=first.value if len(amounts) == 1 else None,
            message="Je n'ai pas bien compris votre prix. Quel montant proposez-vous, et est-ce par unité ou pour tout le lot ?",
            llm_hint_basis=hint,
        )

    amount = picked.value
    rest = ntext[picked.end:]
    unit_norm = str(auction_unit).upper()
    total_cue = has_total_cue(ntext)
    unit_match = UNIT_AFTER_PRICE_RE.match(rest)
    pkg_match = _PACKAGE_EXPR_RE.match(rest)
    explicit_word = None
    if unit_match:
        explicit_word = unit_match.group(1)

    package_word = pkg_match.group("pkg") if pkg_match else None
    explicit_unit = None
    if explicit_word and explicit_word not in BID_PACKAGE_WORDS:
        explicit_unit = normalize_unit(explicit_word)

    # -- contradictions : « 450000 la tonne pour tout » -> on demande
    if total_cue and (explicit_unit or package_word):
        return BidPriceParse(
            BidPriceStatus.NEEDS_BASIS, amount=amount, llm_hint_basis=hint,
            message=basis_question(amount, unit_norm, auction_quantity),
        )

    # -- 1. lot entier
    if total_cue:
        return BidPriceParse(
            BidPriceStatus.RESOLVED, amount=amount, basis=PriceBasis.TOTAL_LOT, source=Provenance.USER_EXPLICIT,
            llm_hint_basis=hint,
        )

    # -- 2. conditionnement (« 12000 la caisse de 25 kg »)
    if package_word:
        content = _content_of(pkg_match) if pkg_match else None
        ptype = package_word.upper()
        if content is None:
            return BidPriceParse(
                BidPriceStatus.NEEDS_PACKAGE_SIZE, amount=amount, basis=PriceBasis.PER_PACKAGE, package_type=ptype,
                source=Provenance.USER_EXPLICIT, message=package_size_question(ptype), llm_hint_basis=hint,
            )
        c_amount, c_unit = content
        if unit_family(c_unit) != unit_family(unit_norm):
            return BidPriceParse(
                BidPriceStatus.INVALID, amount=amount, basis=PriceBasis.PER_PACKAGE, package_type=ptype,
                message=(
                    f"Une {ptype.lower()} de {fmt_num(float(c_amount))} {unit_display(c_unit)} ne se compare pas à une "
                    f"enchère en {unit_display(unit_norm)}."
                ),
                llm_hint_basis=hint,
            )
        return BidPriceParse(
            BidPriceStatus.RESOLVED, amount=amount, basis=PriceBasis.PER_PACKAGE, package_type=ptype,
            package_content_amount=c_amount, package_content_unit=c_unit, source=Provenance.USER_EXPLICIT,
            llm_hint_basis=hint,
        )

    # -- 3. unité explicite (« 450000 la tonne »)
    if explicit_unit:
        if unit_family(explicit_unit) != unit_family(unit_norm):
            return BidPriceParse(
                BidPriceStatus.INVALID, amount=amount, basis=PriceBasis.PER_BASE_UNIT, price_unit=explicit_unit,
                message=(
                    f"Un prix par {unit_display(explicit_unit)} ne correspond pas à une enchère en "
                    f"{unit_display(unit_norm)}. Précisez votre prix par {unit_display(unit_norm)} ou pour tout le lot."
                ),
                llm_hint_basis=hint,
            )
        return BidPriceParse(
            BidPriceStatus.RESOLVED, amount=amount, basis=PriceBasis.PER_BASE_UNIT, price_unit=explicit_unit,
            source=Provenance.USER_EXPLICIT, llm_hint_basis=hint,
        )

    # -- 4. montant nu : seule la question posée peut fixer la base
    if context is not None and context.kind in ("PER_AUCTION_UNIT", "KEEP_BASIS") and context.basis is not None:
        return BidPriceParse(
            BidPriceStatus.RESOLVED, amount=amount, basis=context.basis, price_unit=context.price_unit,
            package_type=context.package_type, package_content_amount=context.package_content_amount,
            package_content_unit=context.package_content_unit, source=Provenance.QUESTION_CONTEXT_EXPLICIT,
            llm_hint_basis=hint,
        )
    return BidPriceParse(
        BidPriceStatus.NEEDS_BASIS, amount=amount, llm_hint_basis=hint,
        message=basis_question(amount, unit_norm, auction_quantity),
    )


_PRICE_VOCAB = frozenset(
    "fcfa cfa f franc francs la le les l par pour tout ensemble au total en de d du des un une tonne tonnes t kg kilo "
    "kilos litre litres sac sacs sachet sachets caisse caisses bidon bidons carton cartons panier paniers lot "
    "millions million mille k je propose prix offre mets mettons c est cest a chaque forfait globalement entier "
    "fait contient finalement plutot alors enfin mettez changez changer corrige corriger remplace nouveau nouvelle".split()
)


def is_price_reply(text: Any) -> bool:
    """Le message ne fait QUE dire un prix (« 450000 », « 450 000 la tonne », « 4,5 millions pour tout ») :
    un montant identifiable et AUCUN mot étranger au vocabulaire du prix. Sert de voie déterministe (sans LLM)
    quand une question de prix vient d'être posée ; tout message plus riche retombe sur le classifieur."""
    ntext = normalize_text(text)
    if _pick_price_amount(ntext, find_amounts(ntext)) is None:
        return False
    words = re.findall(r"[a-z]+", _AMOUNT_RE.sub(" ", ntext))
    return all(w in _PRICE_VOCAB for w in words)


def resolve_basis_reply(
    text: Any, *, amount: Decimal, auction_unit: str, auction_quantity: Any,
) -> BidPriceParse:
    """Réponse à « X FCFA, c'est par tonne ou pour l'ensemble ? » : le montant est déjà établi.
    Une réponse qui porte SON PROPRE nombre est un nouveau prix, relu sans contexte."""
    ntext = normalize_text(text)
    if find_amounts(ntext):
        return parse_bid_price(text, auction_unit=auction_unit, auction_quantity=auction_quantity)
    synthetic = f"{format(amount.normalize(), 'f')} {ntext}"
    return parse_bid_price(synthetic, auction_unit=auction_unit, auction_quantity=auction_quantity)


def resolve_package_reply(pending: BidPriceParse, text: Any, *, auction_unit: str) -> BidPriceParse:
    """Réponse à « Quelle quantité contient une caisse ? » (ex. « 25 kg »)."""
    ntext = normalize_text(text)
    m = re.fullmatch(
        r"\s*(?:(?:la|une|un|c est|cest|environ)\s+)*(?P<cnum>\d+(?:[.,]\d+)?)\s*(?P<cunit>"
        + "|".join(sorted(_CONTENT_UNITS, key=len, reverse=True)) + r")\s*",
        ntext,
    )
    if not m or pending.amount is None or not pending.package_type:
        return BidPriceParse(
            BidPriceStatus.NEEDS_PACKAGE_SIZE, amount=pending.amount, basis=PriceBasis.PER_PACKAGE,
            package_type=pending.package_type, source=pending.source,
            message=package_size_question(pending.package_type or "caisse"),
        )
    factor, c_unit = _CONTENT_UNITS[m.group("cunit")]
    content = Decimal(m.group("cnum").replace(",", ".")) * factor
    if content <= 0 or unit_family(c_unit) != unit_family(auction_unit):
        return BidPriceParse(
            BidPriceStatus.INVALID, amount=pending.amount, basis=PriceBasis.PER_PACKAGE, package_type=pending.package_type,
            message=f"Cette quantité ne correspond pas à une enchère en {unit_display(auction_unit)}.",
        )
    return BidPriceParse(
        BidPriceStatus.RESOLVED, amount=pending.amount, basis=PriceBasis.PER_PACKAGE, package_type=pending.package_type,
        package_content_amount=content, package_content_unit=c_unit, source=Provenance.USER_EXPLICIT,
    )


# ---------------------------------------------------------------------------
# Affichage & comparaison (un bid affiche SA base, jamais celle de l'enchère)
# ---------------------------------------------------------------------------


def render_pricing_label(snapshot: CommercialPricingSnapshot) -> str:
    """« 450 000 FCFA par tonne » / « 4 500 000 FCFA pour l'ensemble » / « 12 000 FCFA par caisse de 25 kg »."""
    money = f"{fmt_num(float(snapshot.commercial_price_amount))} FCFA"
    if snapshot.price_basis == PriceBasis.TOTAL_LOT:
        return f"{money} pour l'ensemble"
    if snapshot.price_basis == PriceBasis.PER_PACKAGE and snapshot.package_content_amount is not None:
        content = float(snapshot.package_content_amount)
        return (
            f"{money} par {str(snapshot.package_type or 'conditionnement').lower()} de "
            f"{fmt_num(content)} {unit_display(snapshot.package_content_unit, content)}"
        )
    return f"{money} par {unit_display(snapshot.price_unit)}"


def comparable_total(snapshot: CommercialPricingSnapshot, auction_quantity: Any, auction_unit: str) -> Optional[Decimal]:
    """Total comparable d'un bid pour la quantité de l'enchère, ou `None` (jamais deviné)."""
    try:
        return snapshot.total_for(auction_quantity, auction_unit)
    except PricingSnapshotError:
        return None


__all__ = [
    "BidPriceStatus",
    "BidPriceContext",
    "BidPriceParse",
    "BID_PACKAGE_WORDS",
    "find_amounts",
    "parse_bid_price",
    "is_price_reply",
    "resolve_basis_reply",
    "resolve_package_reply",
    "price_per_unit_question",
    "basis_question",
    "package_size_question",
    "render_pricing_label",
    "comparable_total",
]
