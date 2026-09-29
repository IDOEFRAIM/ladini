"""Contrat de PERSISTANCE de la sémantique commerciale (Phase B2a).

Règle centrale : *la sémantique d'une transaction doit survivre à la persistance*. Un `OrderItem`
ou un `Bid` historique ne relit jamais le `Product` courant, la taxonomie courante, un palier
mutable ni un ancien état de conversation pour savoir « 500 FCFA — par quoi ? ».

`CommercialPricingSnapshot` est la PROJECTION PERSISTÉE du même contrat que `CommercialOffer`
(`domain/commercial_offer.py`, qui reste canonique) — pas un second modèle. Deux formes, UN
seul objet et UN seul jeu de règles :

- forme JSON (`to_dict`/`from_dict`) pour les lignes MUTABLES (`products.commercial_pricing`,
  `market_offers.pricing_snapshot`, `orders.award_pricing_snapshot`) ;
- forme colonnes typées (`to_columns`/`from_columns`) pour les lignes HISTORIQUES
  (`order_items.*`, `bids.*`) : contraignables en base, jamais un blob.

## Précision

Aucun `float` ne porte un montant ici : `Decimal`, sérialisé en CHAÎNE dans le JSON (un JSON
number repasserait par un float). Le montant commercial (celui que l'utilisateur a dit) est
EXACT et fait autorité ; le prix normalisé (par unité de base) est un DÉRIVÉ arrondi
`ROUND_HALF_UP` à 4 décimales — 5 000 000 / 200 000 kg = 25, 1 000 000 / 3 kg = 333333.3333.
La colonne héritée `products.price` (numeric(12,2)) n'est qu'une projection à 2 décimales du
dérivé ; `assert_legacy_projection_matches` échoue AVANT l'écriture si elles divergent.

## Fiabilité à la lecture

Une ligne SANS snapshot (antérieure à B2a) n'est jamais « comprise » : `legacy_pricing_view`
renvoie `UNKNOWN_BASIS`/`LEGACY_PARTIAL` — on n'invente pas que l'ancien bid était « par unité
de l'enchère ».
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional

from ladini.core.formatting import fmt_num
from ladini.domain.commercial_offer import (
    CommercialOffer,
    PriceBasis,
    base_unit_for,
    unit_display,
)
from ladini.domain.pricing_tiers import unit_factor as _unit_factor
from ladini.domain.pricing_tiers import unit_family as _unit_family

SNAPSHOT_SCHEMA_VERSION = 1
DEFAULT_CURRENCY = "XOF"  # convention existante : `orders.currency`, `payments.currency`
MONEY_QUANT = Decimal("0.01")
NORMALIZED_QUANT = Decimal("0.0001")
ROUNDING = ROUND_HALF_UP
#: tolérance de la projection legacy (numeric(12,2)) : un centime
LEGACY_TOLERANCE = Decimal("0.01")

_CURRENCY_ALIASES = {"FCFA": "XOF", "CFA": "XOF", "F CFA": "XOF", "XOF": "XOF"}
CERTIFIED_BASES = frozenset(b.value for b in PriceBasis)
LEGACY_UNSPECIFIED = "LEGACY_UNSPECIFIED"

_ZERO = Decimal(0)


class PricingSnapshotError(ValueError):
    """Snapshot invalide : jamais persisté, jamais « corrigé » silencieusement."""


class PricingReliability(str, Enum):
    CERTIFIED = "CERTIFIED"  # snapshot versionné, écrit par le contrat B2a
    LEGACY_PARTIAL = "LEGACY_PARTIAL"  # montant connu, sens partiel (ligne antérieure à B2a)
    UNKNOWN_BASIS = "UNKNOWN_BASIS"  # montant connu, base du prix INCONNUE


class PackageSaleMode(str, Enum):
    """Comment un produit conditionné peut être acheté (Étape 19).

    - `PACKAGE_ONLY` (défaut, contrat métier actuel « pas de tetris de conditionnements » —
      `pricing_tiers.classify_pack_count_unit`) : l'acheteur donne un NOMBRE de paquets ;
      « 2 L » sur un sachet de 0,5 L est REFUSÉ (jamais converti en silence en 4 sachets).
    - `BASE_UNIT_ALLOWED` : une quantité en unité du contenu est acceptée SI la division est
      exacte (2 L = 4 sachets de 0,5 L ; 1,2 L = 2,4 sachets => refus).
    """

    PACKAGE_ONLY = "PACKAGE_ONLY"
    BASE_UNIT_ALLOWED = "BASE_UNIT_ALLOWED"


# ---------------------------------------------------------------------------
# Décimaux
# ---------------------------------------------------------------------------


def to_decimal(value: Any, *, field: str = "value") -> Decimal:
    """`Decimal` exact depuis int / str / Decimal / float. Un float passe par son `repr` le plus
    court (0.5 -> « 0.5 », jamais 0.5000000000000000277) : c'est le nombre que l'utilisateur a dit."""
    if isinstance(value, bool) or value is None:
        raise PricingSnapshotError(f"{field}: nombre attendu, reçu {value!r}")
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            result = Decimal(str(value).strip().replace(",", "."))
        except (InvalidOperation, ValueError) as exc:
            raise PricingSnapshotError(f"{field}: nombre invalide {value!r}") from exc
    if not result.is_finite():
        raise PricingSnapshotError(f"{field}: nombre non fini {value!r}")
    return result


def _opt_decimal(value: Any, field: str) -> Optional[Decimal]:
    return None if value in (None, "") else to_decimal(value, field=field)


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANT, rounding=ROUNDING)


def quantize_normalized(value: Decimal) -> Decimal:
    return value.quantize(NORMALIZED_QUANT, rounding=ROUNDING)


def normalize_currency(value: Optional[str]) -> str:
    raw = str(value or "").strip().upper()
    return _CURRENCY_ALIASES.get(raw, raw) if raw else DEFAULT_CURRENCY


def _factor(unit: str) -> Decimal:
    return Decimal(str(_unit_factor(unit)))


def _canon_unit(unit: Optional[str]) -> Optional[str]:
    cleaned = str(unit or "").strip().upper()
    return cleaned or None


def _canon_word(value: Optional[str]) -> Optional[str]:
    cleaned = str(value or "").strip().upper()
    return cleaned or None


# ---------------------------------------------------------------------------
# Le snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommercialPricingSnapshot:
    """Sémantique commerciale figée. Immuable (`frozen`) — une modification = un NOUVEAU snapshot."""

    commercial_price_amount: Decimal
    price_basis: PriceBasis
    price_unit: Optional[str] = None  # PER_BASE_UNIT : « TONNE » dans « 450000 / TONNE »
    package_type: Optional[str] = None  # PER_PACKAGE : « SACHET »
    package_content_amount: Optional[Decimal] = None
    package_content_unit: Optional[str] = None
    commercial_quantity_amount: Optional[Decimal] = None
    commercial_quantity_unit: Optional[str] = None
    inventory_quantity_amount: Optional[Decimal] = None
    inventory_quantity_unit: Optional[str] = None
    normalized_unit_price: Optional[Decimal] = None  # DÉRIVÉ (prix par unité de base)
    normalized_unit: Optional[str] = None
    currency: str = DEFAULT_CURRENCY
    #: provenance de la BASE du prix (audit) : USER_EXPLICIT / QUESTION_CONTEXT_EXPLICIT / …
    price_source: Optional[str] = None
    schema_version: int = SNAPSHOT_SCHEMA_VERSION

    # ------------------------------------------------------------ validation
    def issues(self) -> List[str]:
        """Incohérences du snapshot (liste vide = valide). Mêmes règles que les CHECK en base."""
        out: List[str] = []
        if self.schema_version != SNAPSHOT_SCHEMA_VERSION:
            out.append(f"schema_version {self.schema_version} non supportée")
        if not self.currency:
            out.append("currency manquante")
        if self.commercial_price_amount is None or self.commercial_price_amount <= 0:
            out.append("commercial_price_amount doit être > 0")
        if not isinstance(self.price_basis, PriceBasis):
            out.append("price_basis inconnue")
            return out
        if self.price_basis == PriceBasis.PER_PACKAGE:
            if not self.package_type:
                out.append("PER_PACKAGE exige package_type")
            if self.package_content_amount is None or self.package_content_amount <= 0:
                out.append("PER_PACKAGE exige package_content_amount > 0")
            if not self.package_content_unit:
                out.append("PER_PACKAGE exige package_content_unit")
        else:
            if self.package_type or self.package_content_amount is not None or self.package_content_unit:
                out.append(f"{self.price_basis.value} n'admet aucun conditionnement")
        if self.price_basis == PriceBasis.PER_BASE_UNIT and not self.price_unit:
            out.append("PER_BASE_UNIT exige price_unit")
        if self.price_basis != PriceBasis.PER_BASE_UNIT and self.price_unit:
            out.append(f"{self.price_basis.value} n'admet pas de price_unit")
        for name in ("commercial_quantity", "inventory_quantity"):
            amount, unit = getattr(self, f"{name}_amount"), getattr(self, f"{name}_unit")
            if (amount is None) != (unit is None):
                out.append(f"{name}: montant et unité vont ensemble")
            elif amount is not None and amount <= 0:
                out.append(f"{name}_amount doit être > 0")
        if (self.normalized_unit_price is None) != (self.normalized_unit is None):
            out.append("normalized_unit_price et normalized_unit vont ensemble")
        if self.normalized_unit_price is not None and self.normalized_unit_price <= 0:
            out.append("normalized_unit_price doit être > 0")
        # compatibilité de famille
        if self.price_basis == PriceBasis.PER_BASE_UNIT and self.price_unit:
            ref = self.inventory_quantity_unit or self.commercial_quantity_unit
            if ref and _unit_family(self.price_unit) != _unit_family(ref):
                out.append(f"price_unit {self.price_unit} incompatible avec l'unité {ref}")
        if self.price_basis == PriceBasis.PER_PACKAGE and self.package_content_unit:
            ref = self.inventory_quantity_unit
            if ref and _unit_family(self.package_content_unit) != _unit_family(ref):
                out.append(
                    f"package_content_unit {self.package_content_unit} incompatible avec {ref}"
                )
        return out

    def assert_valid(self) -> "CommercialPricingSnapshot":
        problems = self.issues()
        if problems:
            raise PricingSnapshotError("; ".join(problems))
        return self

    # ------------------------------------------------------------ dérivés
    def compute_normalized(self) -> Optional[Decimal]:
        """Prix par unité de BASE (KG / LITRE / l'unité elle-même), en Decimal — dérivé, jamais
        l'autorité. `None` si non calculable (TOTAL_LOT sans quantité d'inventaire)."""
        amount = self.commercial_price_amount
        if self.price_basis == PriceBasis.PER_BASE_UNIT and self.price_unit:
            return quantize_normalized(amount / _factor(self.price_unit))
        if self.price_basis == PriceBasis.PER_PACKAGE:
            if not self.package_content_amount or not self.package_content_unit:
                return None
            return quantize_normalized(
                amount / (self.package_content_amount * _factor(self.package_content_unit))
            )
        if self.price_basis == PriceBasis.TOTAL_LOT:
            if not self.inventory_quantity_amount or not self.inventory_quantity_unit:
                return None
            return quantize_normalized(
                amount / (self.inventory_quantity_amount * _factor(self.inventory_quantity_unit))
            )
        return None

    def with_normalized(self) -> "CommercialPricingSnapshot":
        """Recalcule `normalized_*` (source de vérité de la normalisation : CE calcul Decimal)."""
        value = self.compute_normalized()
        ref_unit = (
            self.inventory_quantity_unit
            or self.commercial_quantity_unit
            or self.price_unit
            or self.package_content_unit
        )
        unit = (base_unit_for(ref_unit) or _canon_unit(ref_unit)) if ref_unit else None
        return replace(self, normalized_unit_price=value, normalized_unit=unit if value is not None else None)

    def total_for(self, quantity: Any, unit: Optional[str]) -> Decimal:
        """Total (montant arrondi 0,01) pour `quantity` `unit` de ce produit.

        PER_BASE_UNIT : montant × quantité exprimée dans `price_unit` ; TOTAL_LOT : le montant
        (le lot entier, quel que soit l'arrondi de la normalisation) ; PER_PACKAGE : nombre de
        conditionnements EXACT × montant (division inexacte => erreur, jamais un arrondi)."""
        qty = to_decimal(quantity, field="quantity")
        if qty <= 0:
            raise PricingSnapshotError("quantity doit être > 0")
        if self.price_basis == PriceBasis.TOTAL_LOT:
            return quantize_money(self.commercial_price_amount)
        if self.price_basis == PriceBasis.PER_BASE_UNIT:
            qty_unit = _canon_unit(unit) or self.price_unit
            if _unit_family(qty_unit or "") != _unit_family(self.price_unit or ""):
                raise PricingSnapshotError(f"unité {qty_unit} incompatible avec {self.price_unit}")
            in_price_unit = qty * _factor(qty_unit or "") / _factor(self.price_unit or "")
            return quantize_money(self.commercial_price_amount * in_price_unit)
        # PER_PACKAGE
        content = (self.package_content_amount or _ZERO) * _factor(self.package_content_unit or "")
        if content <= 0:
            raise PricingSnapshotError("contenu du conditionnement inconnu")
        base_qty = qty * _factor(_canon_unit(unit) or self.package_content_unit or "")
        count = base_qty / content
        if count != count.to_integral_value():
            raise PricingSnapshotError(
                f"{qty} {unit} n'est pas un nombre entier de {self.package_type}"
            )
        return quantize_money(self.commercial_price_amount * count)

    # ------------------------------------------------------------ JSON (lignes mutables)
    def to_dict(self) -> Dict[str, Any]:
        def s(v: Optional[Decimal]) -> Optional[str]:
            return None if v is None else format(v.normalize(), "f")

        return {
            "schema_version": self.schema_version,
            "currency": self.currency,
            "commercial_price_amount": s(self.commercial_price_amount),
            "price_basis": self.price_basis.value,
            "price_unit": self.price_unit,
            "package_type": self.package_type,
            "package_content_amount": s(self.package_content_amount),
            "package_content_unit": self.package_content_unit,
            "commercial_quantity_amount": s(self.commercial_quantity_amount),
            "commercial_quantity_unit": self.commercial_quantity_unit,
            "inventory_quantity_amount": s(self.inventory_quantity_amount),
            "inventory_quantity_unit": self.inventory_quantity_unit,
            "normalized_unit_price": s(self.normalized_unit_price),
            "normalized_unit": self.normalized_unit,
            "price_source": self.price_source,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["CommercialPricingSnapshot"]:
        """`None` si `data` n'est pas un snapshot de version supportée (jamais une exception pour
        une ligne legacy) ; `PricingSnapshotError` si c'est un snapshot MAL FORMÉ."""
        if not isinstance(data, Mapping) or data.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            return None
        try:
            basis = PriceBasis(str(data.get("price_basis")))
        except ValueError as exc:
            raise PricingSnapshotError(f"price_basis inconnue: {data.get('price_basis')!r}") from exc
        return cls(
            commercial_price_amount=to_decimal(data.get("commercial_price_amount"), field="commercial_price_amount"),
            price_basis=basis,
            price_unit=_canon_unit(data.get("price_unit")),
            package_type=_canon_word(data.get("package_type")),
            package_content_amount=_opt_decimal(data.get("package_content_amount"), "package_content_amount"),
            package_content_unit=_canon_unit(data.get("package_content_unit")),
            commercial_quantity_amount=_opt_decimal(data.get("commercial_quantity_amount"), "commercial_quantity_amount"),
            commercial_quantity_unit=_canon_unit(data.get("commercial_quantity_unit")),
            inventory_quantity_amount=_opt_decimal(data.get("inventory_quantity_amount"), "inventory_quantity_amount"),
            inventory_quantity_unit=_canon_unit(data.get("inventory_quantity_unit")),
            normalized_unit_price=_opt_decimal(data.get("normalized_unit_price"), "normalized_unit_price"),
            normalized_unit=_canon_unit(data.get("normalized_unit")),
            currency=normalize_currency(data.get("currency")),
            price_source=data.get("price_source"),
            schema_version=SNAPSHOT_SCHEMA_VERSION,
        ).assert_valid()

    # ------------------------------------------------------------ colonnes (lignes historiques)
    def to_order_item_columns(self) -> Dict[str, Any]:
        """Colonnes `marketplace.order_items` (les montants de QUANTITÉ restent `quantity` /
        `base_unit_quantity`, colonnes historiques ; `quantity_unit` dit dans quoi `quantity` est
        exprimée : « SACHET » pour 4 sachets, « KG » pour 100 kg)."""
        return {
            "quantity_unit": self.commercial_quantity_unit,
            "commercial_price_amount": self.commercial_price_amount,
            "price_basis": self.price_basis.value,
            "price_unit": self.price_unit,
            "package_type": self.package_type,
            "package_content_amount": self.package_content_amount,
            "package_content_unit": self.package_content_unit,
            "normalized_unit_price": self.normalized_unit_price,
            "normalized_unit": self.normalized_unit,
            "currency": self.currency,
            "pricing_snapshot_version": self.schema_version,
        }

    def to_bid_columns(self) -> Dict[str, Any]:
        """Colonnes `marketplace.bids` (`offered_price` reste LE montant commercial)."""
        return {
            "offered_price": self.commercial_price_amount,
            "offered_price_basis": self.price_basis.value,
            "offered_price_unit": self.price_unit,
            "offered_price_currency": self.currency,
            "package_type": self.package_type,
            "package_content_amount": self.package_content_amount,
            "package_content_unit": self.package_content_unit,
            "normalized_unit_price": self.normalized_unit_price,
            "normalized_unit": self.normalized_unit,
            "pricing_snapshot_version": self.schema_version,
        }

    @classmethod
    def from_order_item(cls, row: Any) -> Optional["CommercialPricingSnapshot"]:
        version = getattr(row, "pricing_snapshot_version", None)
        if version is None:
            return None
        return cls(
            commercial_price_amount=to_decimal(row.commercial_price_amount, field="commercial_price_amount"),
            price_basis=PriceBasis(str(row.price_basis)),
            price_unit=_canon_unit(row.price_unit),
            package_type=_canon_word(row.package_type),
            package_content_amount=_opt_decimal(row.package_content_amount, "package_content_amount"),
            package_content_unit=_canon_unit(row.package_content_unit),
            commercial_quantity_unit=_canon_unit(row.quantity_unit),
            commercial_quantity_amount=_opt_decimal(getattr(row, "quantity", None), "quantity")
            if row.quantity_unit
            else None,
            normalized_unit_price=_opt_decimal(row.normalized_unit_price, "normalized_unit_price"),
            normalized_unit=_canon_unit(row.normalized_unit),
            currency=normalize_currency(row.currency),
            schema_version=int(version),
        )

    @classmethod
    def from_bid(cls, row: Any) -> Optional["CommercialPricingSnapshot"]:
        version = getattr(row, "pricing_snapshot_version", None)
        basis = getattr(row, "offered_price_basis", None)
        if version is None or basis in (None, LEGACY_UNSPECIFIED):
            return None
        return cls(
            commercial_price_amount=to_decimal(row.offered_price, field="offered_price"),
            price_basis=PriceBasis(str(basis)),
            price_unit=_canon_unit(row.offered_price_unit),
            package_type=_canon_word(row.package_type),
            package_content_amount=_opt_decimal(row.package_content_amount, "package_content_amount"),
            package_content_unit=_canon_unit(row.package_content_unit),
            normalized_unit_price=_opt_decimal(row.normalized_unit_price, "normalized_unit_price"),
            normalized_unit=_canon_unit(row.normalized_unit),
            currency=normalize_currency(row.offered_price_currency),
            schema_version=int(version),
        )


