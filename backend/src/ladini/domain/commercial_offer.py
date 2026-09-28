"""Commercial offer semantics — single source of truth for "what does this
price actually mean" (mandat architectural 2026-09-28, "COMMERCIAL QUANTITY &
PRICING DOMAIN HARDENING").

## Pourquoi ce module existe

Avant ce module, une offre commerciale ("50 litres de lait à 500 F le
sachet") était pensée comme un triplet plat `(quantity, unit, price)`. Ce
triplet ne peut PAS représenter "500 F est un prix PAR SACHET, pas par
litre" — la seule façon de le faire aujourd'hui est de fourrer "SACHET"
dans le champ `unit` de la QUANTITÉ, ce qui écrase la vraie unité physique
(LITRE) et fait perdre l'information "combien de litres contient un
sachet ?" entièrement. Incidents réels qui en découlent, tous confirmés
par audit direct du code (pas des hypothèses) :

- "500 F le sachet" lu comme 500 F/litre (le sachet n'a jamais eu de champ
  pour porter son propre contenu physique) ;
- "200 tonnes à 500 000 F la tonne" exécuté comme 500 000 F/kg — la
  quantité est convertie en kg (`actions/common.py::normalize_quantity_to_kg`)
  mais le prix ne l'est JAMAIS en retour (`domain/sales.py::publish_product`,
  `domain/agro.py::declare_crop_cycle`, `domain/procurement.py::create_request`
  — 3 sites confirmés, même bug, même cause) : un facteur 1000x qui ne
  déclenche AUCUNE erreur ;
- "3 sacs" silencieusement réinterprété comme "300 KG" via une table de
  conversion `SAC=100kg/PANIER=25kg/CHARRETTE=250kg` codée en dur
  (`actions/common.py::_UNIT_TO_KG`) — un SAC ou un PANIER n'est PAS une
  unité de masse, c'est un contenant dont le contenu réel varie par
  producteur ; deviner un poids fixe est exactement le type d'erreur que ce
  module doit rendre impossible.
- `UNIT_SYNONYMS` (`domain/quantity_unit.py`) mappe "sachet"/"sachets" vers
  le MÊME code `"SAC"` qu'un sac de 50kg — deux conditionnements de tailles
  radicalement différentes confondus en une seule unité.

## Principe central

Une valeur commerciale CRITIQUE (un prix, une quantité destinée à
l'exécution) ne doit jamais exister sans que sa PROVENANCE et sa BASE
soient explicites :

    price_amount  seul n'est JAMAIS exécutable.
    Il doit exister price_amount + price_basis.

    package_type  seul n'est JAMAIS une unité physique.
    S'il porte le prix (PER_PACKAGE), son contenu physique
    (content_amount + content_unit) doit être connu — sinon
    INCOMPLETE, jamais une exécution devinée.

## Ce que ce module NE fait PAS (délibérément)

- Il ne réimplémente AUCUNE conversion d'unité : les familles MASS/VOLUME
  et leurs facteurs restent dans `domain/pricing_tiers.py`
  (`unit_family`/`unit_factor`, la table la plus complète du dépôt) et
  `domain/quantity_unit.py` (`convert_quantity`). Ce module les RÉUTILISE.
- Il ne remplace pas `domain/pricing_tiers.py::PricingTier` (le moteur de
  paliers de prix existant, producteur ET acheteur) — il lui donne le
  vocabulaire (`PriceBasis`, `PackageDefinition`, `Provenance`) qu'il lui
  manquait pour distinguer explicitement "conditionnement" de "unité de
  mesure", au lieu de laisser `packaging: Optional[str]` et `unit: str`
  interchangeables dans le système de types.
- Il ne crée pas un deuxième `SalesPublishDraft` : `validate_commercial_offer`
  ci-dessous est conçu pour être appelé PAR les drafts existants
  (`SalesPublishDraft`, `ProcurementDraft`, ...), pas pour les remplacer.
- Il ne migre pas la base de données. Voir
  `docs/domain/COMMERCIAL_QUANTITY_PRICING_MODEL.md` pour le plan de
  migration par étapes (Phase A ce module + adaptateurs de compatibilité,
  Phase B migration DB additive, Phase C audit/backfill, Phase D retrait du
  legacy) — ce fichier est la Phase A.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from ladini.domain.pricing_tiers import unit_factor as _unit_factor
from ladini.domain.pricing_tiers import unit_family as _unit_family


class PriceBasis(str, Enum):
    """Ce à quoi un montant de prix se rapporte — jamais implicite.

    `price_amount` sans `PriceBasis` n'a pas de sens exécutable : "500" ne
    dit rien sur si c'est par kg, par sachet, ou pour tout le lot.
    """

    PER_BASE_UNIT = "PER_BASE_UNIT"  # par l'unité physique du produit (kg, litre, tête...)
    PER_PACKAGE = "PER_PACKAGE"  # par exemplaire d'un conditionnement (sac, sachet, bidon...)
    TOTAL_LOT = "TOTAL_LOT"  # pour la totalité de la quantité annoncée, pas un prix unitaire


class Provenance(str, Enum):
    """D'où vient une valeur critique — détermine si elle peut être exécutée.

    Règle centrale (mandat) : `LLM_INFERRED` ou `UNKNOWN` sur une donnée
    financière critique (price.amount, price.basis) → NO BUSINESS WRITE,
    sauf règle déterministe validée (ex: une seule provenance déterministe
    dans ce module : `UNIT_CONVERSION`, jamais une supposition).
    """

    USER_EXPLICIT = "USER_EXPLICIT"  # l'utilisateur l'a dit explicitement, ce tour
    QUESTION_CONTEXT_EXPLICIT = "QUESTION_CONTEXT_EXPLICIT"  # rendu non ambigu par la question posée
    DOMAIN_DERIVED = "DOMAIN_DERIVED"  # dérivé d'une règle métier déterministe (ex: taxonomy)
    UNIT_CONVERSION = "UNIT_CONVERSION"  # calculé par une conversion déterministe certifiée
    DATABASE_VERIFIED = "DATABASE_VERIFIED"  # relu/validé contre une ligne DB existante
    LLM_INFERRED = "LLM_INFERRED"  # extrait par le LLM sans confirmation explicite ni contexte
    UNKNOWN = "UNKNOWN"  # provenance non établie — jamais suffisant pour exécuter

    @property
    def is_execution_safe(self) -> bool:
        """False pour LLM_INFERRED/UNKNOWN — voir docstring de la classe."""
        return self not in (Provenance.LLM_INFERRED, Provenance.UNKNOWN)


class PackageStatus(str, Enum):
    KNOWN = "KNOWN"  # content_amount + content_unit sont renseignés et fiables
    UNKNOWN = "UNKNOWN"  # un conditionnement est en jeu mais son contenu n'est pas connu
    NOT_REQUIRED = "NOT_REQUIRED"  # pas de conditionnement pertinent pour cette offre


@dataclass(frozen=True)
class PackageDefinition:
    """Un conditionnement (SAC, SACHET, BIDON, CAISSE, PANIER...) — jamais
    une unité de mesure physique, même quand le texte utilisateur les
    mélange. `content_amount`/`content_unit` sont ce qu'un exemplaire de ce
    conditionnement contient RÉELLEMENT, en unité physique — inconnus tant
    que l'utilisateur (ou une règle déterministe certifiée) ne les a pas
    fournis.
    """

    package_type: Optional[str] = None
    content_amount: Optional[float] = None
    content_unit: Optional[str] = None
    status: PackageStatus = PackageStatus.NOT_REQUIRED
    source: Provenance = Provenance.UNKNOWN

    @property
    def is_content_known(self) -> bool:
        return (
            self.status == PackageStatus.KNOWN
            and self.content_amount is not None
            and bool(self.content_unit)
        )


@dataclass(frozen=True)
class InventoryQuantity:
    """Quantité physique réellement disponible/mouvementée — TOUJOURS dans
    l'unité de base physique du produit (jamais un conditionnement)."""

    amount: float
    unit: str
    source: Provenance = Provenance.UNKNOWN


