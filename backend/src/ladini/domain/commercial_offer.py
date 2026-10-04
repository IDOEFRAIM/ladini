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
from typing import Any, Dict, List, Optional, Tuple

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

    `count` (mandat 2026-09-30, "MODELE CANONIQUE package_count/package_size/
    available_quantity") — COMBIEN d'exemplaires de ce conditionnement le
    producteur déclare avoir ("50 pots de 4 L" -> count=50). Concept
    DISTINCT du `package_count` déjà existant côté ACHETEUR
    (`interpreter/structured_action_contract.py::SET_PACKAGE_COUNT`,
    surfacé en état sous `action_package_count`) : celui-ci est "combien
    d'exemplaires d'un `PricingTier` déjà PUBLIÉ l'acheteur veut commander",
    calculé à la commande, jamais persisté sur l'offre elle-même — voir
    `domain/pricing_tiers.py::compute_line`/`requested_pack_count`. Nommer ce
    champ `count`, toujours qualifié par `PackageDefinition.` (jamais un
    `package_count` nu dans un dict plat), évite la collision par
    construction plutôt que par convention.

    `count` seul (sans `content_amount`) et `content_amount` seul (sans
    `count`) sont tous deux des états VALIDES ("50 pots" taille inconnue ;
    "pot de 4 L" nombre inconnu — voir invariant I6 du mandat) : aucune
    quantité totale ne doit alors être dérivée, voir
    `derive_available_quantity_from_package` plus bas, seul endroit où
    `count × content_amount` est calculé (mandat §5 : un seul endroit, pas
    dispersé).
    """

    package_type: Optional[str] = None
    content_amount: Optional[float] = None
    content_unit: Optional[str] = None
    status: PackageStatus = PackageStatus.NOT_REQUIRED
    source: Provenance = Provenance.UNKNOWN
    # `count` ajouté APRÈS `source`, volontairement en DERNIÈRE position —
    # des appelants existants construisent `PackageDefinition` par arguments
    # POSITIONNELS (ex: `tests/unit/test_pricing_persistence_services.py`,
    # `PackageDefinition("SACHET", 0.5, "LITRE", PackageStatus.KNOWN, ...)`)
    # ; l'insérer plus tôt aurait décalé silencieusement tous ces appels.
    count: Optional[int] = None

    @property
    def is_content_known(self) -> bool:
        # (2026-09-30) `> 0` ajouté explicitement pour l'invariant I2 du
        # mandat ("package_size > 0 lorsqu'il est présent") — ne change AUCUN
        # comportement existant : les deux call sites qui posent déjà
        # `status=KNOWN` (`commercial_offer_flow.py::
        # build_commercial_offer_from_sales_state`/`offer_from_package_tier`)
        # ne le font déjà QUE quand `content_amount > 0` est vérifié en amont
        # (le second via `PricingTier.quantity: Field(gt=0)`).
        return (
            self.status == PackageStatus.KNOWN
            and self.content_amount is not None
            and self.content_amount > 0
            and bool(self.content_unit)
        )

    @property
    def is_count_known(self) -> bool:
        """Invariant I1 du mandat ("package_count > 0 lorsqu'il est
        présent") appliqué OPÉRATIONNELLEMENT plutôt que par exception à la
        construction (cohérent avec `is_content_known` ci-dessus, qui ne
        valide pas non plus `content_amount` à la construction) : un
        `count` présent mais <= 0 n'est jamais "connu" — il ne peut donc
        jamais servir de base à `derive_available_quantity_from_package`,
        exactement comme s'il était absent. Voir les tests dédiés
        (`test_package_quantity_model.py`) qui verrouillent ce choix."""
        return self.count is not None and self.count > 0

    @property
    def can_derive_available_quantity(self) -> bool:
        """I9 : le calcul `count × content_amount` n'a de sens QUE si les
        DEUX facteurs (et l'unité du contenu) sont explicitement connus."""
        return self.is_count_known and self.is_content_known


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
    #: Pour `PER_BASE_UNIT` : l'unité PHYSIQUE à laquelle le prix se rapporte (« 500000 par
    #: TONNE » -> "TONNE"). `None` = l'unité de la quantité commerciale. Sans ce champ, un
    #: prix « par kg » sur une quantité annoncée en tonnes était indiscernable d'un prix
    #: « par tonne » (Phase B1, scénario `200 tonnes à 500 000`).
    basis_unit: Optional[str] = None

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


_CLARIFICATION_QUESTIONS: Dict[str, str] = {
    "quantity": "Quelle quantité proposez-vous ?",
    "price": "Quel est votre prix ?",
    "price_basis": (
        "Ce prix, c'est pour quoi exactement ? Par kilo (ou litre/tête), "
        "pour un conditionnement (sac, sachet, bidon...), ou pour tout le lot ?"
    ),
    "package_content_amount": (
        "Quelle quantité contient un exemplaire de ce conditionnement ? "
        "(ex: 0,5 litre, 50 kg...)"
    ),
}


def validate_commercial_offer(
    *,
    inventory_quantity: Optional[InventoryQuantity],
    pricing: Optional[Pricing],
    package: Optional[PackageDefinition] = None,
) -> CommercialOfferValidation:
    """Point d'entrée central (Phase 13 du mandat) — VALID/INCOMPLETE/INVALID,
    jamais un booléen. Règle centrale : `price.amount` seul n'est jamais
    exécutable (il lui faut un `price.basis` dont la provenance est
    `is_execution_safe`) ; `PriceBasis.PER_PACKAGE` sans `PackageDefinition`
    au contenu CONNU n'est jamais exécutable non plus — c'est exactement le
    scénario "500 F le sachet" de la mission.

    `missing_fields`/`conflicts` sont des listes STRUCTURÉES (jamais un
    message d'erreur générique) — chaque champ manquant de
    `missing_fields` a une question de clarification prête à poser
    (`_CLARIFICATION_QUESTIONS`), consommable directement par le mécanisme
    `ASK_MISSING_FIELD` déjà existant du pipeline conversationnel (voir
    `nodes/rendering/ask.py`).

    Conçu pour être appelé PAR un draft existant (`SalesPublishDraft`, ...)
    avant qu'il ne quitte l'état "collecte" pour "confirmation" — pas encore
    câblé dans le chemin de conversation live cette session (voir
    `docs/domain/COMMERCIAL_QUANTITY_PRICING_MODEL.md` §Phase B pour le plan
    de câblage), mais son contrat est stable et entièrement testé en
    isolation (`tests/unit/test_commercial_offer_validation.py`)."""
    missing: List[str] = []
    conflicts: List[str] = []

    if inventory_quantity is None:
        missing.append("quantity")
    elif inventory_quantity.amount <= 0:
        conflicts.append("quantity_not_positive")

    if pricing is None:
        missing.append("price")
    else:
        if pricing.amount <= 0:
            conflicts.append("price_amount_not_positive")
        if not pricing.is_basis_known:
            # `price.amount` seul, sans base fiable : NON EXÉCUTABLE (règle
            # centrale du mandat) — qu'elle soit totalement absente
            # (`basis is None`) ou posée par une provenance non fiable
            # (LLM_INFERRED/UNKNOWN) ne change rien à la conclusion.
            missing.append("price_basis")
        elif pricing.basis == PriceBasis.PER_PACKAGE:
            if package is None or not package.is_content_known:
                # PER_PACKAGE sans définition de contenu connue : NON
                # EXÉCUTABLE (règle centrale) — le scénario "500 F le
                # sachet" exact de la mission.
                missing.append("package_content_amount")

    if conflicts:
        return CommercialOfferValidation(
            status="INVALID",
            missing_fields=tuple(missing),
            conflicts=tuple(conflicts),
        )
    if missing:
        question = _CLARIFICATION_QUESTIONS.get(missing[0])
        return CommercialOfferValidation(
            status="INCOMPLETE",
            missing_fields=tuple(missing),
            clarification_question=question,
        )
    return CommercialOfferValidation(status="VALID")


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


def derive_available_quantity_from_package(
    package: Optional[PackageDefinition],
) -> Optional[InventoryQuantity]:
    """SEUL endroit du dépôt où `package_count × package_size` produit une
    quantité disponible (mandat 2026-09-30 §5 : un calcul, pas dispersé).

    Ferme l'incident réel : "j'ai 50 pot de 4 litre" doit produire une
    disponibilité de 200 L (50 × 4), jamais rester à "4 L" (le contenu d'UN
    pot) en perdant le "50", ni devenir "50 L" en perdant la taille — voir
    les cas canoniques A-E du mandat, verrouillés par
    `tests/unit/test_package_quantity_model.py`.

    Ne calcule QUE si `count`/`content_amount`/`content_unit` sont TOUS
    explicitement connus (`PackageDefinition.can_derive_available_quantity`)
    — I9 : "aucune estimation implicite". Un `PackageDefinition` partiel
    (compte seul, ou taille seule — I6) renvoie `None` : c'est à l'appelant
    de traiter ce `None` comme une disponibilité encore manquante, jamais
    comme zéro ni comme une valeur devinée.

    Aucune conversion d'unité n'intervient ici : `count` et `content_amount`
    portent déjà la MÊME unité physique (`content_unit`, celle du
    conditionnement tel que déclaré) — la quantité dérivée est directement
    exprimée dans cette unité. Une éventuelle réconciliation avec une
    disponibilité déjà connue dans une AUTRE unité est un problème de
    flow/parser, explicitement hors périmètre de cette étape (mandat §15)."""
    if package is None or not package.can_derive_available_quantity:
        return None
    assert package.count is not None and package.content_amount is not None
    assert package.content_unit is not None
    return InventoryQuantity(
        amount=float(package.count) * float(package.content_amount),
        unit=package.content_unit,
        source=Provenance.DOMAIN_DERIVED,
    )


# ═══════════════════════════════════════════════════════════════════════════
# PHASE B1 — AGRÉGAT `CommercialOffer` (vertical slice SALES_PUBLISH_PRODUCT)
# ═══════════════════════════════════════════════════════════════════════════
#
# La Phase A a livré les briques (quantité, prix, base, conditionnement,
# provenance, validateur). Il manquait l'OBJET qui les réunit et qui puisse être
# (a) reconstruit à chaque tour depuis l'état legacy, (b) sérialisé dans le draft
# versionné, (c) rendu tel que l'utilisateur l'a formulé, (d) converti en payload
# d'exécution de façon DÉTERMINISTE. Ce n'est PAS un second système : le
# `SalesPublishDraft` existant le porte, le `validator` existant l'appelle.

_COMMERCIAL_OFFER_SCHEMA_VERSION = 1

#: Unité de base physique retenue par famille convertible (convention existante du
#: dépôt : `actions/common.py::normalize_quantity_to_kg`, tonnes/grammes -> KG).
_BASE_UNIT_BY_FAMILY = {"MASS": "KG", "VOLUME": "LITRE"}

_UNIT_LABELS: Dict[str, Tuple[str, str]] = {
    "LITRE": ("litre", "litres"),
    "KG": ("kg", "kg"),
    "TONNE": ("tonne", "tonnes"),
    "TETE": ("tête", "têtes"),
    "SAC": ("sac", "sacs"),
    "PANIER": ("panier", "paniers"),
    "UNITE": ("unité", "unités"),
}


def unit_display(unit: Optional[str], amount: Optional[float] = None) -> str:
    """Libellé humain d'une unité (« 50 litres », « 1 litre », « 200 tonnes »)."""
    code = str(unit or "").strip().upper()
    singular, plural = _UNIT_LABELS.get(code, (code.lower(), code.lower()))
    if amount is not None and abs(float(amount)) > 1:
        return plural
    return singular


def _fmt(value: Optional[float]) -> str:
    from ladini.core.formatting import fmt_num

    return str(fmt_num(value))


def base_unit_for(unit: Optional[str]) -> Optional[str]:
    """Unité de base physique de `unit` : KG pour la masse, LITRE pour le volume, sinon
    l'unité elle-même (TETE, SAC, PANIER... comptés tels quels)."""
    if not unit:
        return None
    code = str(unit).strip().upper()
    family = convertible_measurement_family(code)
    if family:
        return _BASE_UNIT_BY_FAMILY[family]
    return code


@dataclass(frozen=True)
class CommercialOffer:
    """Ce qu'un producteur propose, avec le SENS de chaque nombre.

    `commercial_quantity` : la quantité comme dite (« 200 TONNE »).
    `inventory_quantity`  : la même quantité dans l'unité physique de base (« 200000 KG »).
    `pricing`/`package`   : le prix ET sa base ; le conditionnement et son contenu.
    `normalized`          : calcul interne uniquement, jamais la formulation affichée.
    """

    product: Optional[str] = None
    commercial_quantity: Optional[CommercialQuantity] = None
    inventory_quantity: Optional[InventoryQuantity] = None
    pricing: Optional[Pricing] = None
    package: Optional[PackageDefinition] = None
    normalized: Optional[NormalizedRepresentation] = None

    # -- validation ---------------------------------------------------------
    def validate(self) -> CommercialOfferValidation:
        return validate_offer(self)

    # -- (dé)sérialisation (JSON, sans migration : portée par le draft) -----
    def to_dict(self) -> Dict[str, Any]:
        cq, iq, pr, pk, nm = (
            self.commercial_quantity,
            self.inventory_quantity,
            self.pricing,
            self.package,
            self.normalized,
        )
        return {
            "schema": _COMMERCIAL_OFFER_SCHEMA_VERSION,
            "product": self.product,
            "commercial_quantity": (
                {"amount": cq.amount, "unit": cq.unit, "source": cq.source.value} if cq else None
            ),
            "inventory_quantity": (
                {"amount": iq.amount, "unit": iq.unit, "source": iq.source.value} if iq else None
            ),
            "pricing": (
                {
                    "amount": pr.amount,
                    "basis": pr.basis.value if pr.basis else None,
                    "basis_unit": pr.basis_unit,
                    "currency": pr.currency,
                    "source": pr.source.value,
                    "basis_source": pr.basis_source.value,
                }
                if pr
                else None
            ),
            "package": (
                {
                    "package_type": pk.package_type,
                    "content_amount": pk.content_amount,
                    "content_unit": pk.content_unit,
                    "count": pk.count,
                    "status": pk.status.value,
                    "source": pk.source.value,
                }
                if pk
                else None
            ),
            "normalized": (
                {
                    "quantity_amount": nm.quantity_amount,
                    "quantity_unit": nm.quantity_unit,
                    "unit_price": nm.unit_price,
                    "unit_price_basis": nm.unit_price_basis.value if nm.unit_price_basis else None,
                }
                if nm
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["CommercialOffer"]:
        if not isinstance(data, dict) or data.get("schema") != _COMMERCIAL_OFFER_SCHEMA_VERSION:
            return None

        def _p(value: Any) -> Provenance:
            try:
                return Provenance(value)
            except ValueError:
                return Provenance.UNKNOWN

        def _num(value: Any) -> Optional[float]:
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        def _int(value: Any) -> Optional[int]:
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        cq = data.get("commercial_quantity")
        iq = data.get("inventory_quantity")
        pr = data.get("pricing")
        pk = data.get("package")
        nm = data.get("normalized")
        try:
            return cls(
                product=data.get("product"),
                commercial_quantity=(
                    CommercialQuantity(float(cq["amount"]), str(cq["unit"]), _p(cq.get("source")))
                    if isinstance(cq, dict) and _num(cq.get("amount")) is not None
                    else None
                ),
                inventory_quantity=(
                    InventoryQuantity(float(iq["amount"]), str(iq["unit"]), _p(iq.get("source")))
                    if isinstance(iq, dict) and _num(iq.get("amount")) is not None
                    else None
                ),
                pricing=(
                    Pricing(
                        amount=float(pr["amount"]),
                        basis=PriceBasis(pr["basis"]) if pr.get("basis") else None,
                        currency=pr.get("currency") or "FCFA",
                        source=_p(pr.get("source")),
                        basis_source=_p(pr.get("basis_source")),
                        basis_unit=pr.get("basis_unit"),
                    )
                    if isinstance(pr, dict) and _num(pr.get("amount")) is not None
                    else None
                ),
                package=(
                    PackageDefinition(
                        package_type=pk.get("package_type"),
                        content_amount=_num(pk.get("content_amount")),
                        content_unit=pk.get("content_unit"),
                        count=_int(pk.get("count")),
                        status=PackageStatus(pk.get("status") or "NOT_REQUIRED"),
                        source=_p(pk.get("source")),
                    )
                    if isinstance(pk, dict)
                    else None
                ),
                normalized=(
                    NormalizedRepresentation(
                        quantity_amount=_num(nm.get("quantity_amount")),
                        quantity_unit=nm.get("quantity_unit"),
                        unit_price=_num(nm.get("unit_price")),
                        unit_price_basis=(
                            PriceBasis(nm["unit_price_basis"]) if nm.get("unit_price_basis") else None
                        ),
                    )
                    if isinstance(nm, dict)
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _content_in_inventory_unit(offer: "CommercialOffer") -> Optional[float]:
    """Contenu d'UN conditionnement exprimé dans l'unité de base de l'inventaire, ou `None`
    si le contenu est inconnu ou si son unité n'est pas convertible vers l'inventaire."""
    pkg, inv = offer.package, offer.inventory_quantity
    if pkg is None or not pkg.is_content_known or inv is None or pkg.content_amount is None:
        return None
    converted = convert_commercial_quantity_to_base_unit(
        CommercialQuantity(float(pkg.content_amount), str(pkg.content_unit)), inv.unit
    )
    if converted is not None:
        return converted[0]
    if str(pkg.content_unit).upper() == str(inv.unit).upper():
        return float(pkg.content_amount)
    return None


def derive_normalized(offer: "CommercialOffer") -> Optional[NormalizedRepresentation]:
    """Représentation INTERNE (prix par unité de base de l'inventaire) — calcul déterministe,
    jamais affiché comme formulation commerciale. `None` si l'offre n'est pas normalisable."""
    inv, pr = offer.inventory_quantity, offer.pricing
    if inv is None or pr is None or pr.basis is None:
        return None
    unit_price: Optional[float] = None
    if pr.basis == PriceBasis.PER_BASE_UNIT:
        basis_unit = pr.basis_unit or (
            offer.commercial_quantity.unit if offer.commercial_quantity else inv.unit
        )
        if str(basis_unit).upper() == str(inv.unit).upper():
            unit_price = pr.amount
        else:
            factor = convert_commercial_quantity_to_base_unit(
                CommercialQuantity(1.0, str(basis_unit)), inv.unit
            )
            if factor is not None and factor[0] > 0:
                unit_price = pr.amount / factor[0]
    elif pr.basis == PriceBasis.PER_PACKAGE:
        content = _content_in_inventory_unit(offer)
        if content:
            unit_price = pr.amount / content
    elif pr.basis == PriceBasis.TOTAL_LOT:
        if inv.amount > 0:
            unit_price = pr.amount / inv.amount
    if unit_price is None:
        return None
    return NormalizedRepresentation(
        quantity_amount=inv.amount,
        quantity_unit=inv.unit,
        unit_price=round(unit_price, 2),
        unit_price_basis=PriceBasis.PER_BASE_UNIT,
    )


def validate_offer(offer: "CommercialOffer") -> CommercialOfferValidation:
    """Validateur de l'AGRÉGAT — étend `validate_commercial_offer` (Phase A, inchangé) avec les
    incohérences qui n'existent qu'au niveau de l'offre complète :

    - base de prix `PER_BASE_UNIT` sur une unité incommensurable avec la quantité (prix « par
      SAC » sur du LITRE sans conditionnement défini) -> `price_basis` manquant, jamais un
      avertissement suivi d'une confirmation ;
    - contenu d'un conditionnement dans une unité incompatible avec l'inventaire
      (0,5 KG pour du lait au litre) -> conflit.
    """
    base = validate_commercial_offer(
        inventory_quantity=offer.inventory_quantity, pricing=offer.pricing, package=offer.package
    )
    if base.status != "VALID":
        return base

    pr, inv, pkg = offer.pricing, offer.inventory_quantity, offer.package
    assert pr is not None and inv is not None  # garanti par un statut VALID
    conflicts: List[str] = []
    missing: List[str] = []

    if pr.basis == PriceBasis.PER_BASE_UNIT and pr.basis_unit:
        same_unit = str(pr.basis_unit).upper() == str(inv.unit).upper()
        family_basis = convertible_measurement_family(pr.basis_unit)
        if not same_unit and (family_basis is None or family_basis != convertible_measurement_family(inv.unit)):
            missing.append("price_basis")
    if pr.basis == PriceBasis.PER_PACKAGE and pkg is not None and pkg.is_content_known:
        if _content_in_inventory_unit(offer) is None:
            conflicts.append("package_content_unit_incompatible_with_inventory")

    if conflicts:
        return CommercialOfferValidation(status="INVALID", conflicts=tuple(conflicts))
    if missing:
        return CommercialOfferValidation(
            status="INCOMPLETE",
            missing_fields=tuple(missing),
            clarification_question=_CLARIFICATION_QUESTIONS.get(missing[0]),
        )
    return base


def format_package_content(amount: Optional[float], unit: Optional[str]) -> str:
    """Contenu d'UN conditionnement dans l'unité la plus lisible : 0,5 litre -> « 500 ml », 2 -> « 2 litres »."""
    if amount is None:
        return ""
    u = str(unit or "").upper()
    if u == "LITRE" and amount < 1:
        return f"{_fmt(round(amount * 1000, 6))} ml"
    if u == "KG" and amount < 1:
        return f"{_fmt(round(amount * 1000, 6))} g"
    return f"{_fmt(amount)} {unit_display(u, amount)}"


def render_offer_summary(offer: "CommercialOffer") -> str:
    """Formulation COMMERCIALE de l'offre — celle que l'utilisateur a choisie, jamais la
    représentation normalisée interne (« 500 000 FCFA par tonne », pas « 500 FCFA/kg »)."""
    cq, pr, pk = offer.commercial_quantity, offer.pricing, offer.package
    if cq is None:
        return "Récapitulatif de la publication en cours de construction."
    head = f"Publication de {_fmt(cq.amount)} {unit_display(cq.unit, cq.amount)} de {offer.product}"
    if pr is None or pr.basis is None:
        return head
    amount = f"{_fmt(pr.amount)} {pr.currency}"
    if pr.basis == PriceBasis.PER_PACKAGE and pk is not None and pk.is_content_known:
        ptype = (pk.package_type or "conditionnement").lower()
        if pk.count:
            # Stock CONDITIONNÉ déclaré (« 100 sachets de 500 ml ») : la structure commerciale est conservée
            # (hotfix 2026-10-03) — jamais « 100 litres », jamais un prix par millilitre.
            plural = f"{ptype}s" if pk.count > 1 else ptype
            return (
                f"Publication de {pk.count} {plural} de {offer.product} de "
                f"{format_package_content(pk.content_amount, pk.content_unit)} à {amount} le {ptype}.\n"
                f"Quantité totale : {_fmt(cq.amount)} {unit_display(cq.unit, cq.amount)}."
            )
        return (
            f"{head} à {amount} par {ptype} de "
            f"{_fmt(pk.content_amount)} {unit_display(pk.content_unit, pk.content_amount)}."
        )
    if pr.basis == PriceBasis.TOTAL_LOT:
        return f"{head} pour {amount} au total (lot entier)."
    basis_unit = pr.basis_unit or cq.unit
    return f"{head} à {amount} par {unit_display(basis_unit)}."


def offer_execution_payload(offer: "CommercialOffer") -> Dict[str, Any]:
    """Payload MCP DÉRIVÉ de l'offre certifiée — le SEUL passage offre -> base de données.

    Mapping sur la structure EXISTANTE (aucune migration) :
      - `quantity`/`unit`  = inventaire en unité de base ;
      - `price`            = prix par unité de base (représentation normalisée) ;
      - `pricing_tiers`    = un palier `{quantity, unit, price, packaging}` UNIQUEMENT quand
                             le prix est PAR CONDITIONNEMENT (le JSONB `products.pricing_tiers`
                             porte alors « 500 FCFA / sachet de 0,5 L »).
    Aucun champ `original_*`/`*_display` : l'exécuteur n'a plus rien à renormaliser.
    Lève `ValueError` sur une offre non VALID — l'exécution ne devine jamais."""
    verdict = offer.validate()
    normalized = offer.normalized or derive_normalized(offer)
    if not verdict.is_valid or normalized is None or normalized.unit_price is None:
        raise ValueError(
            f"offre non exécutable: {verdict.status} {verdict.missing_fields}{verdict.conflicts}"
        )
    inv, pr, pk = offer.inventory_quantity, offer.pricing, offer.package
    assert inv is not None and pr is not None
    payload: Dict[str, Any] = {
        "product": offer.product,
        "quantity": inv.amount,
        "unit": inv.unit,
        "price": normalized.unit_price,
    }
    if pr.basis == PriceBasis.PER_PACKAGE and pk is not None and pk.is_content_known:
        tier: Dict[str, Any] = {
            "quantity": pk.content_amount,
            "unit": pk.content_unit,
            "price": pr.amount,
            "packaging": (pk.package_type or "").lower() or None,
        }
        if pk.count and tier["packaging"]:
            # B16 : « 100 sachets de 500 ml » — le COMPTE est l'inventaire de cette variante (Σ == stock publié).
            tier["available_count"] = int(pk.count)
        payload["pricing_tiers"] = [tier]
    return payload


__all__ = [
    "format_package_content",
    "CommercialOffer",
    "unit_display",
    "base_unit_for",
    "derive_normalized",
    "validate_offer",
    "render_offer_summary",
    "offer_execution_payload",
    "PriceBasis",
    "Provenance",
    "PackageStatus",
    "PackageDefinition",
    "InventoryQuantity",
    "CommercialQuantity",
    "Pricing",
    "NormalizedRepresentation",
    "CommercialOfferValidation",
    "validate_commercial_offer",
    "convertible_measurement_family",
    "convert_commercial_quantity_to_base_unit",
    "derive_available_quantity_from_package",
]
