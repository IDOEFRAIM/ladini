"""Résolution d'un `ambiguous_group` (une quantité pour PLUSIEURS produits sans répartition
claire — voir `interpreter/new_task_contract.py::NewTaskAmbiguousGroup`) à partir de la réponse
libre de l'utilisateur à la clarification (`flows/buyer/recurring_need.py::
_ambiguous_quantity_clarification`).

Fonction PURE, sans DB/LLM — mécanique GÉNÉRALE pour N candidats (pas un cas spécial
"mouton/chèvre") : détecte le mode TOTAL/EACH/RESTE à partir du texte brut, valide les quantités
retrouvées contre le total connu, et ne calcule "le reste" QUE si un SEUL candidat reste sans
quantité explicite (mandat §5 : jamais un calcul silencieux ambigu)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

_EACH_MARKERS = ("de chaque", "chacun", "chacune")
_REST_MARKERS = ("le reste", "reste", "restant", "restante")
_NUMBER_RE = re.compile(r"(-?\d+(?:[.,]\d+)?)")


def _fold(text: str) -> str:
    """Minuscule + sans accents — même normalisation des deux côtés (candidats ET texte
    utilisateur) pour que "chevre" (candidat, déjà non accentué par l'interpréteur) matche
    "chèvres"/"CHÈVRE"/... dans la réponse libre."""
    normalized = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in normalized if not unicodedata.combining(c))


class AmbiguousResolutionKind(str, Enum):
    RESOLVED = "RESOLVED"
    #: Le texte tente clairement de répondre (mentionne un candidat, "de chaque" ou "reste")
    #: mais le résultat est invalide (somme ne correspond pas au total, valeur négative, ou plus
    #: d'un candidat reste sans quantité explicite) — une clarification CIBLÉE doit être posée,
    #: jamais le slot générique `ambiguous_quantity`.
    INVALID = "INVALID"
    #: Le texte ne fait manifestement pas référence à cette clarification (aucun candidat cité,
    #: aucun marqueur EACH/RESTE) — traité comme une tâche autonome distincte par l'appelant
    #: (interruption), jamais comme une réponse mal formée.
    NOT_A_RESOLUTION = "NOT_A_RESOLUTION"


@dataclass(frozen=True)
class AmbiguousResolution:
    kind: AmbiguousResolutionKind
    #: {candidat: quantité}, un candidat par entrée — seulement si RESOLVED.
    allocations: Optional[Dict[str, float]] = None
    #: Message de clarification ciblée à afficher — seulement si INVALID.
    message: Optional[str] = None


def _find_candidate_positions(folded_text: str, candidates: List[str]) -> Dict[str, List[Tuple[int, int]]]:
    """Position (start, end) de CHAQUE occurrence de chaque candidat dans le texte — un candidat
    est reconnu comme RACINE (`chevre` matche `chevres`/`chèvres`), jamais un mot entier strict,
    pour couvrir pluriels/accents déjà pliés par `_fold`."""
    positions: Dict[str, List[Tuple[int, int]]] = {c: [] for c in candidates}
    for candidate in candidates:
        root = _fold(candidate)
        if not root:
            continue
        for m in re.finditer(re.escape(root), folded_text):
            positions[candidate].append((m.start(), m.end()))
    return positions


def resolve_ambiguous_group_reply(
    text: str,
    *,
    total_quantity: float,
    candidates: List[str],
    epsilon: float = 1e-6,
) -> AmbiguousResolution:
    """Résout un `ambiguous_group` (mandat §1-§6) :

    - "57 de chaque" / "chacun" (mode EACH) -> chaque candidat reçoit `total_quantity`.
    - N quantités explicites, une par candidat (mode TOTAL explicite) -> validées contre
      `total_quantity` (somme égale, jamais silencieusement acceptée sinon).
    - Une quantité explicite + "le reste" pour l'UNIQUE candidat restant (mode RESTE) ->
      reste = total - somme des quantités explicites, refusé si négatif ou si PLUSIEURS
      candidats restent sans quantité (mandat §5 : jamais un calcul ambigu).
    - Rien de tout ça (aucun candidat cité, aucun marqueur EACH/RESTE) -> NOT_A_RESOLUTION,
      laissé à l'appelant pour traiter le message comme une tâche autonome distincte.
    """
    folded = _fold(text)
    if not folded.strip():
        return AmbiguousResolution(AmbiguousResolutionKind.NOT_A_RESOLUTION)

    has_each_marker = any(marker in folded for marker in _EACH_MARKERS)
    has_rest_marker = any(marker in folded for marker in _REST_MARKERS)
    positions = _find_candidate_positions(folded, candidates)
    mentioned = [c for c in candidates if positions[c]]

    if not mentioned and not has_each_marker:
        return AmbiguousResolution(AmbiguousResolutionKind.NOT_A_RESOLUTION)

    # Mode EACH — "57 de chaque"/"chacun" : chaque candidat reçoit le total connu, qu'il soit
    # nommément cité ou non (la question posée listait déjà tous les candidats). Priorité sur
    # TOTAL/RESTE : "de chaque" est une réponse à l'alternative EXACTE posée par la clarification
    # elle-même ("... au TOTAL... ou ... DE CHAQUE ?"), jamais une simple coïncidence de mots.
    if has_each_marker:
        return AmbiguousResolution(
            AmbiguousResolutionKind.RESOLVED,
            allocations={c: total_quantity for c in candidates},
        )

    # Un nombre par candidat cité, apparié par proximité GLOBALE (paire candidat/nombre la plus
    # proche d'abord, chaque nombre consommé au plus une fois) — sans ce couplage global, "30
    # tomates et le reste en oignons" laissait le SEUL nombre du texte (30) être repris par LES
    # DEUX candidats (chacun le trouvait "le plus proche" indépendamment), masquant le mode RESTE.
    explicit: Dict[str, float] = _match_numbers_to_candidates(folded, positions, mentioned)

    unspecified = [c for c in candidates if c not in explicit]

    if has_rest_marker:
        if len(unspecified) == 0:
            # "le reste" cité mais tout le monde a déjà une quantité explicite — ambigu, on ne
            # devine pas à qui il s'adresse.
            return AmbiguousResolution(
                AmbiguousResolutionKind.INVALID,
                message=_targeted_message(candidates, explicit, total_quantity),
            )
        if len(unspecified) > 1:
            return AmbiguousResolution(
                AmbiguousResolutionKind.INVALID,
                message=_targeted_message(candidates, explicit, total_quantity),
            )
        remainder_candidate = unspecified[0]
        remainder = total_quantity - sum(explicit.values())
        if remainder < -epsilon:
            return AmbiguousResolution(
                AmbiguousResolutionKind.INVALID,
                message=_targeted_message(candidates, explicit, total_quantity),
            )
        explicit[remainder_candidate] = max(remainder, 0.0)
        return AmbiguousResolution(AmbiguousResolutionKind.RESOLVED, allocations=explicit)

    if unspecified:
        # Certains candidats ont une quantité, d'autres non, et aucun "reste" ne les couvre —
        # clarification ciblée plutôt qu'une invention silencieuse.
        return AmbiguousResolution(
            AmbiguousResolutionKind.INVALID,
            message=_targeted_message(candidates, explicit, total_quantity),
        )

    total_given = sum(explicit.values())
    if any(v < -epsilon for v in explicit.values()):
        return AmbiguousResolution(
            AmbiguousResolutionKind.INVALID,
            message=_targeted_message(candidates, explicit, total_quantity),
        )
    if abs(total_given - total_quantity) > epsilon:
        return AmbiguousResolution(
            AmbiguousResolutionKind.INVALID,
            message=(
                f"{_fmt(total_given)} au total ({', '.join(f'{_fmt(v)} {c}' for c, v in explicit.items())}) "
                f"— mais {_fmt(total_quantity)} attendu. Merci de corriger."
            ),
        )
    return AmbiguousResolution(AmbiguousResolutionKind.RESOLVED, allocations=explicit)


def _match_numbers_to_candidates(
    folded_text: str, positions: Dict[str, List[Tuple[int, int]]], mentioned: List[str], window: int = 30
) -> Dict[str, float]:
    """Apparie chaque candidat cité au nombre le plus proche, GLOBALEMENT (glouton par distance
    croissante sur toutes les paires candidat/occurrence-de-nombre) — un même nombre n'est jamais
    consommé par deux candidats, sinon "30 tomates et le reste en oignons" laissait le seul "30"
    du texte être repris par les DEUX candidats indépendamment (couvre "50 mouton" ET "mouton:
    50" sans favoriser un ordre particulier)."""
    numbers = [(m.start(), m.end(), _to_float(m.group(1))) for m in re.finditer(_NUMBER_RE, folded_text)]
    if not numbers:
        return {}

    triples: List[Tuple[int, int, str]] = []  # (distance, number_index, candidate)
    for candidate in mentioned:
        for cand_start, cand_end in positions[candidate]:
            for idx, (num_start, num_end, _value) in enumerate(numbers):
                if num_end <= cand_start:
                    distance = cand_start - num_end
                elif num_start >= cand_end:
                    distance = num_start - cand_end
                else:
                    continue  # chevauchement improbable, ignoré
                if distance <= window:
                    triples.append((distance, idx, candidate))
    triples.sort(key=lambda t: t[0])

    explicit: Dict[str, float] = {}
    used_numbers: set = set()
    for _distance, idx, candidate in triples:
        if candidate in explicit or idx in used_numbers:
            continue
        explicit[candidate] = numbers[idx][2]
        used_numbers.add(idx)
    return explicit


def _to_float(raw: str) -> float:
    return float(raw.replace(",", "."))


def _fmt(value: float) -> str:
    return f"{value:g}"


def _targeted_message(candidates: List[str], explicit: Dict[str, float], total: float) -> str:
    labels = [c.capitalize() for c in candidates]
    known = ", ".join(f"{_fmt(v)} {c.capitalize()}" for c, v in explicit.items()) or "aucune quantité reconnue"
    return (
        f"Je n'ai pas pu répartir les {_fmt(total)} entre {' et '.join(labels)} ({known}). "
        f"Merci de préciser une quantité pour chaque produit, par exemple "
        f"\"{int(total) if float(total).is_integer() else total} {labels[0].lower()} et le reste pour "
        f"{labels[1].lower() if len(labels) > 1 else 'les autres'}\"."
    )


__all__ = [
    "AmbiguousResolutionKind",
    "AmbiguousResolution",
    "resolve_ambiguous_group_reply",
]
