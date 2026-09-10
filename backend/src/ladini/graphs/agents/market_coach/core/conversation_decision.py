"""ConversationAction — vocabulaire canonique de `cognitive_decision.action`.

`cognitive_guard` (`nodes/cognitive.py`) est l'UNIQUE propriétaire de la
décision de transition conversationnelle (voir sa docstring pour le contrat
complet). Ces constantes centralisent les valeurs que `cognitive_guard`,
`nodes/routing.py::_route_after_cognitive_guard` et `nodes/clarification.py`
partagent — plus de chaînes littérales dupliquées à 3 endroits qui
pourraient diverger silencieusement.

Legacy literal maintained for downstream compatibility : `RECOVER_ACTIVE_GOAL`
et `ABANDON_ACTIVE_GOAL` gardent leur valeur historique en minuscules
(`recover_active_tunnel`/`abandon_tunnel_max_retries`) parce que
`interpreter/strategy.py::response_strategy` (hors périmètre de ce chantier)
compare CES chaînes littérales pour décider `RECOVERY`/`CLARIFICATION`. Les
renommer casserait ce consommateur sans le refondre — voir mandat de
clôture du Bloc 1, §16.
"""

from __future__ import annotations


class ConversationAction:
    """Espace de noms simple (pas un `enum.Enum`) : les deux valeurs legacy
    doivent rester des `str` brutes comparables par égalité littérale avec
    du code hors périmètre — un `Enum` ajouterait une indirection sans
    bénéfice ici."""

    START_OR_PLAN_GOAL = "START_OR_PLAN_GOAL"
    CONTINUE_ACTIVE_GOAL = "CONTINUE_ACTIVE_GOAL"
    INTERRUPT_ACTIVE_GOAL = "INTERRUPT_ACTIVE_GOAL"
    DISAMBIGUATE = "DISAMBIGUATE"
    CLARIFY = "CLARIFY"
    # Legacy literal maintained for downstream compatibility (see module docstring).
    RECOVER_ACTIVE_GOAL = "recover_active_tunnel"
    ABANDON_ACTIVE_GOAL = "abandon_tunnel_max_retries"

    ALL = frozenset(
        {
            START_OR_PLAN_GOAL,
            CONTINUE_ACTIVE_GOAL,
            INTERRUPT_ACTIVE_GOAL,
            DISAMBIGUATE,
            CLARIFY,
            RECOVER_ACTIVE_GOAL,
            ABANDON_ACTIVE_GOAL,
        }
    )


__all__ = ["ConversationAction"]
