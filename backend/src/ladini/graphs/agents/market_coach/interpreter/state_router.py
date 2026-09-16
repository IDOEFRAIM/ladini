"""State Router — INCRÉMENT A du chantier "State Router + micro-prompts"
(2026-09-12).

## Principe

```
PYTHON = connaît l'état conversationnel, les contraintes, les candidats
LLM    = comprend librement comment l'utilisateur s'exprime
PYTHON = valide, résout les IDs, contrôle les règles métier, exécute
```

Ce module ne fait QUE la première ligne : décider, à partir de l'état
CONNU par l'application (jamais du contenu linguistique du message), vers
quelle famille de micro-prompt (future) ou d'interprétation (actuelle)
router ce tour. `choose_interpretation_route` est une fonction PURE :

- elle ne fait AUCUN appel LLM ;
- elle n'analyse PAS le texte du message utilisateur ;
- elle ne déduit AUCUNE intention métier ;
- elle ne dépend PAS de `user_role` (double rôle acheteur/producteur —
  voir `interpreter/routing.py::_classifiable_intents`, jamais réintroduire
  un filtrage par rôle ici non plus).

## Portée

Introduit en Incrément A comme fonction pure/testable seule (aucun
câblage). Depuis :
- Incrément B : `SELECTION` est câblé vers `interpreter/selection_micro.py`.
- Phase C : `ACTIVE_SLOT` est câblé vers `interpreter/active_slot_micro.py`
  (voir `interpreter/routing.py` pour les deux points de branchement).
`STRUCTURED_ACTION`/`NEW_TASK` restent sur l'interpréteur unifié legacy,
qui sert aussi de filet de sécurité explicite/tracé (`legacy_fallback`,
spec §54/§26) pour SELECTION et ACTIVE_SLOT en cas d'échec infrastructurel.

## Sources d'état réutilisées (jamais de nouveau canal concurrent)

- `domain/selection_actions.py::build_selection_context` — reconstruit à
  chaque tour, depuis `vendor_selection_context`/`tier_selection_context`,
  le tunnel producteur/palier (« action structurée attendue »). C'est la
  MÊME fonction déjà utilisée par `interpreter/routing.py` pour gater le
  contrat d'action structurée — pas une resynthèse indépendante.
- `core/pending_interaction.py::get_pending_interaction` /
  `to_tunnel_category` — source canonique unique de « qu'attend-on de
  l'utilisateur ce tour ? », déjà le pont utilisé par `TunnelManager` et
  par la RÈGLE 1ter du prompt interprète actuel.
- `core/state.py::resolve_current_goal` — point de résolution unique du
  goal réellement en cours (voir sa docstring : `current_goal` peut être
  remis à `None` entre deux tours tant que `working_memory.active_goal`
  garde la mémoire du goal suspendu).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    SUBFLOW_OWNED_KINDS,
    get_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.core.slots import SLOT_FILLING_INPUTS
from ladini.graphs.agents.market_coach.core.state import resolve_current_goal
from ladini.graphs.agents.market_coach.domain.selection_actions import (
    build_selection_context,
)


class InterpretationRoute(str, Enum):
    """Famille d'interprétation choisie pour CE tour — voir le graphe cible
    du chantier (§2 de la spec) :

    ``STRUCTURED_ACTION`` > ``SELECTION`` > ``ACTIVE_SLOT`` > ``NEW_TASK``,
    dans cet ordre de priorité (une action structurée active prime toujours
    sur un menu de sélection générique, qui prime sur un simple slot
    attendu, qui prime sur une classification libre)."""

    NEW_TASK = "new_task"
    ACTIVE_SLOT = "active_slot"
    SELECTION = "selection"
    STRUCTURED_ACTION = "structured_action"

    #: Conservé pendant la migration (spec §3) — n'est jamais RENVOYÉ par
    #: `choose_interpretation_route` aujourd'hui ; un futur appelant peut
    #: l'utiliser pour marquer explicitement un repli sur l'interpréteur
    #: unifié legacy (`legacy_fallback=true` en télémétrie, spec §54).
    UNIFIED_FALLBACK = "unified_fallback"


def choose_interpretation_route(state: Dict[str, Any]) -> InterpretationRoute:
    """Décide la route d'interprétation pour ce tour — PUREMENT déterministe
    sur l'état, jamais sur le contenu linguistique du message.

    Ordre de décision (spec §5) :

    1. ``STRUCTURED_ACTION`` si le tunnel producteur/palier est actif
       (``build_selection_context(state).expected_action`` non nul) — un
       menu de paliers/producteurs affiché prime sur tout le reste : c'est
       la même priorité que `interpreter/routing.py` applique déjà avant
       même son FastPath générique (incident réel "LE PREMIER, C'EST À
       DIRE 5 L", 2026-09-01).
    2. ``SELECTION`` si `to_tunnel_category` résout à ``"SELECTION"`` (menu
       de sélection générique actif — commandes, options de menu...) et
       qu'aucune action structurée prioritaire n'a matché au point 1.
    3. ``ACTIVE_SLOT`` si un goal est en cours (`resolve_current_goal`) ET
       que la catégorie résolue est un VRAI slot métier — membre de
       `core/slots.py::SLOT_FILLING_INPUTS` (PRODUCT/PRICE/QUANTITY/UNIT/
       LOCATION/FARM_NAME/DATE/MOVEMENT_TYPE), la seule source de vérité
       pour "qu'est-ce qu'un champ à collecter" (spec Phase C §5) — jamais
       une seconde liste recopiée ici. Exclut explicitement les kinds
       possédés par un sous-flux dédié (`SUBFLOW_OWNED_KINDS` :
       CONFIRM_ACTION, PROVIDE_LOCATION, VERIFY_OTP) : `to_tunnel_category`
       résout `PROVIDE_LOCATION` vers la MÊME chaîne "LOCATION" qu'un
       simple champ `zone` (`ENTER_FIELD`), mais le premier est une
       collecte GPS pilotée par `gps_delivery_gate` — pas un simple champ
       texte à extraire par un micro-prompt générique. CONFIRMATION et
       OTP_CODE ne sont de toute façon jamais dans `SLOT_FILLING_INPUTS` —
       exclus par construction, ce garde ne change rien pour eux.
    4. ``NEW_TASK`` sinon — aucun tunnel actif (ou tunnel hors du registre
       des slots), classification libre.

    N'gère PAS le bypass onboarding (`state.get("is_onboarding")`) : dans
    l'architecture actuelle, ce bypass a lieu EN AMONT de toute décision
    d'interprétation (voir `interpreter/routing.py::_emit_onboarding`), donc
    ce router n'a pas besoin de le dupliquer — il ne doit être consulté que
    lorsqu'une interprétation est réellement nécessaire.
    """
    selection_context = build_selection_context(state)
    if selection_context is not None and selection_context.expected_action:
        return InterpretationRoute.STRUCTURED_ACTION

    pending = get_pending_interaction(state)
    tunnel_category = to_tunnel_category(pending)

    if tunnel_category == "SELECTION":
        return InterpretationRoute.SELECTION

    current_goal = resolve_current_goal(state)
    if (
        current_goal
        and pending.kind not in SUBFLOW_OWNED_KINDS
        and tunnel_category in SLOT_FILLING_INPUTS
    ):
        return InterpretationRoute.ACTIVE_SLOT

    return InterpretationRoute.NEW_TASK


__all__ = ["InterpretationRoute", "choose_interpretation_route"]
