from __future__ import annotations

import inspect
import re
from typing import Any, Dict

from ladini.graphs.agents.market_coach.core.base import get_node_logger
from ladini.graphs.agents.market_coach.utils import MarketRuntime, _normalize_text, _now

logger = get_node_logger("InputNormalizer")

# =====================================================================
# input_normalizer — question UNIQUE : « quelle est l'entrée utilisateur
# canonique de ce tour ? » (2026-09-08, refonte responsabilités des nœuds
# d'entrée).
#
# Ce nœud est délibérément déterministe et sans réseau (hors transcription
# audio, tant que l'adapter d'entrée dédié n'existe pas). Il NE DOIT PLUS :
#   - prendre de décision de sécurité (détection de prompt injection —
#     déplacée vers `nodes/security_moderation.py`, qui en devient
#     l'unique propriétaire, y compris comme SIGNAL, jamais comme
#     remplacement du texte utilisateur) ;
#   - charger le profil / précharger les fermes / router l'onboarding —
#     déplacé vers `nodes/session_bootstrap.py`, qui s'exécute juste
#     avant ce nœud dans le graphe (voir sa docstring pour la
#     justification de cette dette assumée) ;
#   - résoudre le rôle métier, gérer le tunnel, modifier `current_goal`,
#     écrire une réponse finale, un `response_strategy` métier ou un
#     `ag_ui_component`, ni appeler d'outil MCP métier.
# =====================================================================

# Durcissement de l'entrée : borne la taille et retire les caractères de
# contrôle (hors saut de ligne/tab). C'est une défense en profondeur qui
# protège le prompt LLM ; la couche outil (SQL_INJECTION_PATTERNS +
# _preflight_scan) et l'ORM paramétré couvrent déjà l'injection SQL.
_MAX_INPUT_LEN = 1500
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _harden_text(text: str) -> tuple[str, bool]:
    """Nettoie le texte et retourne (texte, tronqué ?) — jamais de
    troncature silencieuse (mandat §4 : signal explicite obligatoire)."""
    if not text:
        return "", False
    cleaned = _CONTROL_CHARS.sub(" ", str(text))
    truncated = len(cleaned) > _MAX_INPUT_LEN
    if truncated:
        cleaned = cleaned[:_MAX_INPUT_LEN]
    return cleaned, truncated


async def input_normalizer(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    """Normalise l'entrée utilisateur du tour — texte brut, texte normalisé,
    horodatage, compteur de tour. Rien d'autre."""
    raw_text = state.get("user_query") or state.get("transcribed_audio") or ""
    turn = int(state.get("turn_count") or 0) + 1

    updates: Dict[str, Any] = {
        "timestamp": _now(),
        "turn_count": turn,
        "final_response": None,
        "ag_ui_component": None,
        "response_strategy": None,
    }

    # Filet "formulaire fantôme" : le moteur formulaire DRY (form_node) a été
    # retiré du graphe. Un checkpoint persisté AVANT le déploiement peut encore
    # porter `active_form`/`form_step` (utilisateur au milieu d'un ancien
    # formulaire) — sans reset, `_route_after_planner` n'a plus de branche vers
    # form_node de toute façon, mais on nettoie explicitement ces champs morts
    # au tout début du tour pour éviter toute logique résiduelle (policies.py,
    # etc.) qui les consulterait encore. Compatibilité de checkpoint, pas une
    # décision métier — voir mandat §14.
    if state.get("active_form") is not None or state.get("form_step") is not None:
        updates["active_form"] = None
        updates["form_step"] = None

    # Audio transcription — seule capacité réseau tolérée ici tant que
    # l'adapter d'entrée dédié n'existe pas (mandat §4).
    audio_path = state.get("audio_file_path")
    if audio_path and not state.get("transcribed_audio"):
        transcriber = getattr(mc_runtime, "transcribe_audio", None)
        if callable(transcriber):
            try:
                maybe_coro = transcriber(audio_path)
                transcribed = (
                    await maybe_coro if inspect.isawaitable(maybe_coro) else maybe_coro
                )
                if transcribed:
                    updates["transcribed_audio"] = str(transcribed).strip()
                    raw_text = transcribed
            except Exception as audio_err:
                logger.error(
                    "[Normalizer] Échec de la transcription audio: %s",
                    audio_err,
                    exc_info=True,
                )

    raw_text, raw_truncated = _harden_text(raw_text)
    normalized, norm_truncated = _harden_text(_normalize_text(raw_text))
    updates["normalized_text"] = normalized
    updates["translated_text"] = normalized
    updates["detected_language"] = "fr"
    updates["input_truncated"] = bool(raw_truncated or norm_truncated)

    return updates


__all__ = ["input_normalizer"]
