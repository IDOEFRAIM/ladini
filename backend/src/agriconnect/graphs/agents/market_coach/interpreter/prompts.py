"""Prompts du sous-graphe MarketCoach (Market Sense).

Templates LLM à substitution STATIQUE (pas de catalogue d'intents).
Tous les prompts respectent la règle d'or anti-hallucination des IDs :
le LLM n'a JAMAIS le droit d'inventer un `bid_id`, `auction_id`, `stock_id`.
Il extrait uniquement la désignation humaine (`selection_index` ou `selected_value`).

NB — Le PROMPT SYSTÈME de l'interpréteur n'est PAS ici : il est construit
dynamiquement (il incorpore le catalogue complet d'INTENT_CONFIG par rôle) par
``interpreter/routing.py::_build_dynamic_interpreter_prompt`` à partir de
``routing._SYSTEM_PROMPT_TEMPLATE``. C'est la SEULE source ; toute modification
du prompt système se fait là-bas. (Un ancien double statique vivait ici et avait
dérivé — sans `price_unit`, unités incomplètes — supprimé pour éviter le piège
d'éditer une copie morte.)
"""
from __future__ import annotations


# =====================================================================
# 1. INPUT INTERPRETER — USER PROMPT (le SYSTEM prompt est dans routing.py)
# =====================================================================

INTERPRETER_USER_PROMPT = """\
Contexte agent :
- current_goal : {current_goal}
- expected_input : {expected_input}{slot_hint_line}
- last_agent_question : {last_agent_question}
- expected_candidates : {expected_candidates}
- suspended_task : {suspended_task}
- panier_en_attente : {cart_pending}

Message utilisateur (déjà nettoyé et traduit en français) :
\"\"\"{normalized_text}\"\"\"

Retourne le JSON strict.
"""


# =====================================================================
# 2. GOAL PLANNER — DELIBERATELY NO PROMPT
# =====================================================================
# Le Goal Planner est PUREMENT déterministe (machine à états sur l'événement
# interprété + INTENT_CONFIG). Aucun prompt LLM n'est utilisé ni nécessaire.
# Si tu cherches le vocabulaire des intents supportés : voir `intent.INTENT_CONFIG`
# qui est la SEULE source de vérité.


__all__ = [
    "INTERPRETER_USER_PROMPT",
]