# ---------------------------------------------------------------------------
# Construction depuis le domaine canonique
# ---------------------------------------------------------------------------


def snapshot_from_offer(offer: CommercialOffer, *, currency: Optional[str] = None) -> CommercialPricingSnapshot:
    """Projection persistée d'une `CommercialOffer` VALID — refuse toute autre."""
    verdict = offer.validate() if hasattr(offer, "validate") else None
    if verdict is not None and not verdict.is_valid:
        raise PricingSnapshotError(f"offre non certifiable: {verdict.status} {verdict.missing_fields}{verdict.conflicts}")
    pr, pk, cq, inv = offer.pricing, offer.package, offer.commercial_quantity, offer.inventory_quantity
    if pr is None or pr.basis is None:
        raise PricingSnapshotError("prix ou base du prix manquants")
    if not pr.basis_source.is_execution_safe:
        raise PricingSnapshotError(f"base du prix non certifiée ({pr.basis_source.value})")
    is_package = pr.basis == PriceBasis.PER_PACKAGE
    snapshot = CommercialPricingSnapshot(
        commercial_price_amount=to_decimal(pr.amount, field="price"),
        price_basis=pr.basis,
        price_unit=_canon_unit(pr.basis_unit or (cq.unit if cq else None))
        if pr.basis == PriceBasis.PER_BASE_UNIT
        else None,
        package_type=_canon_word(pk.package_type) if is_package and pk else None,
        package_content_amount=to_decimal(pk.content_amount, field="package") if is_package and pk and pk.content_amount else None,
        package_content_unit=_canon_unit(pk.content_unit) if is_package and pk else None,
        commercial_quantity_amount=to_decimal(cq.amount, field="quantity") if cq else None,
        commercial_quantity_unit=_canon_unit(cq.unit) if cq else None,
        inventory_quantity_amount=to_decimal(inv.amount, field="inventory") if inv else None,
        inventory_quantity_unit=_canon_unit(inv.unit) if inv else None,
        currency=normalize_currency(currency or pr.currency),
        price_source=pr.basis_source.value,
    )
    return snapshot.with_normalized().assert_valid()


