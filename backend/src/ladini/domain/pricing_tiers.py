"""Typed, validated multi-tier pricing — shared producer + buyer domain logic.

Refonte 2026-08-30 (demande explicite utilisateur, voir plan
`humming-knitting-dragon`) : `pricing_tiers` a été introduit le 2026-08-27
comme un JSON libre, seulement filtré au fil de l'eau par plusieurs points
d'extraction (LLM, parseur déterministe) et un sanitizing léger côté écriture
(`services/database/producer.py::create_product`). Ce module devient la
SEULE source de vérité pour valider et structurer un `pricing_tiers` — tout
le reste (extraction, sanitizing champ-par-champ) reste inchangé et continue
d'alimenter cette validation en entrée.

`tier_id`/`base_unit_quantity`/`min_order_quantity` sont calculés SERVEUR,
jamais demandés au LLM ni au parseur déterministe — ça évite de rouvrir le
prompt/regex déjà durci ce soir pour l'extraction brute (quantity/unit/
price/packaging).
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ladini.domain.quantity_unit import (
    convert_quantity,
    normalize_unit,
)

# ---------------------------------------------------------------------------
# Familles d'unités — voir la décision de scope du plan : seules les
# conversions déjà connues du codebase (masse KG<->TONNE, volume LITRE) sont
# supportées. SAC/PANIER/TETE/UNITE restent des familles à elles seules
# (aucune conversion connue) : un tarif dans l'une de ces unités doit
# correspondre EXACTEMENT à l'unité de base du produit.
# ---------------------------------------------------------------------------
_MASS_FACTORS: Dict[str, float] = {
    "KG": 1.0,
    "KGS": 1.0,
    "KILO": 1.0,
    "KILOS": 1.0,
    "KILOGRAMME": 1.0,
    "KILOGRAMMES": 1.0,
    "G": 0.001,
    "GRAMME": 0.001,
    "GRAMMES": 0.001,
    "TONNE": 1000.0,
    "TONNES": 1000.0,
    "TON": 1000.0,
    "TONS": 1000.0,
    "TONE": 1000.0,
    "TONES": 1000.0,
    "T": 1000.0,
}
_VOLUME_FACTORS: Dict[str, float] = {
    "L": 1.0,
    "LITRE": 1.0,
    "LITRES": 1.0,
}


def _unit_family(unit: str) -> str:
    u = unit.strip().upper()
    if u in _MASS_FACTORS:
        return "MASS"
    if u in _VOLUME_FACTORS:
        return "VOLUME"
    return u  # famille singleton (SAC/PANIER/TETE/UNITE/...) : elle-même


def _unit_factor(unit: str) -> float:
    u = unit.strip().upper()
    if u in _MASS_FACTORS:
        return _MASS_FACTORS[u]
    if u in _VOLUME_FACTORS:
        return _VOLUME_FACTORS[u]
    return 1.0  # famille singleton : jamais convertie, facteur neutre


class PricingTierError(ValueError):
    """Levée quand un `pricing_tiers` échoue la validation — tout le
    payload est rejeté, jamais accepté à moitié (exigence explicite
    utilisateur : aucun prix/unité ne doit être écrasé ou mal interprété)."""


class PricingTier(BaseModel):
    """Un palier de prix/conditionnement validé et structuré."""

    tier_id: str
    quantity: float = Field(gt=0)
    unit: str  # littéral, jamais normalisé — voir domain/catalog/models.py
    price: float = Field(gt=0)
    packaging: Optional[str] = None
    base_unit_quantity: float = Field(gt=0)
    min_order_quantity: int = Field(default=1, ge=1)

    model_config = {"frozen": True}


def validate_pricing_tiers(
    raw_tiers: Optional[List[Dict[str, Any]]], base_unit: str
) -> List[PricingTier]:
    """Valide et structure une liste brute de tarifs.

    Lève `PricingTierError` (jamais de résultat partiel) si : un tarif n'est
    pas un objet, une unité/quantité/prix manque ou est invalide, une unité
    est d'une famille incompatible avec `base_unit`, ou un doublon
    (quantité+unité+conditionnement identiques) est détecté.
    """
    if not raw_tiers:
        return []

    base_unit_clean = str(base_unit or "").strip()
    if not base_unit_clean:
        raise PricingTierError(
            "Impossible de valider des tarifs multiples sans unité de base pour le produit."
        )
    base_family = _unit_family(base_unit_clean)
    base_factor = _unit_factor(base_unit_clean)

    tiers: List[PricingTier] = []
    seen: set = set()
    for i, raw in enumerate(raw_tiers):
        if not isinstance(raw, dict):
            raise PricingTierError(
                f"pricing_tiers[{i}] doit être un objet, reçu {type(raw).__name__}."
            )

        unit_raw = str(raw.get("unit") or "").strip()
        if not unit_raw:
            raise PricingTierError(f"pricing_tiers[{i}] : unité manquante.")

        family = _unit_family(unit_raw)
        if family != base_family:
            raise PricingTierError(
                f"pricing_tiers[{i}] : unité '{unit_raw}' incompatible avec "
                f"l'unité de base du produit ('{base_unit_clean}')."
            )

        try:
            quantity = float(raw.get("quantity"))
            price = float(raw.get("price"))
        except (TypeError, ValueError) as exc:
            raise PricingTierError(
                f"pricing_tiers[{i}] : quantité ou prix invalide."
            ) from exc

        packaging_raw = raw.get("packaging")
        packaging = str(packaging_raw).strip() if packaging_raw else None
        packaging = packaging or None

        dedup_key = (round(quantity, 6), unit_raw.upper(), (packaging or "").lower())
        if dedup_key in seen:
            label = f"{quantity} {unit_raw}" + (f" ({packaging})" if packaging else "")
            raise PricingTierError(f"pricing_tiers[{i}] : tarif en double ({label}).")
        seen.add(dedup_key)

        base_unit_quantity = quantity * (_unit_factor(unit_raw) / base_factor)
        tier_id = str(raw.get("tier_id") or uuid.uuid4().hex[:12])
        min_order_quantity_raw = raw.get("min_order_quantity") or 1
        try:
            min_order_quantity = int(min_order_quantity_raw)
        except (TypeError, ValueError):
            min_order_quantity = 1

        try:
            tiers.append(
                PricingTier(
                    tier_id=tier_id,
                    quantity=quantity,
                    unit=unit_raw,
                    price=price,
                    packaging=packaging,
                    base_unit_quantity=base_unit_quantity,
                    min_order_quantity=min_order_quantity,
                )
            )
        except Exception as exc:  # pydantic.ValidationError
            raise PricingTierError(f"pricing_tiers[{i}] : {exc}") from exc

    return tiers


def tiers_to_dicts(tiers: List[PricingTier]) -> List[Dict[str, Any]]:
    return [t.model_dump() for t in tiers]


# ---------------------------------------------------------------------------
# Côté acheteur (Phase B) — sélection d'un palier + calcul de ligne.
# ---------------------------------------------------------------------------


# (2026-08-30, refonte "LLM pilote la sélection de palier") : le matching
# déterministe en texte libre (regex ordinaux + quantité/unité) a été
# supprimé — il cassait à chaque variation de formulation et dupliquait ce
# que le LLM sait déjà faire mieux. Le LLM reçoit désormais la liste des
# paliers actifs directement dans son prompt (voir `interpreter/routing.py`,
# variable `tier_menu_context`) et renvoie l'id exact choisi dans
# `extracted_entities.selected_value` — jamais une description, jamais un id
# inventé (même discipline que la règle "anti-hallucination d'IDs" déjà en
# place pour la sélection générique). `cart.py` reste le seul juge : il
# valide que ce `selected_value` correspond bien à un `tier_id` de la liste
# actuellement affichée avant de l'utiliser — c'est ce contrôle, pas un
# regex, qui protège contre une éventuelle hallucination du LLM.


def resolve_tier(
    pricing_tiers_raw: Optional[List[Dict[str, Any]]], tier_id: str
) -> PricingTier:
    """Retrouve un tarif déjà persisté (dict brut venant de `Product.pricing_tiers`)
    par son `tier_id`. Les tarifs persistés sont déjà passés par
    `validate_pricing_tiers` à l'écriture — reconstruit juste le modèle typé."""
    for raw in pricing_tiers_raw or []:
        if isinstance(raw, dict) and str(raw.get("tier_id")) == str(tier_id):
            return PricingTier(**raw)
    raise PricingTierError(f"Tarif introuvable (tier_id={tier_id}).")