@dataclass(frozen=True)
class CommercialQuantity:
    """La façon dont l'utilisateur exprime l'offre — peut différer de
    `InventoryQuantity` en unité (ex: "200 TONNE" annoncé, "200000 KG" en
    interne) mais JAMAIS en substance : voir `NormalizedRepresentation`
    pour la version convertie, qui ne remplace jamais celle-ci."""

    amount: float
    unit: str
    source: Provenance = Provenance.UNKNOWN


@dataclass(frozen=True)
class Pricing:
    """Un prix n'existe jamais seul — `amount` sans `basis` explicite n'est
    pas exécutable (voir `PriceBasis`). `source`/`basis_source` sont
    suivis séparément : l'utilisateur peut avoir été explicite sur le
    MONTANT ("500") sans jamais préciser la BASE (par kg ? par sachet ?),
    et les deux ne doivent jamais être confondus dans leur fiabilité."""

    amount: float
    basis: Optional[PriceBasis]
    currency: str = "FCFA"
    source: Provenance = Provenance.UNKNOWN
    basis_source: Provenance = Provenance.UNKNOWN

    @property
    def is_basis_known(self) -> bool:
        return self.basis is not None and self.basis_source.is_execution_safe


@dataclass(frozen=True)
class NormalizedRepresentation:
    """Calcul interne UNIQUEMENT — jamais affiché à l'utilisateur comme
    formulation commerciale (voir Phase 16, confirmation). Dérivée d'une
    `CommercialQuantity`/`Pricing` par une conversion déterministe
    certifiée (`convert_commercial_quantity`/`convert_pricing_to_base_unit`
    ci-dessous), jamais devinée."""

    quantity_amount: Optional[float] = None
    quantity_unit: Optional[str] = None
    unit_price: Optional[float] = None
    unit_price_basis: Optional[PriceBasis] = None


