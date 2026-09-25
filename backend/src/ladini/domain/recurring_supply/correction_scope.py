"""Résolution du champ `ENTER_FIELD("correction_scope")` (voir `flows/buyer/recurring_need.py::
_correct`, `domain/recurring_need_draft.py::plan_correction`, branche `CorrectionNeedsScope`).

Quand une correction nomme un champ (produit/quantité...) sans désigner clairement QUEL item
d'un draft multi-produits elle vise, l'agent demande explicitement la portée : "tout remplacer"
ou lequel des items déjà présents. Cette fonction résout la réponse libre à CETTE question.

Fonction PURE, sans DB/LLM — même famille que `ambiguous_group.py::resolve_ambiguous_group_reply`
(texte libre -> décision déterministe, jamais devinée) : identifiée ici comme le même besoin
structurel (Phase 2 hardening, commit 7, C7 §13 : "clarification structurée avec `target`, pas
un champ scalaire") plutôt que dupliquée sous une forme légèrement différente."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Sequence

#: Vocabulaire fermé, universel (aucun nom de produit) — même idiome que
#: `ambiguous_group.py::_EACH_MARKERS`/`_REST_MARKERS` ou
#: `interpreter/routing.py::_CONFIRM_EXACT_PHRASES` : un ensemble FERMÉ de marqueurs
#: linguistiques génériques, jamais une liste de produits spécifiques.
_ALL_MARKERS = (
    "tout remplacer",
    "remplace tout",
    "remplacer tout",
    "tout",
    "tous",
    "toute",
    "toutes",
    "l'ensemble",
    "en entier",
)

#: Valeur de portée reconnue par `domain/recurring_need_draft.py::plan_correction`
#: (`CORRECTION_SCOPE_ALL`) — reprise ici à l'IDENTIQUE plutôt que réimportée pour éviter tout
#: risque de cycle domain<->market_coach (ce module reste sous `domain/recurring_supply`, hors
#: du package `market_coach`) ; les deux valeurs sont verrouillées ensemble par
#: `tests/unit/test_correction_scope_reply.py::test_the_all_scope_matches_the_domain_constant`.
SCOPE_ALL = "ALL"


def _fold(text: str) -> str:
    """Minuscule + sans accents — même normalisation que `ambiguous_group.py::_fold`, pour que
    les candidats (noms de produits déjà normalisés par l'interpréteur) matchent la réponse
    libre de l'utilisateur quels que soient accents/casse."""
    normalized = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in normalized if not unicodedata.combining(c))


class CorrectionScopeResolutionKind(str, Enum):
    RESOLVED = "RESOLVED"
    #: Le texte ne fait manifestement pas référence à cette question de portée (aucun candidat
    #: cité, aucun marqueur "tout") — traité comme une tâche autonome distincte par l'appelant,
    #: jamais comme une réponse mal formée (même contrat que `AmbiguousResolutionKind.NOT_A_RESOLUTION`).
    NOT_A_RESOLUTION = "NOT_A_RESOLUTION"


@dataclass(frozen=True)
class CorrectionScopeResolution:
    kind: CorrectionScopeResolutionKind
    #: `SCOPE_ALL` ou l'un des `candidates` passés en entrée — seulement si RESOLVED.
    scope: Optional[str] = None


def resolve_correction_scope_reply(
    text: str, *, candidates: Sequence[str]
) -> CorrectionScopeResolution:
    """Un des `candidates` (item déjà présent dans le draft) NOMMÉ dans le texte -> portée CE
    candidat (priorité : plus précis qu'un marqueur générique). Sinon, un marqueur "tout" ->
    `SCOPE_ALL`. Ni l'un ni l'autre -> `NOT_A_RESOLUTION`."""
    folded = _fold(text)
    if not folded.strip():
        return CorrectionScopeResolution(CorrectionScopeResolutionKind.NOT_A_RESOLUTION)

    named: List[str] = []
    for candidate in candidates:
        root = _fold(candidate)
        if root and root in folded:
            named.append(candidate)
    if len(named) == 1:
        return CorrectionScopeResolution(CorrectionScopeResolutionKind.RESOLVED, scope=named[0])
    # 0 ou 2+ candidats nommés à la fois : ambigu comme réponse ciblée — retombe sur "tout" si
    # présent, jamais un pick arbitraire du premier candidat cité.

    if any(marker in folded for marker in _ALL_MARKERS):
        return CorrectionScopeResolution(CorrectionScopeResolutionKind.RESOLVED, scope=SCOPE_ALL)

    return CorrectionScopeResolution(CorrectionScopeResolutionKind.NOT_A_RESOLUTION)


__all__ = [
    "SCOPE_ALL",
    "CorrectionScopeResolutionKind",
    "CorrectionScopeResolution",
    "resolve_correction_scope_reply",
]