class ComputedLine(BaseModel):
    price_total: float
    base_unit_quantity: float


def compute_line(tier: PricingTier, requested_pack_count: int) -> ComputedLine:
    """Prix total + quantité en unité de base pour `requested_pack_count`
    exemplaires du palier `tier` (ex: 3 bidons de 10L). Lève `PricingTierError`
    si en dessous du seuil minimal du palier."""
    if requested_pack_count < tier.min_order_quantity:
        raise PricingTierError(
            f"Quantité minimale pour ce tarif : {tier.min_order_quantity} "
            f"({tier.quantity} {tier.unit} chacun)."
        )
    return ComputedLine(
        price_total=tier.price * requested_pack_count,
        base_unit_quantity=tier.base_unit_quantity * requested_pack_count,
    )


# ---------------------------------------------------------------------------
# NOMBRE DE PAQUETS ≠ QUANTITÉ GLOBALE (audit 2026-09-01)
# ---------------------------------------------------------------------------
# Un `package_count` est SANS DIMENSION : c'est un nombre d'exemplaires du
# conditionnement ("3 bidons"), jamais une quantité en unité de base ("30 L").
# Incident réel reproduit : sur la question "Combien de 10 L (bidon) ?" une
# réponse "30 litres" était acceptée telle quelle comme 30 PAQUETS → 300 L
# facturés 27 000 FCFA au lieu des 3 bidons/2 700 FCFA attendus. Le garde
# historique (unité de famille incompatible, ex "3 kg" sur un palier en L) ne
# pouvait structurellement pas l'attraper : `L` et `LITRE` normalisent vers la
# MÊME unité que le palier, donc le contrôle passait.
#
# Ces trois verdicts sont la SEULE source de vérité sur "ce nombre est-il un
# nombre de paquets ?" — consommée par le parseur (interpreter/routing.py, qui
# n'invente plus d'unité héritée quand un nombre de paquets est attendu) et par
# le domaine (services/domain/cart_service.py, qui refuse explicitement au lieu
# de convertir : aucune résolution automatique "30 L → 3 bidons", cf. la règle
# métier "pas de tetris de conditionnements").
PACK_UNIT_PACK = "PACK"  # nombre de paquets exploitable tel quel
PACK_UNIT_CONTENT = "CONTENT"  # exprimé dans l'unité du CONTENU du palier
PACK_UNIT_FOREIGN = "FOREIGN"  # unité étrangère au palier (ex: "3 kg" sur du L)


