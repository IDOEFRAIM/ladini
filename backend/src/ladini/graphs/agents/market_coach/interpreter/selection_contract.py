"""Contrat JSON minimal du micro-prompt SELECTION (chantier "State Router +
micro-prompts", INCRÉMENT B, 2026-09-12).

Ce module ne fait QUE la validation/l'adaptation — aucun appel LLM ici (voir
`interpreter/selection_micro.py` pour l'orchestration réseau/cache/repair).

## Principe (spec §7-§11)

Le LLM ne retourne jamais d'identifiant technique (`producer_id`,
`pricing_tier_id`, `bid_id`, `order_id`...) — uniquement une désignation
humaine :

- ``selection_index`` : position 1-based dans `expected_candidates`.
- ``selected_value`` : texte libre, résolu par les flows métier EXISTANTS
  (`flows/buyer/procurement.py`, `flows/producer/flow.py`,
  `flows/buyer/order_tracking.py`...) qui font déjà, depuis longtemps,
  leur propre résolution texte→candidat (substring case-insensitive contre
  leurs propres objets métier) — voir l'audit de ce module dans le rapport
  de l'incrément. Ce contrat ne réimplémente PAS une deuxième résolution
  concurrente (spec §4/§10) : `selected_value` traverse tel quel jusqu'à
  ces résolveurs déjà testés.

Les BORNES de ``selection_index`` sont en revanche vérifiées ICI
(`index_within_bounds`) — spec §9 : "le LLM n'est jamais la source de
vérité sur les bornes". C'est une vérification NOUVELLE, plus stricte que
l'ancien fast-path générique (qui ne bornait jamais l'index avant ce
chantier) : elle ne s'applique qu'au nouveau chemin micro-prompt, jamais à
l'interpréteur unifié legacy (route inchangée pour les autres cas)."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class SelectionEvent(str, Enum):
    SELECTION = "SELECTION"
    INTERRUPTION = "INTERRUPTION"
    UNKNOWN = "UNKNOWN"


class SelectionInterpretation(BaseModel):
    """Sortie brute (déjà JSON-décodée) du micro-prompt SELECTION, avant
    adaptation vers le contrat canonique. `frozen=True` : une fois validée,
    ne se modifie plus — mêmes conventions que `domain/selection_actions.py
    ::SelectionAction`."""

    event: SelectionEvent
    selection_index: Optional[int] = None
    selected_value: Optional[str] = None
    #: B27 — certitude du modèle (0..1) sur la LECTURE du message ; jamais une autorisation : une entrée MUTANTE (accepter,
    #: refuser, exécuter) exige en plus la double lecture sémantique et la cohérence des nombres (voir `context_arbitration`).
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    #: B27 — RÉFÉRENCE TEMPORELLE extraite (jamais une date) : le modèle comprend « demain »/« le 5 octobre », le DOMAINE calcule
    #: la date et choisit l'option (`context_arbitration.resolve_date_reference`). `date_offset_days` : 0 aujourd'hui, 1 demain...
    date_offset_days: Optional[int] = Field(default=None, ge=-30, le=366)
    date_day: Optional[int] = Field(default=None, ge=1, le=31)
    date_month: Optional[int] = Field(default=None, ge=1, le=12)
    #: Ce que la date désigne : le DÉMARRAGE du besoin (« celui qui commence le 5 ») ou une LIVRAISON (« celle de demain »).
    date_role: Optional[Literal["START", "DELIVERY"]] = None

    model_config = {"frozen": True}

    @model_validator(mode="after")
    def _check_field_consistency(self) -> "SelectionInterpretation":
        if self.event == SelectionEvent.SELECTION:
            if self.selection_index is None and not self.selected_value:
                raise ValueError(
                    "SELECTION requiert selection_index OU selected_value"
                )
            if self.selection_index is not None and self.selected_value:
                raise ValueError(
                    "SELECTION ne doit pas porter à la fois "
                    "selection_index ET selected_value"
                )
        elif self.selection_index is not None or self.selected_value:
            raise ValueError(
                f"{self.event.value} ne doit porter ni selection_index "
                "ni selected_value"
            )
        if self.event != SelectionEvent.SELECTION and (
            self.date_offset_days is not None or self.date_day is not None or self.date_month is not None
            or self.date_role is not None
        ):
            raise ValueError(f"{self.event.value} ne doit porter aucune référence de date")
        return self


def index_within_bounds(
    interpretation: SelectionInterpretation, num_candidates: int
) -> bool:
    """`True` si `selection_index` (quand présent) tombe dans
    `[1, num_candidates]` — `True` aussi quand `selection_index` est absent
    (rien à borner). Le LLM n'a jamais le dernier mot sur les bornes
    (spec §9) : un index hors bornes rend l'interprétation INVALIDE, à
    traiter comme un JSON invalide par l'appelant (déclenche le repair)."""
    if interpretation.selection_index is None:
        return True
    return 1 <= interpretation.selection_index <= num_candidates


def adapt_selection_to_canonical(
    interpretation: SelectionInterpretation,
) -> Dict[str, Any]:
    """Traduit vers le contrat `input_interpreter` canonique historique —
    EXACTEMENT le même format que l'ancien fast-path numérique/le bypass
    interactif (`detected_intent="UNKNOWN"`, voir `interpreter/routing.py`
    fast-path 1) : aucun consommateur downstream (`goal_planner.py`,
    `nodes/memory.py`) ne lit `detected_intent` pour un événement SELECTION,
    seul `interpreted_event`/`extracted_entities` compte — confirmé par
    audit avant cet incrément.

    N'accepte PAS `SelectionEvent.INTERRUPTION` : ce cas est géré par
    l'appelant (`interpreter/selection_micro.py`), qui doit retomber sur le
    classifier NEW_TASK existant plutôt que produire un résultat ici (spec
    §12/§13/§18 — le micro-prompt SELECTION ne classifie jamais lui-même une
    nouvelle intention)."""
    if interpretation.event == SelectionEvent.SELECTION:
        entities: Dict[str, Any] = {}
        if interpretation.selection_index is not None:
            entities["selection_index"] = interpretation.selection_index
        else:
            entities["selected_value"] = interpretation.selected_value
        analysis: Dict[str, Any] = {"path": "selection_microprompt"}
        if interpretation.confidence is not None:
            analysis["selection_confidence"] = interpretation.confidence
        refs = {k: v for k, v in (("offset_days", interpretation.date_offset_days), ("day", interpretation.date_day),
                                  ("month", interpretation.date_month), ("role", interpretation.date_role)) if v is not None}
        if refs:
            analysis["date_reference"] = refs  # lu par `context_arbitration.resolve_date_reference` (le domaine calcule la date)
        return {
            "interpreted_event": "SELECTION",
            "detected_intent": "UNKNOWN",
            "interpreter_confidence": 0.95,
            "extracted_entities": entities,
            "raw_analysis": analysis,
        }
    if interpretation.event == SelectionEvent.UNKNOWN:
        return {
            "interpreted_event": "UNKNOWN",
            "detected_intent": "UNKNOWN",
            "interpreter_confidence": 0.3,
            "extracted_entities": {},
            "raw_analysis": {"path": "selection_microprompt"},
        }
    raise ValueError(
        "INTERRUPTION n'est pas adaptable ici — l'appelant doit retomber "
        "sur le classifier NEW_TASK (spec §12/§13/§18)."
    )


__all__ = [
    "SelectionEvent",
    "SelectionInterpretation",
    "index_within_bounds",
    "adapt_selection_to_canonical",
]
