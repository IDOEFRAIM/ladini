"""Structured selection-action contract — producer / pricing-tier / package-count.

Refonte 2026-09-01 (directive explicite utilisateur, "LLM pour l'interprétation,
code pour l'exécution"). Avant ce module, le tunnel `BUYER_ADD_TO_CART`
producteur → palier → nombre de paquets était résolu par PLUSIEURS parseurs
concurrents qui se disputaient le même message :

  - le fast-path générique "chiffre nu → `selection_index`" (routing.py,
    ancienne règle "Fast-path 1"), qui ne sait pas DE QUOI l'index est
    l'index (producteur ? palier ?) ;
  - la règle LLM 4bis "sélection de palier", qui produit `selected_value` —
    un canal DIFFÉRENT du précédent pour la MÊME notion de choix ;
  - le fallback regex quantité/unité (`_fallback_quantity_unit_from_text`),
    qui peut extraire un nombre+unité de la MÊME phrase qu'une sélection de
    palier (ex: "le premier, c'est-à-dire 5 L" contient à la fois un ordinal
    ET "5 L", qui décrit le CONDITIONNEMENT, pas une quantité) ;
  - `enrich_payload_from_text` (memory.py), un second passage regex
    entièrement indépendant du premier ;
  - `cart.py`, qui devait ensuite ARBITRER après coup entre ces signaux
    potentiellement contradictoires via des heuristiques de purge
    (`_tier_freshly_resolved`, `_tier_selection_pending`...).

Incident réel confirmé (audit 2026-09-01) : "LE PREMIER, C'EST À DIRE 5 L"
faisait cohabiter `selection_index=1` (résolu correctement en palier "5 L")
ET `quantity=5.0, unit=LITRE` (le fallback regex captant le même "5 L" comme
une quantité) DANS LE MÊME TOUR — la heuristique de purge de `cart.py`, qui
ne purge QUE si aucune quantité n'a été donnée "ce tour", faisait alors
confiance à ce 5.0 comme un nombre de paquets déjà répondu, produisant un
refus absurde ("5 litre, c'est une quantité totale...") au lieu d'enchaîner
sur "combien de bidons de 5 L voulez-vous ?".

Ce module est la source UNIQUE de vérité pour ce tunnel : UN SEUL type
d'action structurée, UNE SEULE fonction de construction du contexte candidat,
UNE SEULE fonction de validation. Le LLM (routing.py) et le FastPath
déterministe produisent tous deux CE MÊME contrat — jamais une interprétation
concurrente. `cart.py` ne fait plus que consommer une `SelectionAction` déjà
validée ; il n'arbitre plus rien après coup.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class ActionType(str, Enum):
    """Les SEULES actions structurées que ce tunnel reconnaît.

    `SET_QUANTITY` couvre le produit HISTORIQUE sans `pricing_tiers` (un seul
    prix, une seule unité) — voir règle 13 du cahier des charges : le flow
    historique doit rester intact, il a donc sa propre action plutôt que
    d'être artificiellement plié dans `SET_PACKAGE_COUNT`.
    """

    SELECT_PRODUCER = "SELECT_PRODUCER"
    SELECT_PRICING_TIER = "SELECT_PRICING_TIER"
    SET_PACKAGE_COUNT = "SET_PACKAGE_COUNT"
    SET_QUANTITY = "SET_QUANTITY"


class SelectionAction(BaseModel):
    """Une action structurée déjà VALIDÉE contre le contexte courant.

    N'est JAMAIS construite directement depuis une sortie LLM/FastPath brute
    — seule `validate_action` en produit une instance, après vérification que
    l'id référencé appartient bien aux options ACTIVES. `frozen=True` : une
    fois validée, elle n'est plus modifiable — elle traverse `cart.py`
    telle quelle jusqu'à l'exécution.
    """

    action: ActionType
    producer_id: Optional[str] = None
    #: Identité EXACTE de l'offre choisie (SELECT_PRODUCER) — `producer_id` seul
    #: n'identifie PAS une offre : un même producteur peut en avoir plusieurs
    #: (produits/prix/stocks différents). Voir `vendor_offer_id`.
    offer_id: Optional[str] = None
    pricing_tier_id: Optional[str] = None
    package_count: Optional[float] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None

    model_config = {"frozen": True}


class ProducerOption(BaseModel):
    producer_id: str = ""
    #: Rang (1-based) de cette option dans le menu AFFICHÉ — égal à la position
    #: dans `vendor_selection_context.vendors`, jamais recalculé.
    display_index: int = 0
    #: Identité de l'OFFRE affichée à cet index (jamais dérivée du seul
    #: producteur). Unique dans un menu. Les contextes RÉELS (`build_selection_context`)
    #: la renseignent toujours ; le défaut vide n'existe que pour les contextes
    #: construits à la main (voir `SelectionContext._normalize_legacy_options`).
    offer_id: str = ""
    label: str


class TierOption(BaseModel):
    tier_id: str
    label: str


class SelectionContext(BaseModel):
    """Ce que le tunnel attend MAINTENANT — et RIEN d'autre.

    Contrairement à `expected_candidates`/`available_mapping` (canal
    générique partagé par tout l'agent — commandes, enchères, désambiguïsation
    catalogue...), ce contexte est reconstruit À CHAQUE tour directement
    depuis `vendor_selection_context`/`tier_selection_context` — jamais un
    canal qui peut rester périmé d'un tour à l'autre (voir l'incident
    documenté en tête de module : `expected_candidates` gardait la liste
    PRODUCTEUR alors que le menu de paliers était déjà affiché).
    """

    expected_action: Optional[ActionType] = None
    producer_options: List[ProducerOption] = Field(default_factory=list)
    tier_options: List[TierOption] = Field(default_factory=list)
    # Palier déjà choisi (survit pour permettre une re-sélection explicite —
    # voir domain/pricing_tiers.py::pending_pack_count_tier, même notion).
    active_tier_id: Optional[str] = None

    model_config = {"frozen": True}

    @model_validator(mode="after")
    def _normalize_legacy_options(self) -> "SelectionContext":
        """Contexte construit à la main sans identité d'offre : rang = position,
        offre = producteur (seul repli possible, non ambigu tant que chaque
        producteur n'apparaît qu'une fois — `validate_action` refuse sinon)."""
        for position, option in enumerate(self.producer_options, start=1):
            if not option.display_index:
                option.display_index = position
            if not option.offer_id:
                option.offer_id = option.producer_id or f"option#{position}"
        return self

    def producer_ids(self) -> set:
        return {p.producer_id for p in self.producer_options}

    def offer_ids(self) -> set:
        return {p.offer_id for p in self.producer_options}

    def offer_for(self, offer_id: str) -> Optional["ProducerOption"]:
        return next((p for p in self.producer_options if p.offer_id == offer_id), None)

    def tier_ids(self) -> set:
        return {t.tier_id for t in self.tier_options}


def _live_ctx(ctx: Any) -> Optional[Dict[str, Any]]:
    if isinstance(ctx, dict) and ctx and not ctx.get("__reset__"):
        return ctx
    return None


def vendor_offer_id(vendor: Dict[str, Any], position: int) -> str:
    """Identité de l'OFFRE d'une ligne du menu vendeur.

    Incident réel (2026-09-28) : le menu « Producteurs disponibles » liste des
    OFFRES (un produit d'un producteur), pas des producteurs. Deux lignes du même
    producteur (`Gilbert-prod` 450000/stock 20 et 461000/stock 461000) ont le même
    `producer_id` : résoudre « 3 » par `producer_id` retombait sur la PREMIÈRE
    offre de ce producteur (la n°2). L'identité minimale d'une offre est son
    `product_id` (pour une production future, l'id du MarketOffer, voir
    `resolve_product_vendors`). Repli positionnel stable uniquement si le catalogue
    n'a renvoyé aucun id (jamais deux offres avec la même clé)."""
    explicit = vendor.get("offer_id") or vendor.get("product_id")
    if explicit:
        return str(explicit)
    return f"{vendor.get('producer_id') or 'unknown'}#{position}"


def stamp_offer_identity(vendors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copie des lignes du menu avec `offer_id` et `display_index` (1-based)
    figés — le snapshot ne dépend plus jamais de l'ordre recalculé d'une
    recherche ultérieure."""
    stamped: List[Dict[str, Any]] = []
    for position, v in enumerate(vendors, start=1):
        row = dict(v)
        row["offer_id"] = vendor_offer_id(v, position)
        row["display_index"] = position
        stamped.append(row)
    return stamped


def build_selection_context(state: Dict[str, Any]) -> SelectionContext:
    """Reconstruit le contexte candidat ACTUEL depuis l'état — jamais depuis
    un canal générique susceptible d'être périmé.

    Ordre de priorité (miroir de la state machine réelle du tunnel) :
      1. Un palier est déjà résolu (`resolved_tier_id`) et on attend le
         nombre de paquets → `SET_PACKAGE_COUNT`, la liste des paliers reste
         exposée pour permettre un changement d'avis explicite.
      2. Un vendeur est choisi et des paliers actifs existent, aucun n'est
         encore résolu → `SELECT_PRICING_TIER`.
      3. Un vendeur est choisi, le produit n'a PAS de paliers → `SET_QUANTITY`
         (flow historique).
      4. Aucun vendeur choisi mais une liste de producteurs est active →
         `SELECT_PRODUCER`.

    Délibérément PAS de garde sur `expected_input` : ce champ est recalculé
    par `validator` (missing-fields) APRÈS l'interprétation, sur des règles
    qui ne connaissent rien du contrat d'action structurée — il retombe à
    `NONE` dès qu'aucun champ scalaire classique (product/quantity/price)
    n'est manquant, y compris pendant un tour où `agent_action` vient
    justement de répondre à la question. La présence structurelle de
    `vendor_selection_context`/`tier_selection_context` (posée EXCLUSIVEMENT
    par ce tunnel, voir flows/buyer/cart.py) est un signal strictement plus
    fiable et ne dépend d'aucun autre nœud du graphe.
    """
    vendor_ctx = _live_ctx(state.get("vendor_selection_context"))
    tier_ctx = _live_ctx(state.get("tier_selection_context"))

    chosen_vendor = None
    if vendor_ctx:
        cv = vendor_ctx.get("chosen_vendor")
        if isinstance(cv, dict):
            chosen_vendor = cv

    # --- Paliers actifs pour LE vendeur choisi (jamais un autre produit) ---
    tier_options: List[TierOption] = []
    active_tier_id: Optional[str] = None
    raw_tiers: List[Any] = []
    if tier_ctx and isinstance(tier_ctx.get("tiers"), list):
        raw_tiers = tier_ctx["tiers"]
        active_tier_id = tier_ctx.get("resolved_tier_id")
    elif chosen_vendor is not None:
        cv_tiers = chosen_vendor.get("pricing_tiers")
        if isinstance(cv_tiers, list):
            raw_tiers = cv_tiers
        if vendor_ctx:
            active_tier_id = vendor_ctx.get("resolved_tier_id") or active_tier_id

    for t in raw_tiers:
        if not isinstance(t, dict) or not t.get("tier_id"):
            continue
        label = f"{t.get('quantity')} {t.get('unit')}"
        if t.get("packaging"):
            label += f" ({t['packaging']})"
        label += f" — {t.get('price')} FCFA"
        tier_options.append(TierOption(tier_id=str(t["tier_id"]), label=label))

    if active_tier_id and any(t.tier_id == active_tier_id for t in tier_options):
        return SelectionContext(
            expected_action=ActionType.SET_PACKAGE_COUNT,
            tier_options=tier_options,
            active_tier_id=active_tier_id,
        )

    if tier_options:
        return SelectionContext(
            expected_action=ActionType.SELECT_PRICING_TIER,
            tier_options=tier_options,
        )

    if chosen_vendor is not None:
        # Vendeur résolu, produit SANS palier → flow historique quantité.
        return SelectionContext(expected_action=ActionType.SET_QUANTITY)

    # --- Aucun vendeur choisi encore : liste producteurs, si active ---
    if vendor_ctx and isinstance(vendor_ctx.get("vendors"), list):
        producer_options = []
        # Aucune ligne n'est écartée : `display_index` == index affiché dans le
        # menu (un filtre sur `producer_id` décalait les index suivants).
        for position, v in enumerate(vendor_ctx["vendors"], start=1):
            if not isinstance(v, dict):
                continue
            label = f"{v.get('vendor_name') or 'producteur'} — {v.get('price')} FCFA/{v.get('unit')}"
            producer_options.append(
                ProducerOption(
                    producer_id=str(v.get("producer_id") or ""),
                    display_index=position,
                    offer_id=vendor_offer_id(v, position),
                    label=label,
                )
            )
        if len(producer_options) > 1:
            return SelectionContext(
                expected_action=ActionType.SELECT_PRODUCER,
                producer_options=producer_options,
            )

    return SelectionContext()


def parse_raw_action(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Lit le contrat structuré brut (`agent_action` + son id/valeur) depuis
    un `extracted_entities`/`transaction_payload` fusionné. Ne valide RIEN —
    juste une lecture de forme. `None` si aucune action structurée n'est
    présente (le tunnel retombe alors sur ses gardes historiques, voir
    cart.py — compatibilité descendante pour le mode LLM indisponible)."""
    raw = str(payload.get("agent_action") or "").strip().upper()
    if not raw:
        return None
    try:
        action = ActionType(raw)
    except ValueError:
        return None
    return {
        "action": action,
        "producer_id": payload.get("action_producer_id"),
        "offer_id": payload.get("action_offer_id"),
        "pricing_tier_id": payload.get("action_pricing_tier_id"),
        "package_count": payload.get("action_package_count"),
        "quantity": payload.get("action_quantity"),
        "unit": payload.get("action_unit"),
    }


def validate_action(
    raw: Dict[str, Any], context: SelectionContext
) -> Optional[SelectionAction]:
    """LA seule porte de validation. Ne fait JAMAIS confiance à l'action
    proposée sans la vérifier contre le contexte ACTUEL :

      - le TYPE d'action doit correspondre à ce que le tunnel attend
        MAINTENANT (`context.expected_action`) — SAUF le cas explicitement
        autorisé de re-sélection d'un palier pendant `SET_PACKAGE_COUNT`
        (règle 14 du cahier des charges) ;
      - tout id référencé doit appartenir aux options listées dans
        `context` — jamais un id halluciné, jamais un id d'un AUTRE menu.

    Retourne `None` si l'action est invalide dans ce contexte — le code
    appelant ne modifie alors RIEN (règle 10 : "ne rien modifier" en cas de
    rejet)."""
    action = raw.get("action")
    if action is None or context.expected_action is None:
        return None

    if action == ActionType.SELECT_PRODUCER:
        if context.expected_action != ActionType.SELECT_PRODUCER:
            return None
        oid = raw.get("offer_id")
        if oid:
            option = context.offer_for(str(oid))
            if option is None:
                return None
            return SelectionAction(
                action=action, offer_id=option.offer_id, producer_id=option.producer_id
            )
        # Contrat historique (producer_id seul) : accepté UNIQUEMENT si non
        # ambigu — un producteur à plusieurs offres dans ce menu ne désigne PAS
        # une offre. On ne devine jamais (clarifier, jamais deviner-et-écrire).
        pid = raw.get("producer_id")
        if not pid:
            return None
        matches = [o for o in context.producer_options if o.producer_id == str(pid)]
        if len(matches) != 1:
            return None
        return SelectionAction(
            action=action, offer_id=matches[0].offer_id, producer_id=matches[0].producer_id
        )

    if action == ActionType.SELECT_PRICING_TIER:
        # Autorisé pendant SELECT_PRICING_TIER (choix initial) ET pendant
        # SET_PACKAGE_COUNT (changement d'avis explicite, règle 14) — jamais
        # pendant SELECT_PRODUCER ou SET_QUANTITY.
        if context.expected_action not in (
            ActionType.SELECT_PRICING_TIER,
            ActionType.SET_PACKAGE_COUNT,
        ):
            return None
        tid = raw.get("pricing_tier_id")
        if not tid or str(tid) not in context.tier_ids():
            return None
        return SelectionAction(action=action, pricing_tier_id=str(tid))

    if action == ActionType.SET_PACKAGE_COUNT:
        if context.expected_action != ActionType.SET_PACKAGE_COUNT:
            return None
        count = raw.get("package_count")
        try:
            count_f = float(count)
        except (TypeError, ValueError):
            return None
        if count_f <= 0:
            return None
        return SelectionAction(action=action, package_count=count_f)

    if action == ActionType.SET_QUANTITY:
        if context.expected_action != ActionType.SET_QUANTITY:
            return None
        qty = raw.get("quantity")
        try:
            qty_f = float(qty)
        except (TypeError, ValueError):
            return None
        if qty_f <= 0:
            return None
        unit = raw.get("unit")
        return SelectionAction(
            action=action, quantity=qty_f, unit=str(unit) if unit else None
        )

    return None


def fast_path_action(
    text: str, context: SelectionContext
) -> Optional[Dict[str, Any]]:
    """Raccourci déterministe : un chiffre NU (rien d'autre dans le message)
    produit directement le contrat structuré — SANS passer par le LLM.

    Volontairement minimal (règle 16 : le FastPath ne doit pas posséder sa
    propre logique métier concurrente du LLM) : seul le cas non-ambigu à
    100 % (chiffre nu, rien d'autre) est traité ici. Toute formulation
    textuelle ("le premier", "le bidon de 5 L"...) est laissée au LLM, qui
    reçoit exactement le même `context` et produit le même contrat."""
    clean = text.strip()
    if not clean.isdigit() or context.expected_action is None:
        return None
    idx = int(clean) - 1

    if context.expected_action == ActionType.SELECT_PRODUCER:
        option = next(
            (o for o in context.producer_options if o.display_index == idx + 1), None
        )
        if option is not None:
            return {
                "action": ActionType.SELECT_PRODUCER,
                "offer_id": option.offer_id,
                "producer_id": option.producer_id,
            }
        return None

    if context.expected_action == ActionType.SELECT_PRICING_TIER:
        if 0 <= idx < len(context.tier_options):
            return {
                "action": ActionType.SELECT_PRICING_TIER,
                "pricing_tier_id": context.tier_options[idx].tier_id,
            }
        return None

    if context.expected_action == ActionType.SET_PACKAGE_COUNT:
        # Un chiffre nu ICI est un nombre de paquets, jamais un index de menu
        # (règle 15 : le même "2" change de sens selon l'état, pas de
        # heuristique locale — c'est `context.expected_action`, construit une
        # fois pour toutes par `build_selection_context`, qui tranche).
        return {"action": ActionType.SET_PACKAGE_COUNT, "package_count": float(clean)}

    return None


def tier_menu_prompt_block(context: SelectionContext) -> str:
    """Bloc de contexte injecté dans le prompt LLM (routing.py) — décrit
    EXACTEMENT ce que le tunnel attend, avec les VRAIS ids, et rien d'autre.
    Remplace l'ancien `tier_menu_context` texte libre par un format qui colle
    au contrat de sortie structuré."""
    if context.expected_action == ActionType.SELECT_PRODUCER:
        lines = "\n".join(
            f'  producer_id="{p.producer_id}" : {p.label}'
            for p in context.producer_options
        )
        return (
            "ACTION ATTENDUE : SELECT_PRODUCER\n"
            f"PRODUCTEURS DISPONIBLES :\n{lines}"
        )
    if context.expected_action == ActionType.SELECT_PRICING_TIER:
        lines = "\n".join(
            f'  pricing_tier_id="{t.tier_id}" : {t.label}'
            for t in context.tier_options
        )
        return (
            "ACTION ATTENDUE : SELECT_PRICING_TIER\n"
            f"CONDITIONNEMENTS DISPONIBLES :\n{lines}"
        )
    if context.expected_action == ActionType.SET_PACKAGE_COUNT:
        lines = "\n".join(
            f'  pricing_tier_id="{t.tier_id}" : {t.label}'
            for t in context.tier_options
        )
        return (
            "ACTION ATTENDUE : SET_PACKAGE_COUNT (nombre de paquets du "
            f"conditionnement déjà choisi : \"{context.active_tier_id}\")\n"
            f"Conditionnements de ce produit (pour une RE-SÉLECTION explicite "
            f"seulement) :\n{lines}"
        )
    if context.expected_action == ActionType.SET_QUANTITY:
        return "ACTION ATTENDUE : SET_QUANTITY (produit à tarif unique, sans conditionnement)"
    return ""


__all__ = [
    "ActionType",
    "SelectionAction",
    "ProducerOption",
    "TierOption",
    "SelectionContext",
    "build_selection_context",
    "vendor_offer_id",
    "stamp_offer_identity",
    "parse_raw_action",
    "validate_action",
    "fast_path_action",
    "tier_menu_prompt_block",
]