def build_bid_pricing_snapshot(
    *,
    amount: Any,
    basis: Any,
    price_unit: Optional[str],
    auction_quantity: Any,
    auction_unit: str,
    package_type: Optional[str] = None,
    package_content_amount: Any = None,
    package_content_unit: Optional[str] = None,
    currency: Optional[str] = None,
    source: Optional[str] = None,
) -> CommercialPricingSnapshot:
    """Snapshot d'un bid NEUF. La base est OBLIGATOIRE (« 450000 » seul n'est pas un bid) ; elle
    est validée contre l'unité de l'enchère mais n'en est JAMAIS déduite."""
    try:
        price_basis = PriceBasis(str(getattr(basis, "value", basis)))
    except ValueError as exc:
        raise PricingSnapshotError(f"base du prix absente ou inconnue: {basis!r}") from exc
    unit = _canon_unit(auction_unit)
    snapshot = CommercialPricingSnapshot(
        commercial_price_amount=to_decimal(amount, field="offered_price"),
        price_basis=price_basis,
        price_unit=_canon_unit(price_unit) if price_basis == PriceBasis.PER_BASE_UNIT else None,
        package_type=_canon_word(package_type) if price_basis == PriceBasis.PER_PACKAGE else None,
        package_content_amount=_opt_decimal(package_content_amount, "package_content_amount")
        if price_basis == PriceBasis.PER_PACKAGE
        else None,
        package_content_unit=_canon_unit(package_content_unit) if price_basis == PriceBasis.PER_PACKAGE else None,
        inventory_quantity_amount=to_decimal(auction_quantity, field="auction_quantity"),
        inventory_quantity_unit=unit,
        currency=normalize_currency(currency),
        price_source=source,
    )
    return snapshot.with_normalized().assert_valid()