def _packaging_tokens(tier: "PricingTier") -> set:
    """Mots désignant le CONDITIONNEMENT lui-même ("bidon", "bidons", "sac").

    Répondre "3 sacs" à "combien de sacs de 50 KG ?" EST un nombre de paquets —
    même quand `normalize_unit("sacs")` renvoie une vraie unité (SAC) d'une
    famille différente de celle du palier (KG). Sans cette liste, ce cas
    pourtant parfaitement clair tombait dans le refus `FOREIGN`.
    """
    packaging = str(tier.packaging or "").strip().lower()
    if not packaging:
        return set()
    return {packaging, packaging + "s", packaging.rstrip("s")}


def classify_pack_count_unit(
    tier: "PricingTier", buyer_unit: Optional[str]
) -> str:
    """Que désigne l'unité écrite par l'acheteur en réponse à "combien de X ?".

    Retourne `PACK_UNIT_PACK` / `PACK_UNIT_CONTENT` / `PACK_UNIT_FOREIGN`.
    `buyer_unit` DOIT être l'unité extraite du message de CE tour — jamais
    celle héritée du payload fusionné, qui porte encore l'unité de la demande
    initiale ("30 L de lait") et ferait refuser un "3" pourtant valide.
    """
    raw = str(buyer_unit or "").strip().lower()
    if not raw:
        # Aucune unité écrite : un nombre nu est LE format attendu.
        return PACK_UNIT_PACK
    if raw in _packaging_tokens(tier):
        return PACK_UNIT_PACK

    requested = normalize_unit(raw)
    if requested is None:
        # Mot inconnu du référentiel d'unités ("bidons", "paquets", "cartons")
        # → ce n'est pas une mesure, donc pas une quantité globale déguisée.
        return PACK_UNIT_PACK

    tier_unit = normalize_unit(tier.unit) or str(tier.unit or "").upper()
    same_family = requested == tier_unit or (
        convert_quantity(1.0, requested, tier_unit) is not None
    )
    if not same_family:
        return PACK_UNIT_FOREIGN
    if float(tier.quantity) == 1.0:
        # Le conditionnement contient EXACTEMENT une unité de base : "3 L" et
        # "3 paquets d'1 L" désignent la même chose, aucune ambiguïté.
        return PACK_UNIT_PACK
    return PACK_UNIT_CONTENT


def resolve_stock_debit(order_item: Any) -> float:
    """Quantité (en unité de base du produit) à débiter de
    `Product.quantity_for_sale` pour CET `order_item` — SEUL point de calcul
    du débit de stock, remplace `item.quantity` utilisé directement aux 5
    sites historiques (`services/database/buyer.py`, `.../escrow.py`).
    `base_unit_quantity` est `None` pour toute commande sans palier (créée
    avant cette refonte, ou produit sans `pricing_tiers`) — repli sur
    `quantity` pour rester rigoureusement identique au comportement actuel."""
    base_qty = getattr(order_item, "base_unit_quantity", None)
    if base_qty is not None:
        return float(base_qty)
    return float(getattr(order_item, "quantity", 0) or 0)


__all__ = [
    "PricingTier",
    "PricingTierError",
    "validate_pricing_tiers",
    "tiers_to_dicts",
    "resolve_tier",
    "ComputedLine",
    "compute_line",
    "resolve_stock_debit",
    "PACK_UNIT_PACK",
    "PACK_UNIT_CONTENT",
    "PACK_UNIT_FOREIGN",
    "classify_pack_count_unit",
]
