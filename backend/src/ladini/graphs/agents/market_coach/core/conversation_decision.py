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
    # (2026-09-30, "interruption d'une confirmation active") : une nouvelle intention
    # EXPLICITE du MÊME type de goal que celui déjà verrouillé (ex: SALES_PUBLISH_PRODUCT
    # pour un autre produit) arrive pendant une CONFIRM_ACTION sur un draft canonique versionné
    # (`core/goals.py::DRAFT_BASED_CONFIRMATION_GOALS`). Ni une correction du draft actif, ni
    # une intention assez différente pour déclencher INTERRUPT_ACTIVE_GOAL (même nom de goal —
    # voir la condition d'exclusion de ce dernier). Ne bascule JAMAIS automatiquement (mandat
    # §A3) : pose une question fermée oui/non, voir `nodes/cognitive.py`.
    ASK_SWITCH_CONFIRMATION = "ASK_SWITCH_CONFIRMATION"
    # (2026-10-01, Étape 9A/9B — ambiguïté hors tunnel) : des FAITS métier
    # clairs (produit/quantité) mais AUCUN tunnel actif et AUCUN signal
    # d'action qui départage plusieurs intentions du catalogue également
    # plausibles (ex: "j'ai 90 L de miel" — vendre ? enregistrer en stock ?).
    # Jamais un goal choisi au hasard (mandat §9) : pose une clarification
    # ciblée, préserve les faits+candidats dans `pending_interaction`
    # (`InteractionKind.CLARIFY_INTENT`), sans jamais verrouiller `current_
    # goal` ni créer de draft (voir `nodes/cognitive.py`).
    ASK_INTENT_SELECTION = "ASK_INTENT_SELECTION"
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
            ASK_SWITCH_CONFIRMATION,
            ASK_INTENT_SELECTION,
            RECOVER_ACTIVE_GOAL,
            ABANDON_ACTIVE_GOAL,
        }
    )


__all__ = ["ConversationAction"]