def build_order_item_pricing_snapshot(
    *,
    product_price: Any,
    product_unit: str,
    quantity: Any,
    product_pricing_tiers: Optional[List[Mapping[str, Any]]] = None,
    product_commercial_pricing: Any = None,
    tier_id: Optional[str] = None,
    base_unit_quantity: Any = None,
    price_at_sale: Any = None,
    currency: Optional[str] = None,
    price_source: Optional[str] = None,
) -> CommercialPricingSnapshot:
    """LE constructeur unique du snapshot d'une ligne de commande (achat direct, précommande,
    allocation récurrente). Aucun appelant ne recompose ces champs.

    Ce qui est figé, dans l'ordre de priorité de la source :

    1. un PALIER acheté (`tier_id`) -> PER_PACKAGE exact (prix du paquet, contenu, conditionnement) ;
    2. le snapshot certifié du produit (`commercial_pricing`) :
       - PER_BASE_UNIT (« 450000 / TONNE » même si le stock est en KG) : conservé tel quel ;
       - TOTAL_LOT : le lot se vend ENTIER (jamais de vente partielle d'un prix total) ;
       - PER_PACKAGE acheté SANS palier (quantité en unité de base) : le prix RÉELLEMENT facturé
         est par unité de base -> PER_BASE_UNIT au prix de vente (`price_at_sale`). On n'écrit pas
         « 500 / sachet » pour une ligne qui a été facturée au litre : le snapshot dit ce qui s'est passé ;
    3. les champs plats du produit (`price` par `unit`, convention historique).

    `price_at_sale` (colonne héritée) doit s'accorder avec le snapshot : sinon `PricingSnapshotError`
    AVANT tout commit (dual-write)."""
    qty = to_decimal(quantity, field="quantity")
    unit = _canon_unit(product_unit) or "KG"
    cur = normalize_currency(currency)
    certified = CommercialPricingSnapshot.from_dict(product_commercial_pricing)

    tier = None
    if tier_id:
        for raw in product_pricing_tiers or []:
            if isinstance(raw, Mapping) and str(raw.get("tier_id")) == str(tier_id):
                tier = raw
                break
        if tier is None:
            raise PricingSnapshotError(f"palier introuvable (tier_id={tier_id})")

    if tier is not None:
        packaging = _canon_word(tier.get("packaging")) or "PAQUET"
        snapshot = CommercialPricingSnapshot(
            commercial_price_amount=to_decimal(tier.get("price"), field="tier.price"),
            price_basis=PriceBasis.PER_PACKAGE,
            package_type=packaging,
            package_content_amount=to_decimal(tier.get("quantity"), field="tier.quantity"),
            package_content_unit=_canon_unit(tier.get("unit")),
            commercial_quantity_amount=qty,
            commercial_quantity_unit=packaging,  # `quantity` = nombre de paquets
            inventory_quantity_amount=_opt_decimal(base_unit_quantity, "base_unit_quantity"),
            inventory_quantity_unit=unit,
            currency=cur,
            price_source=price_source or "PRODUCT_PRICING_TIER",
        )
    elif certified is not None and certified.price_basis == PriceBasis.TOTAL_LOT:
        lot = certified.inventory_quantity_amount
        lot_unit = certified.inventory_quantity_unit
        if lot is not None and lot_unit and (
            _unit_family(unit) != _unit_family(lot_unit)
            or qty * _factor(unit) != lot * _factor(lot_unit)
        ):
            raise PricingSnapshotError(
                f"un lot à prix total se vend entier ({lot} {lot_unit}), pas {qty} {unit}"
            )
        snapshot = replace(
            certified,
            commercial_quantity_amount=qty,
            commercial_quantity_unit=unit,
            price_source=price_source or certified.price_source,
        )
    elif certified is not None and certified.price_basis == PriceBasis.PER_BASE_UNIT:
        snapshot = replace(
            certified,
            commercial_quantity_amount=qty,
            commercial_quantity_unit=unit,
            inventory_quantity_amount=qty,
            inventory_quantity_unit=unit,
            price_source=price_source or certified.price_source,
        )
    else:
        # champs plats du produit, ou PER_PACKAGE certifié acheté à l'unité de base : le prix
        # facturé (`price_at_sale`, sinon `price`) est un prix PAR UNITÉ de vente.
        charged = price_at_sale if price_at_sale is not None else product_price
        source = price_source or (
            "NORMALIZED_FROM_CERTIFIED_PACKAGE" if certified is not None else "PRODUCT_LEGACY_FIELDS"
        )
        snapshot = CommercialPricingSnapshot(
            commercial_price_amount=to_decimal(charged, field="price"),
            price_basis=PriceBasis.PER_BASE_UNIT,
            price_unit=unit,
            commercial_quantity_amount=qty,
            commercial_quantity_unit=unit,
            inventory_quantity_amount=qty,
            inventory_quantity_unit=unit,
            currency=cur,
            price_source=source,
        )
    snapshot = snapshot.with_normalized().assert_valid()
    if price_at_sale is not None:
        assert_order_item_legacy_matches(snapshot, quantity=qty, price_at_sale=price_at_sale)
    return snapshot


