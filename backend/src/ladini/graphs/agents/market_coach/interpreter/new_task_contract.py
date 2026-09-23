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
from typing import Any, Dict, List, Optional

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
            return self

        # CONFIRM/REJECT/OUT_OF_SCOPE/UNKNOWN : jamais d'intent, jamais
        # d'entités — rien de fiable à en tirer, même règle "one semantic
        # action" que STRUCTURED_ACTION/ACTIVE_SLOT (protection contre un
        # mélange de sémantiques dans la même réponse).
        if self.intent is not None:
            raise ValueError(
                f"{self.disposition.value} ne doit porter aucun champ 'intent'"
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
        detected_intent = "UNKNOWN"

    out: Dict[str, Any] = {
        "interpreted_event": decision.disposition.value,
        "detected_intent": detected_intent,
        "interpreter_confidence": decision.confidence,
        "extracted_entities": entities,
        "raw_analysis": {"path": path},
    }
    if validation_status:
        out["validation_status"] = validation_status
    return out


__all__ = [
    "NewTaskDisposition",
    "NewTaskPricingTier",
    "NewTaskEntities",
    "NewTaskInterpretation",
    "NewTaskPromptContext",
    "adapt_new_task_to_canonical",
]
