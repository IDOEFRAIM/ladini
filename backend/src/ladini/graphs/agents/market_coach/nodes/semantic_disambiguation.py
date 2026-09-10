from __future__ import annotations

from typing import Any, Dict, List, Optional

from ladini.graphs.agents.market_coach.core.base import get_node_logger
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.interpreter.intent import (
    INTENT_CONFIG,
    INTENT_DISAMBIGUATION,
)
from ladini.graphs.agents.market_coach.utils import (
    MarketRuntime,
    normalize_slot_keys,
)

logger = get_node_logger("SemanticDisambiguationNode")

_DISAMBIGUATION_CONFIDENCE_THRESHOLD = 0.85


def _detect_disambiguation_candidates(
    text_lower: str, role_upper: str | None = None
) -> Optional[Dict[str, Any]]:
    """Return the INTENT_DISAMBIGUATION entry with the MOST SPECIFIC matching hint.

    Picking the first dict entry with any substring match (declaration order)
    lets a short generic hint ("suivre") shadow a longer, far more specific
    hint declared later ("suivre mes appels d'offres") — e.g. "Suivre mes
    appels d'offre" matched ORDER_TRACKING_INTENT's generic "suivre" before
    ever reaching AUCTION_TRACKING_INTENT's exact phrase, sending the buyer
    to the wrong menu (commandes instead of enchères). Scoring by the
    longest matched hint across ALL entries makes specificity win regardless
    of declaration order.
    """
    # NOTE (refonte double-rôle) : `role_upper` n'est plus utilisé pour EXCLURE
    # des entrées — un même utilisateur peut déclencher un menu de
    # désambiguïsation producteur OU acheteur selon le texte, quel que soit
    # son rôle de session par défaut. Le paramètre est conservé pour compat
    # de signature (appelants existants) mais n'a plus d'effet filtrant.
    best_entry: Optional[Dict[str, Any]] = None
    best_key: Optional[str] = None
    best_len = 0
    for key, entry in INTENT_DISAMBIGUATION.items():
        hints = entry.get("lexical_hints") or []
        for hint in hints:
            hint_lower = str(hint or "").lower()
            if hint_lower and hint_lower in text_lower and len(hint_lower) > best_len:
                best_len = len(hint_lower)
                best_entry = entry
                best_key = key
    if best_entry is not None:
        return {"id": best_key, **best_entry}
    return None


def extract_disambiguation_intents(entry: Dict[str, Any]) -> List[tuple[str, str]]:
    """Point UNIQUE de lecture des intents candidats d'une entrée
    `INTENT_DISAMBIGUATION` (2026-09-08, clôture Bloc 1, mandat §21/§22).

    Schéma canonique : `entry["options"]` est la SEULE source — un ancien
    champ `candidates` (liste d'intents nue, redondante avec `options`) a
    été retiré du catalogue. `options` accepte deux formes historiques,
    normalisées ici en `(intent, label)` :
        - tuple/list : `(intent, label)` ;
        - dict       : `{"intent": ..., "label": ...}`.
    `cognitive_guard` (pour `intent_competition`) et ce module (pour
    construire le menu) appellent tous les deux CE helper — plus de
    seconde lecture indépendante de `options` qui pourrait diverger."""
    pairs: List[tuple[str, str]] = []
    for opt in entry.get("options") or []:
        if isinstance(opt, (tuple, list)) and len(opt) >= 2:
            intent_key, label = opt[0], opt[1]
        elif isinstance(opt, dict):
            intent_key, label = opt.get("intent"), opt.get("label")
        else:
            continue
        if not intent_key:
            continue
        pairs.append((str(intent_key), str(label or intent_key)))
    return pairs