def build_total_lot_order_item_snapshot(
    *,
    total_amount: Any,
    quantity: Any,
    unit: str,
    price_at_sale: Any = None,
    currency: Optional[str] = None,
    price_source: str = "DIRECT_SALE_DECLARED",
) -> CommercialPricingSnapshot:
    """Vente déclarée « j'ai vendu 50 kg pour 25 000 » : le producteur a dit un TOTAL, pas un prix
    unitaire — c'est un TOTAL_LOT, le prix unitaire n'est qu'un dérivé."""
    qty = to_decimal(quantity, field="quantity")
    u = _canon_unit(unit) or "KG"
    snapshot = CommercialPricingSnapshot(
        commercial_price_amount=to_decimal(total_amount, field="total_amount"),
        price_basis=PriceBasis.TOTAL_LOT,
        commercial_quantity_amount=qty,
        commercial_quantity_unit=u,
        inventory_quantity_amount=qty,
        inventory_quantity_unit=u,
        currency=normalize_currency(currency),
        price_source=price_source,
    ).with_normalized().assert_valid()
    if price_at_sale is not None:
        assert_order_item_legacy_matches(snapshot, quantity=qty, price_at_sale=price_at_sale)
    return snapshot


# ---------------------------------------------------------------------------
# Dual-write : la projection legacy doit égaler celle du snapshot (échec AVANT commit)
# ---------------------------------------------------------------------------


