"""Prompt dédié du micro-prompt SELECTION (chantier "State Router +
micro-prompts", INCRÉMENT B, 2026-09-12).

Volontairement PETIT et SPÉCIALISÉ (spec §5/§18) : contrairement au gros
prompt unifié (`_build_dynamic_interpreter_prompt` dans `routing.py`), il ne
contient JAMAIS le catalogue des 41 intentions, les règles quantity/price,
les règles de dates, ni le schéma universel — seulement ce qui est
nécessaire pour comprendre une réponse à un menu déjà affiché. Le LLM garde
l'entière responsabilité de la compréhension linguistique libre (ordinaux,
descriptions, changements d'avis) ; ce module ne fait AUCUNE énumération de
formulations possibles."""

from __future__ import annotations

from typing import List

# À incrémenter à CHAQUE changement comportemental de ce prompt (nouvelle
# règle, contrat JSON modifié) — composante de la clé de cache LLM
# (`interpreter/selection_micro.py::_cache_key`) ET dimension Langfuse
# (`prompt_version`), pour comparer AVANT/APRÈS un changement de prompt.
# Distincte de `INTERPRETER_PROMPT_VERSION` (routing.py) — chaque famille de
# micro-prompt a sa propre version (spec §19).
# v4 (B27) : lecture NATURELLE d'une réponse de menu — accord/refus libres de l'option affichée, références de date
# (extraites, jamais calculées), certitude ; aucune liste de phrases.
SELECTION_PROMPT_VERSION = "selection_v4"

SELECTION_SYSTEM_PROMPT = (
    "Tu interprètes, dans une conversation WhatsApp au Burkina Faso, la "
    "réponse d'un utilisateur à une liste de choix déjà affichée. Tu ne "
    "classifies AUCUNE intention métier — seulement si ce message répond au "
    "menu, l'interrompt, ou reste ambigu. Réponds UNIQUEMENT avec un objet "
    "JSON valide, sans aucun texte autour."
)

_USER_PROMPT_TEMPLATE = (
    "But métier actif : {current_goal}\n"
    "Question précédemment posée : {last_agent_question}\n"
    "Options proposées :\n{candidates_block}\n"
    'Message utilisateur : "{normalized_text}"\n'
    "\n"
    "Détermine UN SEUL évènement :\n"
    "- SELECTION : le message désigne clairement une des options ci-dessus, "
    "explicitement (\"le deuxième\", \"celui de Diallo\") ou implicitement "
    "(\"le moins cher\", \"le gros bidon\", \"finalement le premier\").\n"
    "- INTERRUPTION : le message démarre clairement une demande DIFFÉRENTE, "
    "sans rapport avec les options proposées (ex: le menu liste des "
    'commandes et le message dit "je veux vendre du maïs"). Une nouvelle '
    "demande COMPLÈTE (un produit, une quantité, une fréquence...) n'est "
    "jamais une sélection, même quand le menu parle du même domaine ou du "
    "même produit (ex: l'écran affiche le besoin « Bœuf » et le message "
    'demande 2 chèvres chaque semaine). Un verbe '
    "d'action seul, qui ne décrit ni ne désigne AUCUNE des options "
    'affichées (ex: "confirmer", "annuler", "je confirme"), est TOUJOURS '
    "une interruption — même s'il n'y a qu'une seule option affichée, "
    "même si ce verbe pourrait sembler s'appliquer à \"une commande\" en "
    "général : sans référence explicite ou implicite à CETTE option "
    "précise, ce n'est pas une sélection. EXCEPTION : quand une option affichée "
    "est elle-même un accord ou un refus de ce que l'écran propose (« Confirmer », "
    "« Pas cette fois »...), un message qui exprime NATURELLEMENT cet accord ou "
    "ce refus pour ce qui est affiché, SANS rien ajouter ni modifier, est la "
    "SÉLECTION de cette option — quelle que soit la formulation. Un accord qui "
    "ajoute, change ou conditionne quelque chose (une quantité, une date, un "
    "autre produit : « oui mais… ») n'est PAS un accord : c'est une INTERRUPTION.\n"
    "- UNKNOWN : trop ambigu pour choisir une option ou conclure à une "
    'interruption (ex: "celui-là" sans repère suffisant, "l\'autre" avec '
    "plus de deux options).\n"
    "\n"
    "Pour SELECTION :\n"
    '- utilise "selection_index" (entier, 1-based) si tu identifies '
    "exactement UNE option ;\n"
    '- sinon utilise "selected_value" (texte court, la désignation humaine '
    "utile — jamais un identifiant technique) ;\n"
    "- ne renseigne jamais les deux à la fois, n'invente aucun identifiant ;\n"
    "- \"confidence\" (0.0 à 1.0) : ta certitude que CE message désigne cette option ;\n"
    "- si le message désigne une option par sa DATE (« demain », « celui du 5 »), "
    "renseigne \"date_offset_days\" (entier : 0 aujourd'hui, 1 demain...) OU "
    "\"date_day\"/\"date_month\" (le jour et le mois DITS) — jamais une date "
    "calculée : le système la calcule. \"date_role\" : START si la date est celle "
    "du DÉMARRAGE (« celui qui commence le 5 »), DELIVERY si c'est une LIVRAISON "
    "(« celle de demain »), sinon null.\n"
    'Pour INTERRUPTION et UNKNOWN : "selection_index" et "selected_value" '
    "doivent être null.\n"
    "\n"
    "Réponds strictement avec cet objet JSON, sans aucun autre texte :\n"
    '{{"event": "SELECTION|INTERRUPTION|UNKNOWN", "selection_index": '
    '<entier ou null>, "selected_value": "<texte ou null>", "confidence": '
    '<0.0 à 1.0>, "date_offset_days": <entier ou null>, "date_day": <1-31 ou '
    'null>, "date_month": <1-12 ou null>, "date_role": "<START|DELIVERY|null>"}}'
)

_REPAIR_PROMPT_TEMPLATE = (
    "Ta réponse précédente est invalide : {reason}\n"
    "\n"
    "Contraintes :\n"
    '- "event" doit être SELECTION, INTERRUPTION ou UNKNOWN.\n'
    "- si SELECTION : \"selection_index\" (entier entre 1 et "
    '{num_candidates}) OU "selected_value" (texte), jamais les deux, '
    "jamais aucun des deux.\n"
    '- si INTERRUPTION ou UNKNOWN : "selection_index" et "selected_value" '
    "doivent être null.\n"
    "\n"
    "Corrige uniquement le JSON, sans texte autour."
)


def _format_candidates(candidates: List[str]) -> str:
    if not candidates:
        return "(aucune option listée)"
    return "\n".join(f"{i}. {label}" for i, label in enumerate(candidates, start=1))


def build_selection_user_prompt(
    *,
    current_goal: str,
    last_agent_question: str,
    candidates: List[str],
    normalized_text: str,
) -> str:
    return _USER_PROMPT_TEMPLATE.format(
        current_goal=current_goal or "AUCUN",
        last_agent_question=last_agent_question or "—",
        candidates_block=_format_candidates(candidates),
        normalized_text=normalized_text,
    )


def build_selection_repair_prompt(*, num_candidates: int, reason: str) -> str:
    return _REPAIR_PROMPT_TEMPLATE.format(
        reason=reason, num_candidates=max(num_candidates, 1)
    )


__all__ = [
    "SELECTION_PROMPT_VERSION",
    "SELECTION_SYSTEM_PROMPT",
    "build_selection_user_prompt",
    "build_selection_repair_prompt",
]