async def semantic_disambiguation(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
) -> Dict[str, Any]:
    """Render un menu AG-UI de désambiguïsation — EXÉCUTEUR d'une décision
    déjà prise par `cognitive_guard`.

    (2026-09-08, correction topologique du bloc conversationnel, mandat
    §12/§14) : ce nœud ne décide plus lui-même « faut-il désambiguïser ? ».
    Avant ce correctif, il recalculait ICI son propre jeu de conditions
    (event ∈ {NEW_TASK, UNKNOWN}, absence de `pending_interaction` actif,
    seuil de confiance LLM) — une SECONDE policy de désambiguïsation,
    potentiellement en désaccord avec celle que `cognitive_guard` applique
    pour DÉCIDER de router ici (voir sa docstring, section DISAMBIGUATE).
    Le graphe compilé n'atteint plus ce nœud QUE via
    `cognitive_decision.action == "DISAMBIGUATE"`
    (`nodes/routing.py::_route_after_cognitive_guard`), qui a déjà vérifié
    l'intégralité de ces conditions AVANT de router ici — les revérifier
    serait dupliquer la même décision à deux endroits (source d'écarts
    futurs, exactement la classe de bug que ce chantier corrige).

    Reste ICI, et seulement ICI (exécution, pas décision) :
    - la construction du menu à partir du candidat précalculé
      (`disambiguation_candidate`, contrat : voir `cognitive_guard`) ;
    - le repli de compatibilité si ce champ est absent (ancien checkpoint
      persisté avant ce déploiement — voir ci-dessous) ;
    - le garde-fou structurel `len(options) >= 2` (un menu à one option
      n'a pas de sens, quel que soit l'appelant) ;
    - le stash des entités déjà extraites, le pending_interaction, le menu
      AG-UI — dette de rendu explicitement NON reprise dans ce chantier
      (mandat §14 : "ne pas refondre ces dettes sans goal_planner/
      memory_update").
    """
    # (2026-09-08, P1-5 audit architectural) : `forced_role` supprimé — ce
    # canal n'a jamais existé (voir interpreter/routing.py).
    role_upper = str(state.get("user_role") or "").upper().strip()
    text_lower = (state.get("normalized_text") or state.get("user_query") or "").lower()

    # Repli de compatibilité (mandat §13) : `disambiguation_candidate` est
    # calculé INCONDITIONNELLEMENT par `cognitive_guard` à chaque tour
    # depuis 2026-09-08 (champ EPHEMERAL, jamais persisté d'un tour à
    # l'autre — voir `core/state_profile.py`) donc ce repli n'a en pratique
    # aucun lecteur légitime SAUF un appel de ce nœud hors du graphe
    # compilé (test unitaire direct, script). Conservé néanmoins : le coût
    # est nul (un seul appel lexical local, déjà existant) et il évite un
    # menu vide silencieux si un futur appelant oubliait de précalculer ce
    # champ avant de router ici.
    entry = state.get("disambiguation_candidate")
    if not entry:
        logger.debug(
            "[Disambiguation] disambiguation_candidate absent — repli de "
            "compatibilité sur le calcul lexical local"
        )
        entry = _detect_disambiguation_candidates(text_lower, role_upper)

    intents = extract_disambiguation_intents(entry) if entry else []
    if len(intents) < 2:
        # (2026-09-08, clôture Bloc 1, mandat §25) : ce nœud n'est atteint
        # QUE via `cognitive_decision.action == "DISAMBIGUATE"`, décision
        # qui exige DÉJÀ un candidat avec ≥2 options valides (voir
        # `nodes/cognitive.py::_classify_nominal_action`). Arriver ici sans
        # candidat exploitable est donc structurellement IMPOSSIBLE dans le
        # graphe compilé nominal — sauf violation de contrat (checkpoint
        # corrompu, appel direct hors graphe). Logué en ERROR : silencieux
        # avant ce correctif, désormais observable.
        if (state.get("cognitive_decision") or {}).get("action") == "DISAMBIGUATE":
            logger.error(
                "[Disambiguation] CONTRACT VIOLATION — action=DISAMBIGUATE mais "
                "aucun candidat exploitable (disambiguation_candidate=%r, "
                "intents résolus=%d) — cognitive_guard a laissé passer une "
                "décision DISAMBIGUATE invalide.",
                entry,
                len(intents),
            )
        return {}

    title = entry.get("title") or "Que souhaitez-vous faire exactement ?"
    pedagogical_intro = entry.get("pedagogical_hint") or ""
    mapping: Dict[str, str] = {}
    labels: List[str] = []
    lines = [f"🤔 *{title}*"]
    if pedagogical_intro:
        lines.append(f"\n{pedagogical_intro}\n")

    for i, (intent_key, label) in enumerate(intents, start=1):
        mapping[str(i)] = intent_key
        labels.append(label)
        intent_label = (INTENT_CONFIG.get(intent_key) or {}).get("label", "")
        if intent_label and intent_label != label:
            lines.append(f"{i}. *{label}* — {intent_label}")
        else:
            lines.append(f"{i}. {label}")

    lines.append("\nRépondez simplement par le numéro de votre choix.")
    logger.info(
        "[Disambiguation] action=%s trigger=%s candidates=%d",
        (state.get("cognitive_decision") or {}).get("action"),
        entry.get("id"),
        len(mapping),
    )

    # Stash any entities already extracted from the triggering utterance
    # (e.g. "J'ai 958 kg de tomates à 375 FCFA") into transaction_payload so
    # they survive the disambiguation turn. Without this the menu short-circuits
    # before memory_update promotes them, post_response_cleanup wipes
    # extracted_entities, and the chosen tunnel restarts its form from scratch.
    stashed_payload = dict(state.get("transaction_payload") or {})
    extracted = normalize_slot_keys(dict(state.get("extracted_entities") or {}))
    for key, value in extracted.items():
        if value in (None, "", [], {}):
            continue
        stashed_payload.setdefault(key, value)

    return {
        "status": "WAITING_INPUT",
        "current_goal": "DISAMBIGUATION_PENDING",
        "goal_status": "WAITING_INPUT",
        **set_pending_interaction(
            InteractionKind.SELECTION_MENU,
            goal="DISAMBIGUATION_PENDING",
            context_ref="intent_disambiguation",
        ),
        "expected_candidates": labels,
        "available_mapping": mapping,
        "transaction_payload": stashed_payload,
        "working_memory": {
            **(state.get("working_memory") or {}),
            "available_mapping_kind": "intent_disambiguation",
            "disambiguation_pending": True,
            "disambiguation_trigger_id": entry.get("id"),
        },
        "response_strategy": "SELECTION_MENU",
        "final_response": "\n".join(lines),
        "ag_ui_component": {
            "lc_type": "constructor",
            "id": ["ag_ui", "ListMenu"],
            "kwargs": {
                "title": title,
                "options": [
                    {"index": str(i), "label": lbl}
                    for i, lbl in enumerate(labels, start=1)
                ],
                "metadata": {"kind": "intent_disambiguation"},
            },
        },
    }


__all__ = [
    "semantic_disambiguation",
    "_detect_disambiguation_candidates",
    "_DISAMBIGUATION_CONFIDENCE_THRESHOLD",
]