def assert_order_item_legacy_matches(
    snapshot: CommercialPricingSnapshot, *, quantity: Any, price_at_sale: Any
) -> None:
    """`order_items.price_at_sale` (colonne héritée) doit être la projection du snapshot :
    prix du conditionnement / de l'unité pour PER_PACKAGE et PER_BASE_UNIT (à un centime),
    prix unitaire dérivé pour TOTAL_LOT (le total, lui, reste exact dans le snapshot)."""
    legacy = to_decimal(price_at_sale, field="price_at_sale")
    qty = to_decimal(quantity, field="quantity")
    if snapshot.price_basis == PriceBasis.TOTAL_LOT:
        total = snapshot.commercial_price_amount
        if abs(legacy * qty - total) > LEGACY_TOLERANCE * qty:
            raise PricingSnapshotError(
                f"dual-write: price_at_sale {legacy} × {qty} ≠ total du lot {total}"
            )
        return
    expected = snapshot.commercial_price_amount
    if snapshot.price_basis == PriceBasis.PER_BASE_UNIT and snapshot.price_unit and snapshot.commercial_quantity_unit:
        # « 450000 / TONNE » vendu en KG : price_at_sale est le prix PAR KG (450), pas 450000.
        expected = (
            snapshot.commercial_price_amount
            * _factor(snapshot.commercial_quantity_unit)
            / _factor(snapshot.price_unit)
        )
    if abs(legacy - expected) > LEGACY_TOLERANCE:
        raise PricingSnapshotError(
            f"dual-write: price_at_sale {legacy} ≠ prix du snapshot {expected} ({snapshot.price_basis.value})"
        )


def assert_legacy_projection_matches(
    snapshot: CommercialPricingSnapshot, *, legacy_price: Any, legacy_unit: Optional[str]
) -> None:
    """`products.price`/`products.unit` = projection du prix NORMALISÉ (par unité de base)."""
    if snapshot.normalized_unit_price is None:
        raise PricingSnapshotError("dual-write: snapshot sans prix normalisé, projection legacy impossible")
    if abs(to_decimal(legacy_price, field="legacy_price") - snapshot.normalized_unit_price) > LEGACY_TOLERANCE:
        raise PricingSnapshotError(
            f"dual-write: products.price {legacy_price} ≠ prix normalisé {snapshot.normalized_unit_price}"
        )
    if legacy_unit and snapshot.normalized_unit and _canon_unit(legacy_unit) != snapshot.normalized_unit:
        raise PricingSnapshotError(
            f"dual-write: products.unit {legacy_unit} ≠ unité normalisée {snapshot.normalized_unit}"
        )


# ---------------------------------------------------------------------------
# Total d'attribution d'un appel d'offres
# ---------------------------------------------------------------------------


def award_total(snapshot: CommercialPricingSnapshot, auction_quantity: Any, auction_unit: str) -> Decimal:
    """Total dû pour l'enchère attribuée, selon la BASE déclarée du bid (jamais l'unité de
    l'enchère supposée) : 450000/TONNE × 10 TONNE = 4 500 000 ; TOTAL_LOT = le montant tel quel."""
    return snapshot.total_for(auction_quantity, auction_unit)