@dataclass(frozen=True)
class CommercialOfferValidation:
    """Résultat de `validate_commercial_offer` — jamais un simple booléen :
    le mandat exige une liste structurée de ce qui manque/entre en conflit,
    et une question de clarification prête à poser plutôt qu'un message
    d'erreur générique."""

    status: str  # "VALID" | "INCOMPLETE" | "INVALID"
    missing_fields: Tuple[str, ...] = ()
    conflicts: Tuple[str, ...] = ()
    clarification_question: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        return self.status == "VALID"


def convertible_measurement_family(unit: Optional[str]) -> Optional[str]:
    """Famille de conversion déterministe pour `unit` (MASS/VOLUME), ou
    `None` si `unit` est une famille singleton (un conditionnement comme
    SAC/PANIER/SACHET, ou un dénombrement comme TETE/UNITE) — RÉUTILISE
    `domain/pricing_tiers.py::unit_family` (voir docstring de module), ne
    duplique aucune table de conversion.

    Une famille singleton signifie : AUCUNE conversion déterministe
    n'existe vers/depuis cette unité — la traiter comme un conditionnement
    à contenu inconnu plutôt que deviner un facteur, c'est exactement le
    rôle de `PackageDefinition`."""
    if not unit:
        return None
    fam = _unit_family(unit)
    return fam if fam in ("MASS", "VOLUME") else None


def convert_commercial_quantity_to_base_unit(
    quantity: CommercialQuantity, base_unit: str
) -> Optional[Tuple[float, float]]:
    """Convertit `quantity` (ex: 200 TONNE) vers `base_unit` (ex: KG) via une
    règle déterministe (même famille MASS/VOLUME uniquement — voir
    `convertible_measurement_family`). Retourne `(amount_converted, factor)`
    ou `None` si la conversion n'est PAS autorisée (familles différentes, ou
    l'une des deux est un conditionnement/dénombrement) — dans ce cas
    l'appelant NE DOIT PAS deviner, seulement refuser ou demander.

    `factor` est le multiplicateur appliqué à `quantity.amount` — un prix
    exprimé "par `quantity.unit`" doit être DIVISÉ par ce même facteur pour
    rester exprimé "par `base_unit`" (Phase 6 : la normalisation ne doit
    jamais désynchroniser prix et quantité)."""
    src_family = convertible_measurement_family(quantity.unit)
    dst_family = convertible_measurement_family(base_unit)
    if src_family is None or dst_family is None or src_family != dst_family:
        return None
    factor = _unit_factor(quantity.unit) / _unit_factor(base_unit)
    return quantity.amount * factor, factor


__all__ = [
    "PriceBasis",
    "Provenance",
    "PackageStatus",
    "PackageDefinition",
    "InventoryQuantity",
    "CommercialQuantity",
    "Pricing",
    "NormalizedRepresentation",
    "CommercialOfferValidation",
    "convertible_measurement_family",
    "convert_commercial_quantity_to_base_unit",
]
