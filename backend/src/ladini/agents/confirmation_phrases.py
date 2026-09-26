"""Vocabulaire CANONIQUE de confirmation/refus déterministe — source UNIQUE.

Historiquement défini uniquement dans `graphs/agents/market_coach/interpreter/
routing.py` (chemin déterministe `_interpret_fast_path`, gated sur
`expected_input == "CONFIRMATION"` : "un oui exact ne doit jamais coûter un
appel LLM"). Extrait ici (mandat onboarding 2026-09-26, "'ok' ne confirme pas
le profil") pour que `ladini.agents.onboarding` — un module volontairement
SANS dépendance à LangGraph/`graphs.*` (voir son propre docstring) — puisse
réutiliser EXACTEMENT le même vocabulaire sans créer un second moteur de
confirmation ni introduire de cycle d'import (`routing.py` dépend de
`ladini.agents.reducers` via `core/state.py`, donc l'inverse — `agents.*`
important `routing.py` directement — bouclerait).

Zéro dépendance ici, y compris à l'intérieur de `ladini.agents` — un import
de ce module ne doit jamais rien déclencher d'autre."""

from __future__ import annotations

_CONFIRM_EXACT_PHRASES = frozenset(
    {
        "oui", "oui oui", "ok", "ok ok", "okay", "d'accord", "daccord", "je confirme",
        "je suis d'accord", "je suis daccord", "c'est bon", "cest bon",
        "tout est bon", "ca va", "ça va", "parfait", "vas-y", "vasy", "go",
        "valide", "valider", "je valide", "confirmer",
        "confirme", "confirmé", "yes",
    }
)

_REJECT_EXACT_PHRASES = frozenset(
    {
        "non", "non non", "annule", "annuler", "stop", "pas d'accord",
        "pas daccord", "je ne confirme pas", "je refuse", "no",
    }
)

__all__ = ["_CONFIRM_EXACT_PHRASES", "_REJECT_EXACT_PHRASES"]
