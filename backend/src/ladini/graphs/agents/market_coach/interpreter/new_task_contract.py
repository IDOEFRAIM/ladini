"""Contrat Pydantic du micro-prompt NEW_TASK (chantier "State Router +
micro-prompts", Incrément F, 2026-09-13).

`NewTaskInterpretation` est volontairement SPÉCIALISÉ — miroir structurel de
`structured_action_contract.py::StructuredActionDecision` — jamais le gros
contrat historique à 23 clés universelles (`agent_action`/`action_*`/
`selection_index`/`selected_value`/`movement_type`/`reason` : tous morts ici,
possédés par STRUCTURED_ACTION/SELECTION ou par des intentions dépréciées —
voir la matrice d'audit du rapport F). `extra="forbid"` (ici ET sur
`NewTaskEntities`) rend structurellement impossible qu'un ID technique ou un
champ d'une autre route se glisse silencieusement dans la sortie — pas
seulement une convention de prompt.

`interpreted_event` n'est PLUS un choix libre à 9 valeurs (ANSWER/UPDATE/
SELECTION/INTERRUPTION en moins — possédés par ACTIVE_SLOT/SELECTION/
STRUCTURED_ACTION désormais) : `NewTaskDisposition` n'en garde que 5, les
seules qu'une classification "aucun tunnel actif" peut réellement produire
(voir `state_router.py::choose_interpretation_route`, route NEW_TASK)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class NewTaskDisposition(str, Enum):
    #: Nouvelle demande métier — `intent` est alors OBLIGATOIRE, une valeur
    #: du catalogue fourni (jamais une chaîne inventée hors catalogue — voir
    #: le model_validator plus bas, qui revérifie contre le catalogue REÇU
    #: par CE prompt, pas contre INTENT_CONFIG dans l'absolu).
    NEW_TASK = "NEW_TASK"
    #: Accord/validation explicite — vocabulaire fermé déjà couvert par le
    #: fast-path déterministe (`routing.py::_CONFIRM_EXACT_PHRASES`) pour la
    #: majorité des cas ; ce micro-prompt ne voit que les formulations
    #: libres restantes (ex: pendant un panier acheteur en attente de
    #: validation — voir `NewTaskPromptContext.cart_pending`).
    CONFIRM = "CONFIRM"
    #: Refus/abandon explicite — même remarque que CONFIRM.
    REJECT = "REJECT"
    #: Message hors du domaine agricole/marketplace (politique, salutation
    #: vide, spam...).
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    #: Impossible à classer de façon fiable — sortie VALIDE, jamais forcée
    #: vers une intention juste pour produire quelque chose (spec §23).
    UNKNOWN = "UNKNOWN"
    #: (2026-10-01, Étape 9A/9B — ambiguïté hors tunnel) : des FAITS métier
    #: clairs (produit/quantité/...) sont compris, mais AUCUN signal d'action
    #: explicite ne départage plusieurs intentions du catalogue également
    #: compatibles avec ces mêmes faits (ex: "j'ai 90 L de miel" — vendre ?
    #: enregistrer en stock ? les deux lisent product+quantity de façon
    #: identique). Distinct d'UNKNOWN : ici les faits SONT compris, seule
    #: l'action reste à choisir — `entities` doit donc être préservé (jamais
    #: vidé comme pour UNKNOWN/CONFIRM/REJECT/OUT_OF_SCOPE), et
    #: `candidate_goals` (≥2 intentions du catalogue fourni) porte les choix
    #: plausibles. Ne JAMAIS deviner une seule intention "la plus probable"
    #: ici — c'est exactement le biais que cette disposition existe pour
    #: éliminer (mandat §9 : "pas de simple max confidence wins").
    AMBIGUOUS = "AMBIGUOUS"


class NewTaskPricingTier(BaseModel):
    """Une déclinaison tarifaire parmi plusieurs pour le MÊME produit — pas
    un menu de sélection (celui-là appartient à STRUCTURED_ACTION), une
    déclaration multi-tarifs faite par un producteur en une fois (règle
    5bis historique : "500f le demi-litre en sachet et 600f le bidon")."""

    model_config = {"extra": "forbid"}

    quantity: Optional[float] = None
    unit: Optional[str] = None
    price: Optional[float] = None
    packaging: Optional[str] = None


class NewTaskRecurringItem(BaseModel):
    """Un produit additionnel PORTANT SA PROPRE quantité/unité, pour une demande de
    besoin récurrent visant plusieurs produits à la fois en un seul message (ex:
    "10 kg de tomate et 20 kg d'oignon tous les jours sauf dimanche") — modèle sur
    `NewTaskPricingTier` ci-dessus : `additional_products` (liste de noms nus, plus
    bas) ne porte aucune quantité individuelle, insuffisant pour construire un
    `RecurringNeedDraft` par produit (bug réel confirmé 2026-09-23 : l'oignon
    disparaissait silencieusement, `flows/buyer/recurring_need.py::_create_flow` ne
    lisant que `product`/`quantity`/`unit`, seul le draft tomate était créé, sans
    aucun avertissement)."""

    model_config = {"extra": "forbid"}

    product: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None


class NewTaskAmbiguousGroup(BaseModel):
    """UNE quantité donnée pour PLUSIEURS noms de produits SANS répartition explicite (ex:
    "57 moutons chèvres" — deux animaux, un seul nombre, aucun mot indiquant un total ou une
    quantité par produit). Bug réel confirmé 2026-09-24 : le micro-prompt, sans ce champ, n'avait
    d'autre choix que d'inventer une répartition (product="mouton"/"chèvre" à 57 chacun) ou de
    fusionner en un produit incohérent ("moutons chevres") — jamais fiable dans les deux cas
    (spec §4 : "ne jamais inventer une information absente"). `candidates` porte les noms tels
    quels, `flows/buyer/recurring_need.py` construit la question de clarification, JAMAIS ce
    micro-prompt lui-même."""

    model_config = {"extra": "forbid"}

    quantity: Optional[float] = None
    unit: Optional[str] = None
    candidates: List[str] = []


class NewTaskOrphanQuantity(BaseModel):
    """UNE quantité mentionnée dans le message SANS AUCUN nom de produit qui lui soit
    rattaché — ni le produit principal, ni un candidat pour `ambiguous_groups` (ci-dessus, qui
    suppose au moins DEUX noms candidats). Ex: "150 kg tomates et 200 kg chaque semaine" : le
    second "200 kg" ne nomme aucun produit du tout. Distinct de `additional_items` (produit
    connu + sa propre quantité, plus haut) : ici, c'est ZÉRO nom, jamais un produit deviné.

    Bug réel production (2026-09-26, "150 kg tomate et 200 kg chaque semaine") : avant ce
    champ, le LLM n'avait NULLE PART où mettre cette quantité (`additional_items` exige un
    `product`, `ambiguous_groups` exige ≥2 `candidates`) — elle était donc soit silencieusement
    perdue, soit (pire, le bug réellement observé) refusionnée dans la quantité du produit déjà
    connu par une garde Python anti-troncature (`new_task_micro.py::_finalize`, pensée pour "2
    tonnes et 250 kg" — UNE SEULE quantité fragmentée en plusieurs nombres, jamais deux
    quantités DISTINCTES) qui ne savait pas faire la différence entre les deux cas :
    150+200=350 de tomates, jamais dit par l'utilisateur. `flows/buyer/recurring_need.py`
    construit la question de clarification ("à quel produit correspondent les 200 KG ?"),
    JAMAIS ce micro-prompt lui-même — même principe que `NewTaskAmbiguousGroup`."""

    model_config = {"extra": "forbid"}

    quantity: Optional[float] = None
    unit: Optional[str] = None


class NewTaskEntities(BaseModel):
    """Uniquement des informations DÉJÀ DITES dans CE message — jamais un ID
    technique (spec §11), jamais un champ possédé par une autre route (spec
    §17/18/19/20 : pas de `agent_action`, pas de `selection_index`, pas de
    `movement_type`/`reason` — intentions STOCK_* toutes dépréciées, voir
    audit). `extra="forbid"` : le LLM ne peut pas en injecter un malgré
    tout, même silencieusement."""

    model_config = {"extra": "forbid"}

    product: Optional[str] = None
    additional_products: List[str] = []
    quantity: Optional[float] = None
    unit: Optional[str] = None
    price: Optional[float] = None
    price_unit: Optional[str] = None
    # Phase B2b : SUGGESTIONS du LLM sur la sémantique d'un prix (bids, ventes). Jamais autoritaires :
    # le DOMAINE (`domain/bid_pricing_flow.py`, `domain/commercial_offer_flow.py`) lit la base dans le texte
    # ou dans la question posée et les journalise seulement. Volontairement absents du prompt (coût de
    # tokens, déterminisme) : acceptés s'ils sont émis, jamais demandés.
    price_basis: Optional[str] = None
    package_type: Optional[str] = None
    package_content_amount: Optional[float] = None
    package_content_unit: Optional[str] = None
    pricing_tiers: List[NewTaskPricingTier] = []
    estimated_available_at: Optional[str] = None
    expected_harvest_date: Optional[str] = None
    deadline: Optional[str] = None
    zone: Optional[str] = None
    farm_name: Optional[str] = None
    # CREATE_RECURRING_NEED (spec Phase 2 §3, `intent.py::INTENT_CONFIG` —
    # `recurrence_type` y est un champ `required`, `weekly_days`/
    # `excluded_weekdays`/`max_price_per_unit` sont dans son `label_map`) :
    # absents d'ici avant ce correctif (2026-09-23, bug réel confirmé —
    # "20 kg de tomate tous les jours sauf les dimanches" retombait sur
    # BUYER_REQUEST, `extra="forbid"` rendant IMPOSSIBLE toute extraction de
    # récurrence par ce micro-prompt). Voir `new_task_prompts.py` pour les
    # règles d'extraction correspondantes.
    recurrence_type: Optional[str] = None
    weekly_days: List[int] = []
    excluded_weekdays: List[int] = []
    max_price_per_unit: Optional[float] = None
    # Chantier multi-produits CREATE_RECURRING_NEED (2026-09-23, suite du correctif
    # state-leak ci-dessus) : UN item structuré {product, quantity, unit} par
    # produit additionnel, PARTAGEANT la même récurrence/dates/prix max que le
    # premier produit (`product`/`quantity`/`unit` plus haut) — jamais mélangé avec
    # `additional_products` (noms nus, sans quantité, pour les AUTRES intentions,
    # ex: BUYER_REQUEST). Voir `new_task_prompts.py` pour la règle d'extraction.
    additional_items: List[NewTaskRecurringItem] = []
    # (2026-09-24, bug réel production — "14 coq et 57 moutons chèvres chaque semaine") :
    # UNE quantité pour PLUSIEURS produits sans répartition claire — voir
    # `NewTaskAmbiguousGroup`, jamais dans `additional_items` (qui suppose une quantité PAR
    # produit déjà connue).
    ambiguous_groups: List[NewTaskAmbiguousGroup] = []
    # (2026-09-26, bug réel production — "150 kg tomate et 200 kg chaque semaine") : voir
    # `NewTaskOrphanQuantity` ci-dessus — UNE quantité, ZÉRO nom de produit. Jamais dans
    # `additional_items` (qui exige un produit) ni `ambiguous_groups` (qui exige ≥2 candidats).
    orphan_quantities: List[NewTaskOrphanQuantity] = []
    # Correction d'une demande en cours : portée PROPOSÉE par le LLM (« ALL » = tout
    # remplacer, « ITEM » = un produit nommé). Validée par le domaine
    # (`domain/recurring_need_draft.py::plan_correction`), jamais appliquée à l'aveugle.
    correction_scope: Optional[Literal["ALL", "ITEM"]] = None
    # UPDATE_RECURRING_NEED (B24) : l'ACTION demandée sur un besoin récurrent EXISTANT quand ce n'est pas un simple
    # changement de quantité/fréquence — PROPOSÉE par le LLM, jamais exécutée sur sa seule foi : le flow la valide
    # (cible exacte, propriété, ambiguïté arrêter/ignorer). `null` pour « change la quantité/la fréquence ».
    update_action: Optional[Literal["PAUSE", "RESUME", "CANCEL", "SKIP_OCCURRENCE", "OVERRIDE_OCCURRENCE"]] = None


class NewTaskInterpretation(BaseModel):
    """Sortie brute (déjà JSON-décodée) du micro-prompt NEW_TASK.

    `confidence` (spec §22) : conservé pour compatibilité avec le
    downstream existant (`interpreter_confidence`) — PAS une probabilité
    calibrée, une pseudo-précision produite par le LLM. Documenté ici,
    jamais présenté comme davantage ailleurs."""

    model_config = {"extra": "forbid", "frozen": True}

    disposition: NewTaskDisposition
    intent: Optional[str] = None
    confidence: float = 0.0
    entities: NewTaskEntities = Field(default_factory=NewTaskEntities)
    #: AMBIGUOUS uniquement (Étape 9A/9B) — ≥2 intentions du catalogue
    #: fourni, également plausibles pour les mêmes faits. Vide pour toute
    #: autre disposition (revérifié par le model_validator ci-dessous).
    candidate_goals: List[str] = []

    @model_validator(mode="before")
    @classmethod
    def _default_entities(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("entities") is None:
            data = {**data, "entities": {}}
        return data

    @model_validator(mode="after")
    def _check_one_semantic_disposition(self) -> "NewTaskInterpretation":
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence doit être comprise entre 0.0 et 1.0")

        if self.disposition == NewTaskDisposition.NEW_TASK:
            if not self.intent:
                raise ValueError("NEW_TASK requiert un champ 'intent' explicite")
            if self.candidate_goals:
                raise ValueError("NEW_TASK ne doit porter aucun 'candidate_goals'")
            return self

        if self.disposition == NewTaskDisposition.AMBIGUOUS:
            if self.intent is not None:
                raise ValueError("AMBIGUOUS ne doit porter aucun champ 'intent'")
            if len(self.candidate_goals) < 2:
                raise ValueError(
                    "AMBIGUOUS requiert au moins 2 'candidate_goals' — sinon "
                    "ce n'est pas réellement ambigu, choisis NEW_TASK ou UNKNOWN"
                )
            # Les FAITS restent préservés (spec §2 : facts != action) — pas
            # de contrainte "entities vide" ici, à l'inverse de CONFIRM/
            # REJECT/OUT_OF_SCOPE/UNKNOWN ci-dessous.
            return self

        # CONFIRM/REJECT/OUT_OF_SCOPE/UNKNOWN : jamais d'intent, jamais
        # d'entités, jamais de candidate_goals — rien de fiable à en tirer,
        # même règle "one semantic action" que STRUCTURED_ACTION/ACTIVE_SLOT
        # (protection contre un mélange de sémantiques dans la même réponse).
        if self.intent is not None:
            raise ValueError(
                f"{self.disposition.value} ne doit porter aucun champ 'intent'"
            )
        if self.candidate_goals:
            raise ValueError(
                f"{self.disposition.value} ne doit porter aucun 'candidate_goals'"
            )
        if self.entities.model_dump(exclude_defaults=True):
            raise ValueError(
                f"{self.disposition.value} ne doit porter aucune entité "
                "(rien de fiable à en tirer)"
            )
        return self


@dataclass(frozen=True)
class NewTaskPromptContext:
    """Contexte MINIMAL réellement utile à NEW_TASK (spec §26) — jamais le
    menu/les IDs/`expected_candidates`/`last_agent_question` du prompt
    legacy (possédés par SELECTION/ACTIVE_SLOT/STRUCTURED_ACTION
    désormais)."""

    reference_date: str
    #: Panier acheteur prêt à valider (règle 1bis historique) — signal
    #: d'ÉTAT, jamais un mot-clé du texte. Toujours réel : ni SELECTION ni
    #: ACTIVE_SLOT ne couvrent ce cas (`CONFIRM_ACTION` est dans
    #: `SUBFLOW_OWNED_KINDS`, donc jamais routé vers ACTIVE_SLOT — voir
    #: `state_router.py`), donc NEW_TASK reste le SEUL endroit qui sait
    #: qu'un "je suis d'accord" libre doit devenir CONFIRM ici.
    cart_pending: bool = False
    #: But métier suspendu — UNIQUEMENT rempli lors d'une reclassification
    #: après DEVIATION (spec §27) : aide à comprendre "je veux plutôt
    #: vendre" sans jamais redonner tout le tunnel. `None` en usage normal
    #: (aucun tunnel actif au moment du NEW_TASK).
    previous_goal: Optional[str] = None
    #: Vente(s) en attente de confirmation producteur — signal d'ÉTAT
    #: (2026-09-14, incident réel répété : "confirmer"/"annuler" nu, tapé
    #: juste après `flows/buyer/order_tracking.py::_producer_sales_block`
    #: qui invite EXPLICITEMENT "Tapez *confirmer*... ou *annuler*...",
    #: retombait en UNKNOWN — `SALES_LIST_ORDERS` est READ, ne pose aucun
    #: tunnel/`PendingInteraction`, donc NEW_TASK reste le SEUL endroit qui
    #: peut savoir que ce mot nu répond à une instruction que L'AGENT
    #: LUI-MÊME vient de donner). Même principe que `cart_pending` ci-dessus
    #: — c'est le LLM, jamais un court-circuit Python, qui décide (voir
    #: `interpreter/routing.py::_interpret_fast_path`, dont chaque branche
    #: doit structurellement échoer `locked_goal` et ne peut donc jamais
    #: introduire un nouveau but).
    producer_order_action_pending: bool = False
    #: B24 — écran récurrent actuellement affiché (décrit par l'APPLICATION, jamais le texte utilisateur) : sert à juger la
    #: RELATION du message avec l'écran (corriger la cible affichée ou nouvelle tâche). Rempli uniquement quand l'attente
    #: d'un écran vivant a été écartée par la route SELECTION.
    screen_context: Optional[str] = None


def adapt_new_task_to_canonical(
    decision: NewTaskInterpretation,
    *,
    entities: Dict[str, Any],
    validation_status: Optional[str],
    locked_goal: Optional[str],
    path: str,
) -> Dict[str, Any]:
    """Vers la forme canonique `InterpreterResult.from_legacy_dict` — MÊME
    forme exacte que produisait l'ancien chemin LLM unifié pour ces mêmes
    dispositions (CONFIRM/REJECT reprennent `locked_goal` comme
    `detected_intent`, exactement comme le fast-path déterministe de
    `routing.py::_interpret_fast_path` le fait déjà pour le vocabulaire
    fermé) — le downstream (`goal_planner`, `nodes/validation.py`, les
    flows) ne sait pas d'où vient ce dict."""
    if decision.disposition == NewTaskDisposition.NEW_TASK:
        detected_intent = (decision.intent or "UNKNOWN").upper()
    elif decision.disposition in (NewTaskDisposition.CONFIRM, NewTaskDisposition.REJECT):
        detected_intent = str(locked_goal or "UNKNOWN").upper()
    else:
        # AMBIGUOUS/OUT_OF_SCOPE/UNKNOWN : aucune intention UNIQUE à porter —
        # pour AMBIGUOUS, les candidats vivent dans `candidate_goals`
        # ci-dessous, jamais dans `detected_intent` (spec §9 : pas de choix
        # arbitraire d'un "gagnant" parmi les candidats).
        detected_intent = "UNKNOWN"

    out: Dict[str, Any] = {
        "interpreted_event": decision.disposition.value,
        "detected_intent": detected_intent,
        "interpreter_confidence": decision.confidence,
        # AMBIGUOUS préserve les FAITS déjà compris (spec §2/§13) — jamais
        # vidés sous prétexte que l'action n'est pas résolue.
        "extracted_entities": entities,
        "raw_analysis": {"path": path},
    }
    if decision.disposition == NewTaskDisposition.AMBIGUOUS:
        out["candidate_goals"] = list(decision.candidate_goals)
    if validation_status:
        out["validation_status"] = validation_status
    return out


__all__ = [
    "NewTaskDisposition",
    "NewTaskPricingTier",
    "NewTaskRecurringItem",
    "NewTaskAmbiguousGroup",
    "NewTaskOrphanQuantity",
    "NewTaskEntities",
    "NewTaskInterpretation",
    "NewTaskPromptContext",
    "adapt_new_task_to_canonical",
]