# ---------------------------------------------------------------------------
# Achat d'un produit conditionné (Étape 19)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackagePurchase:
    status: str  # "OK" | "NEEDS_PACKAGE_COUNT" | "NOT_DIVISIBLE" | "NOT_PACKAGED"
    package_count: Optional[int] = None
    base_quantity: Optional[Decimal] = None
    reason: Optional[str] = None


def resolve_package_purchase(
    snapshot: CommercialPricingSnapshot,
    requested_amount: Any,
    requested_unit: Optional[str] = None,
    *,
    mode: PackageSaleMode = PackageSaleMode.PACKAGE_ONLY,
) -> PackagePurchase:
    """Combien de conditionnements représente la demande de l'acheteur ? Jamais de décision
    arbitraire : refus explicite plutôt qu'arrondi (2 L = 4 sachets de 0,5 L ; 1,2 L n'est pas
    divisible)."""
    if snapshot.price_basis != PriceBasis.PER_PACKAGE:
        return PackagePurchase("NOT_PACKAGED", reason="prix non conditionné")
    amount = to_decimal(requested_amount, field="requested_amount")
    content = (snapshot.package_content_amount or _ZERO) * _factor(snapshot.package_content_unit or "")
    if content <= 0:
        return PackagePurchase("NOT_DIVISIBLE", reason="contenu du conditionnement inconnu")
    unit = _canon_unit(requested_unit)
    content_family = _unit_family(snapshot.package_content_unit or "")
    in_content_units = bool(unit) and _unit_family(unit or "") == content_family and unit != snapshot.package_type
    # « 3 L » sur des paquets de 1 L EST un nombre de paquets (aucune ambiguïté).
    unit_content_is_one = (snapshot.package_content_amount or _ZERO) == 1 and (
        _canon_unit(snapshot.package_content_unit) == unit
    )
    if not unit or unit == snapshot.package_type or unit_content_is_one or not in_content_units:
        count = amount
        base = amount * content
    else:
        if mode == PackageSaleMode.PACKAGE_ONLY:
            return PackagePurchase(
                "NEEDS_PACKAGE_COUNT",
                reason=f"achat par {snapshot.package_type} uniquement: indiquer un nombre de {snapshot.package_type}",
            )
        base = amount * _factor(unit)
        count = base / content
    if count <= 0 or count != count.to_integral_value():
        return PackagePurchase(
            "NOT_DIVISIBLE",
            base_quantity=base,
            reason=f"{amount} {unit or snapshot.package_type} n'est pas un nombre entier de {snapshot.package_type}",
        )
    return PackagePurchase("OK", package_count=int(count), base_quantity=base)


# ---------------------------------------------------------------------------
# Lecture rétro-compatible (lignes sans snapshot)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PricingView:
    """Ce qu'on peut affirmer d'une ligne, sans jamais fabriquer de certitude."""

    amount: Optional[Decimal]
    currency: str
    basis: Optional[PriceBasis]  # None = INCONNUE (jamais « par unité de l'enchère »)
    reliability: PricingReliability
    snapshot: Optional[CommercialPricingSnapshot] = None
    note: Optional[str] = None
    #: produit à `pricing_tiers` sans snapshot certifié : le libellé COMMERCIAL est celui des paliers ;
    #: `amount` (le `Product.price` legacy shadow, prix brut du 1er palier) n'est alors JAMAIS affiché.
    tiers_label: Optional[str] = None

    @property
    def basis_label(self) -> str:
        return self.basis.value if self.basis else "UNKNOWN"

    @property
    def status(self) -> str:
        """`CERTIFIED` / `LEGACY_PARTIAL` / `UNKNOWN_BASIS` (mandat B2c.4 §3) — alias lisible de
        `reliability`, un seul champ à tester côté consommateur."""
        return self.reliability.value

    @property
    def pricing_label(self) -> str:
        """Le SEUL libellé commercial à afficher (mandat B2c.4 §3-8) — jamais reconstruit dans un
        node/renderer buyer. Une ligne CERTIFIÉE affiche sa base réelle (« 500 FCFA par sachet de
        0,5 L », jamais « 1000 FCFA/L » — voir `render_pricing_label`) ; une ligne LEGACY_PARTIAL
        affiche le montant SANS inventer de base ; une ligne sans montant n'a rien à afficher."""
        if self.snapshot is not None:
            return render_pricing_label(self.snapshot)
        if self.tiers_label:
            return self.tiers_label
        if self.amount is not None:
            return f"{fmt_num(float(self.amount))} FCFA — base historique non certifiée"
        return "Prix non disponible"

    @property
    def is_comparable(self) -> bool:
        """Un prix normalisé (par unité de BASE) n'existe que pour une vue CERTIFIÉE dont le lot a
        une quantité connue (mandat B2c.4 §17) — jamais déduit d'une ligne LEGACY_PARTIAL/
        UNKNOWN_BASIS, ni d'un TOTAL_LOT sans inventaire pour se ramener à un prix unitaire."""
        return self.snapshot is not None and self.snapshot.normalized_unit_price is not None

    @property
    def not_comparable_reason(self) -> Optional[str]:
        if self.is_comparable:
            return None
        if self.snapshot is not None:
            return "quantité de référence inconnue pour normaliser ce lot"
        return self.note or "base de prix non certifiée"


