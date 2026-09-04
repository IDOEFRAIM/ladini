"""InterpreterResult — contrat UNIQUE pour le résultat de l'interprétation
(refonte architecturale 2026-09-02, mandat §9-13).

Avant ce module, le fast-path déterministe (`_interpret_fast_path`) et le
chemin LLM (`input_interpreter`) écrivaient chacun un dict ad-hoc — de forme
identique par convention (`interpreted_event`/`detected_intent`/
`interpreter_confidence`/`extracted_entities`/`raw_analysis`), mais sans
aucun point de passage commun qui GARANTISSE cette forme ni qui empêche
`UNKNOWN` de rester un fourre-tout muet. Ce module ne réécrit PAS la
mécanique d'interprétation (~500 lignes de règles déterministes/regex
patiemment durcies contre des incidents réels, cf. commentaires de
`interpreter/routing.py`) — il ajoute UN point de conversion obligatoire,
à la sortie de `make_input_interpreter`, par lequel TOUT résultat (fast-path
ET LLM) doit passer avant de devenir un patch d'état. C'est ce point unique
qui rend structurellement impossible un `UNKNOWN` sans `UnknownReason`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class UnknownReason(str, Enum):
    """Catégories explicites — voir mandat §11. Le router (`interpreter/
    routing.py::route_after_validator`) et `nodes/rendering/*` peuvent
    désormais distinguer POURQUOI l'interprétation n'a pas permis de
    progresser, au lieu d'un `UNKNOWN` muet routé uniformément vers
    `to_strategy`."""

    AMBIGUOUS = "AMBIGUOUS"
    UNSUPPORTED = "UNSUPPORTED"
    NO_ACTION = "NO_ACTION"
    INVALID_ACTION = "INVALID_ACTION"
    INTERPRETATION_FAILURE = "INTERPRETATION_FAILURE"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"


#: `raw_analysis.path` -> raison, pour les points où la CAUSE de l'UNKNOWN
#: est connue avec certitude au moment où le dict legacy est produit (panne
#: d'infrastructure, pas une ambiguïté de contenu). Tout `path` absent de
#: cette table retombe sur `_DEFAULT_UNKNOWN_REASON` : le LLM lui-même a
#: classé le message comme ne correspondant à rien de connu — la vraie
#: notion d'"ambiguïté" au sens du mandat.
_UNKNOWN_REASON_BY_PATH: Dict[str, UnknownReason] = {
    "no_llm": UnknownReason.TECHNICAL_FAILURE,
    "llm_crash": UnknownReason.TECHNICAL_FAILURE,
}
_DEFAULT_UNKNOWN_REASON = UnknownReason.AMBIGUOUS

_KNOWN_LEGACY_KEYS = frozenset(
    {
        "interpreted_event",
        "detected_intent",
        "interpreter_confidence",
        "extracted_entities",
        "raw_analysis",
        "unknown_reason",
    }
)


@dataclass(frozen=True)
class InterpreterResult:
    """Contrat unique — que le producteur soit le fast-path déterministe ou
    l'appel LLM, l'état ne reçoit jamais rien d'autre que la sérialisation
    de CE type (voir `to_state_patch`)."""

    event: str
    intent: str
    entities: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    raw_analysis: Dict[str, Any] = field(default_factory=dict)
    reason: Optional[UnknownReason] = None
    # Clés legacy supplémentaires produites par certains chemins (ex:
    # `validation_status` sur le chemin LLM) — passthrough explicite plutôt
    # que silencieusement perdues par la conversion.
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_legacy_dict(cls, d: Optional[Dict[str, Any]]) -> "InterpreterResult":
        d = d or {}
        event = str(d.get("interpreted_event") or "UNKNOWN").upper().strip()
        raw_analysis = dict(d.get("raw_analysis") or {})

        reason: Optional[UnknownReason] = None
        if event == "UNKNOWN":
            explicit = d.get("unknown_reason")
            if explicit:
                try:
                    reason = UnknownReason(str(explicit).upper().strip())
                except ValueError:
                    reason = None
            if reason is None:
                path = str(raw_analysis.get("path") or "")
                reason = _UNKNOWN_REASON_BY_PATH.get(path, _DEFAULT_UNKNOWN_REASON)

        extra = {k: v for k, v in d.items() if k not in _KNOWN_LEGACY_KEYS}

        return cls(
            event=event,
            intent=str(d.get("detected_intent") or "UNKNOWN").upper().strip(),
            entities=dict(d.get("extracted_entities") or {}),
            confidence=float(d.get("interpreter_confidence") or 0.0),
            raw_analysis=raw_analysis,
            reason=reason,
            extra=extra,
        )

    def to_state_patch(self) -> Dict[str, Any]:
        """Sérialisation — le SEUL format que `input_interpreter` renvoie
        désormais au graphe, quel que soit le chemin interne qui a produit
        le résultat."""
        patch: Dict[str, Any] = {
            "interpreted_event": self.event,
            "detected_intent": self.intent,
            "interpreter_confidence": self.confidence,
            "extracted_entities": self.entities,
            "raw_analysis": self.raw_analysis,
            "unknown_reason": self.reason.value if self.reason else None,
        }
        patch.update(self.extra)
        return patch


__all__ = ["InterpreterResult", "UnknownReason"]