def legacy_pricing_view(amount: Any, *, currency: Optional[str] = None, partial: bool = False) -> PricingView:
    return PricingView(
        amount=_opt_decimal(amount, "amount"),
        currency=normalize_currency(currency),
        basis=None,
        reliability=PricingReliability.LEGACY_PARTIAL if partial else PricingReliability.UNKNOWN_BASIS,
        note="base de prix historique inconnue",
    )


def bid_pricing_view(bid: Any) -> PricingView:
    snapshot = CommercialPricingSnapshot.from_bid(bid)
    if snapshot is None:
        return legacy_pricing_view(getattr(bid, "offered_price", None), currency=getattr(bid, "offered_price_currency", None))
    return PricingView(
        amount=snapshot.commercial_price_amount,
        currency=snapshot.currency,
        basis=snapshot.price_basis,
        reliability=PricingReliability.CERTIFIED,
        snapshot=snapshot,
    )


def order_item_pricing_view(item: Any) -> PricingView:
    snapshot = CommercialPricingSnapshot.from_order_item(item)
    if snapshot is None:
        return legacy_pricing_view(getattr(item, "price_at_sale", None), partial=True)
    return PricingView(
        amount=snapshot.commercial_price_amount,
        currency=snapshot.currency,
        basis=snapshot.price_basis,
        reliability=PricingReliability.CERTIFIED,
        snapshot=snapshot,
    )


def _field(row: Any, name: str) -> Any:
    """Lit `name` sur une ligne ORM (`getattr`) OU une `RowMapping`/dict issue de `.mappings()`
    (mandat B2c.4 : les consommateurs buyer — `services/database/buyer.py::search_products` —
    sélectionnent des colonnes en SQL brut, pas des objets ORM ; un seul accesseur ici évite un
    adaptateur par appelant)."""
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def product_pricing_view(product: Any) -> PricingView:
    snapshot = CommercialPricingSnapshot.from_dict(_field(product, "commercial_pricing"))
    if snapshot is None:
        from ladini.domain.pricing_tiers import describe_tiers

        tiers_label = describe_tiers(_field(product, "pricing_tiers"))
        if tiers_label:
            # `Product.price` d'un produit à paliers = champ de COMPATIBILITÉ legacy, pas la vérité
            # commerciale : le montant n'est pas exposé, seuls les paliers font foi.
            return PricingView(
                amount=None,
                currency=normalize_currency(None),
                basis=None,
                reliability=PricingReliability.LEGACY_PARTIAL,
                note="tarification par conditionnements",
                tiers_label=tiers_label,
            )
        return legacy_pricing_view(_field(product, "price"), partial=True)
    return PricingView(
        amount=snapshot.commercial_price_amount,
        currency=snapshot.currency,
        basis=snapshot.price_basis,
        reliability=PricingReliability.CERTIFIED,
        snapshot=snapshot,
    )


def market_offer_pricing_view(offer_row: Any) -> PricingView:
    """Sémantique d'un `MarketOffer` (production future) — même contrat que `product_pricing_view` :
    un snapshot présent (`pricing_snapshot`) est CERTIFIÉ ; sinon `price_per_unit` legacy est affiché
    tel quel, JAMAIS relu comme « certainement par unité » (une ligne antérieure à B2c.3 ne dit rien
    sur sa base — voir `market_offers_pricing_snapshot_chk`, la colonne n'est contrainte que si non
    NULL, donc son absence est une vraie inconnue, pas une omission à combler)."""
    snapshot = CommercialPricingSnapshot.from_dict(_field(offer_row, "pricing_snapshot"))
    if snapshot is None:
        return legacy_pricing_view(_field(offer_row, "price_per_unit"), partial=True)
    return PricingView(
        amount=snapshot.commercial_price_amount,
        currency=snapshot.currency,
        basis=snapshot.price_basis,
        reliability=PricingReliability.CERTIFIED,
        snapshot=snapshot,
    )


# ---------------------------------------------------------------------------
# Affichage & comparaison (mandat B2c.4 §3 : UNE seule API — un formatter/comparateur
# canonique, jamais un par node/consumer). Relocalisé depuis `bid_pricing_flow.py`
# (2026-09-28) : ne dépend que de `CommercialPricingSnapshot`, rien de spécifique aux bids —
# `bid_pricing_flow` ré-exporte les deux noms pour ses appelants existants.
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


def comparable_total(snapshot: CommercialPricingSnapshot, quantity: Any, unit: str) -> Optional[Decimal]:
    """Total comparable de ce snapshot pour `quantity`/`unit` demandés, ou `None` (jamais deviné —
    ex: PER_PACKAGE sur une quantité qui n'est pas un nombre entier de conditionnements)."""
    try:
        return snapshot.total_for(quantity, unit)
    except PricingSnapshotError:
        return None


__all__ = [
    "SNAPSHOT_SCHEMA_VERSION",
    "DEFAULT_CURRENCY",
    "LEGACY_UNSPECIFIED",
    "CERTIFIED_BASES",
    "PricingSnapshotError",
    "PricingReliability",
    "PackageSaleMode",
    "CommercialPricingSnapshot",
    "PackagePurchase",
    "PricingView",
    "to_decimal",
    "quantize_money",
    "quantize_normalized",
    "normalize_currency",
    "snapshot_from_offer",
    "build_bid_pricing_snapshot",
    "build_order_item_pricing_snapshot",
    "build_total_lot_order_item_snapshot",
    "assert_order_item_legacy_matches",
    "assert_legacy_projection_matches",
    "award_total",
    "resolve_package_purchase",
    "legacy_pricing_view",
    "bid_pricing_view",
    "order_item_pricing_view",
    "product_pricing_view",
    "market_offer_pricing_view",
    "render_pricing_label",
    "comparable_total",
]
