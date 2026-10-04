"""B20 — ARBITRAGE DU CONTEXTE CONVERSATIONNEL : à quel contexte appartient CE message ?

Un menu n'est pas une prison. Avant B20, la réponse d'un utilisateur était attribuée par le *premier* contexte encore
présent dans l'état (menu de commandes, pending, `working_memory`), puis laissée à des jugements LLM successifs
(micro-prompt SELECTION puis NEW_TASK) pour décider s'il s'agissait d'une « interruption ». Conséquences observées en
production : « voir les détails » (réponse au menu d'un DIGEST récurrent — message PROACTIF, donc absent de l'état)
partait vers la liste des commandes ; « mes besoins » restait prisonnier d'un ancien menu `SELECTION_MENU` de commandes ;
« YUP » était repris par ce vieux menu alors qu'un nouveau message interactif avait été envoyé depuis.

Cette primitive centrale, PURE et déterministe (aucun appel LLM, aucun I/O), décide dans cet ordre :

  1. ``ACTIVE_MENU_ACTION``       réponse au menu/à l'interaction SORTANTE interactive la plus récente (alias fermés, résolus
                                  dans le contexte du menu propriétaire — jamais une phrase globale) ;
  2. ``INTERRUPT_WITH_NEW_GOAL``  commande de navigation explicite (« mes besoins », « mes commandes ») ou nouvelle demande
                                  explicite (« je veux acheter/vendre ... ») alors qu'un ancien MENU est affiché ;
  3. ``SUPERSEDE_STALE_CONTEXT``  un message interactif sortant plus récent a pris la main sur un ancien menu : l'ancien
                                  contexte est abandonné, la classification continue sans lui ;
  4. ``ACTIVE_SLOT``              un vrai tunnel/slot vivant : comportement existant (State Router + micro-prompts) ;
  5. ``GENERIC_CLASSIFICATION``   aucun contexte : classification libre existante.

Contrat « sortant » : seuls les messages INTERACTIFS (qui attendent une réponse nue — `templates.INTERACTIVE_TEMPLATE_OWNERS`)
deviennent propriétaires de la réponse suivante ; une notification informative ne supplante jamais un menu actif. Un
chiffre nu ne détourne jamais un slot de données (quantité, prix...) vers un menu sortant : seuls les alias TEXTUELS le
font ; un menu affiché (sélection) reste, lui, soumis à la règle « le plus récent gagne ».

Durée de vie : un menu vit au plus son TTL existant (`PENDING_INTERACTION_TTL_SECONDS`, appliqué par
`get_pending_interaction`) ; un message sortant interactif au plus `RECURRING_SUPPLY_DIGEST_PENDING_TTL_SECONDS`.
Un « menu fantôme » (mapping/candidats sans pending vivant) est un reliquat périmé : il est purgé, jamais ressuscité.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Mapping, Optional

from ladini.agents.reducers import mark_deleted
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
)
from ladini.graphs.agents.market_coach.nodes.cleanup import (
    _GENERIC_SELECTION_KEYS,
    _MENU_CACHE_KEYS,
)

logger = logging.getLogger("Ladini.Market.ContextArbitration")


class ArbitrationKind(str, Enum):
    ACTIVE_MENU_ACTION = "ACTIVE_MENU_ACTION"
    INTERRUPT_WITH_NEW_GOAL = "INTERRUPT_WITH_NEW_GOAL"
    SUPERSEDE_STALE_CONTEXT = "SUPERSEDE_STALE_CONTEXT"
    ACTIVE_SLOT = "ACTIVE_SLOT"
    GENERIC_CLASSIFICATION = "GENERIC_CLASSIFICATION"


#: Kinds qui sont des MENUS (l'utilisateur choisit parmi des options affichées).
MENU_KINDS: FrozenSet[InteractionKind] = frozenset(
    {
        InteractionKind.SELECTION_MENU,
        InteractionKind.SELECT_PRODUCER,
        InteractionKind.SELECT_PRICING_TIER,
        InteractionKind.CLARIFY_INTENT,
    }
)
#: Kinds qui sont des slots de DONNÉES (un nombre/texte attendu : prix, quantité, champ...).
SLOT_KINDS: FrozenSet[InteractionKind] = frozenset(
    {InteractionKind.ENTER_QUANTITY, InteractionKind.ENTER_PACKAGE_COUNT, InteractionKind.ENTER_FIELD}
)
#: Kinds où une navigation explicite peut interrompre (menus + slots). Les sous-flux (confirmation, GPS, OTP) ne sont
#: jamais interrompus par une simple navigation : leur sortie reste « annuler ».
INTERRUPTIBLE_KINDS: FrozenSet[InteractionKind] = MENU_KINDS | SLOT_KINDS
#: Menus génériques (listes acheteur : commandes, besoins...) sur lesquels une NOUVELLE demande explicite (« je veux acheter ... »)
#: ou « annuler » est une sortie certaine. Les tunnels producteur/palier (garde de changement de produit, B6/B8) et la
#: clarification d'intention (`CLARIFY_INTENT`, annulation propre au flux) ont leur propre gestion — non touchés.
GENERIC_MENU_KINDS: FrozenSet[InteractionKind] = frozenset({InteractionKind.SELECTION_MENU})


# ── Vocabulaire FERMÉ (comparé après normalisation : minuscules, sans accents ni ponctuation) ───────────────────────
def fold(text: Any) -> str:
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9' ]+", " ", s).replace("'", " ")
    return re.sub(r"\s+", " ", s).strip()


#: Alias d'une ACTION de menu sortant (résolus UNIQUEMENT dans le contexte du menu qui la propose).
MENU_ACTION_ALIASES: Dict[str, FrozenSet[str]] = {
    "VIEW_DETAILS": frozenset(
        {"voir les details", "voir details", "voir le detail", "voir detail", "les details", "details", "detail",
         "le detail", "voir les detail"}
    ),
    "MY_NEEDS": frozenset(
        {"mes besoins", "voir mes besoins", "voir les besoins", "liste de mes besoins", "mes besoins recurrents"}
    ),
}

#: Alias textuels des actions d'un menu RÉCURRENT vivant (`working_memory.recurring_need_menu.actions`) — B21.
RECURRING_MENU_TEXT_ALIASES: Dict[str, FrozenSet[str]] = {
    "CONFIRM": frozenset({"accepter", "j accepte", "accepter la proposition", "confirmer"}),
    "REJECT": frozenset({"refuser", "je refuse", "refuser la proposition", "pas cette fois"}),
    "REFRESH": frozenset({"actualiser", "rechercher", "rechercher maintenant", "rechercher a nouveau", "chercher maintenant"}),
    "VIEW": frozenset({"voir la prochaine livraison", "prochaine livraison"}),
    "ORDERS": frozenset({"voir les commandes", "voir la commande"}),
    "LIST": frozenset({"retour"}),
}
_RECURRING_MENU_TTL_SECONDS = 600.0  # = `flows/buyer/recurring_need._MENU_TTL_SECONDS`

#: Navigation explicite ACHETEUR -> intent. Phrases complètes uniquement (jamais une sous-chaîne).
NAVIGATION_INTENTS: Dict[str, FrozenSet[str]] = {
    "GET_MY_NEEDS": frozenset(
        {"mes besoins", "voir mes besoins", "liste de mes besoins", "mes besoins recurrents", "voir les besoins",
         "mes approvisionnements", "mes approvisionnements recurrents", "voir mes besoins recurrents",
         "voir mes approvisionnements"}
    ),
    "BUYER_LIST_ORDERS": frozenset(
        {"mes commandes", "voir mes commandes", "liste de mes commandes", "suivi de mes commandes",
         "mes commandes en cours", "suivre mes commandes"}
    ),
}

#: Sortie explicite d'un menu générique (le micro-prompt SELECTION les traite déjà comme interruption certaine).
MENU_CANCEL_WORDS: FrozenSet[str] = frozenset({"annuler", "annule", "quitter", "laisse tomber", "laisser tomber"})

_NEW_GOAL_BUY = re.compile(
    r"^(?:je veux|je voudrais|j aimerais|jaimerais|je souhaite|je cherche|il me faut)\s+"
    r"(?:acheter|commander|achete|du|de la|de l|des|un|une)\b.+"
)
_NEW_GOAL_SELL = re.compile(r"^(?:je veux|je voudrais|j aimerais|jaimerais|je souhaite)\s+(?:vendre|mettre en vente)\b.+")


# ── Données d'entrée / sortie ───────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class InteractiveOutbound:
    """Dernier message sortant interactif (cf. `get_last_interactive_outbound`)."""

    owner_type: str
    sent_at: float
    menu_id: Optional[str] = None
    actions: Optional[Mapping[str, str]] = None
    recurring_need_ids: tuple = ()
    occurrence_ids: tuple = ()

    @staticmethod
    def from_tool_result(result: Any) -> Optional["InteractiveOutbound"]:
        if not isinstance(result, dict) or result.get("status") != "success":
            return None
        data = result.get("interactive")
        if not isinstance(data, dict) or not data.get("owner_type"):
            return None
        try:
            return InteractiveOutbound(
                owner_type=str(data["owner_type"]),
                sent_at=float(data["sent_at"]),
                menu_id=data.get("menu_id"),
                actions=dict(data["actions"]) if isinstance(data.get("actions"), dict) else None,
                recurring_need_ids=tuple(data.get("recurring_need_ids") or ()),
                occurrence_ids=tuple(data.get("occurrence_ids") or ()),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class ArbitrationDecision:
    kind: ArbitrationKind
    reason: str
    #: Résultat d'interprétation déterministe (format legacy de l'interpréteur) ; `None` : l'appelant poursuit.
    raw: Optional[Dict[str, Any]] = None
    #: Le message doit être reclassifié (NEW_TASK) SANS l'ancien contexte.
    reclassify: bool = False
    #: Le contexte interactif d'état (menu/pending/goal verrouillé) est abandonné pour ce tour.
    purge: bool = False
    old_context: Optional[str] = None
    new_context: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


# ── Détection de l'état ─────────────────────────────────────────────────────────────────────────────────────────────
def _has_ghost_menu(state: Mapping[str, Any]) -> bool:
    """Reliquat de menu (mapping/candidats) sans pending vivant : périmé par construction."""
    return bool(state.get("available_mapping") or state.get("expected_candidates"))


def _is_bare_digit(norm: str) -> bool:
    return norm.isdigit()


def is_recurring_navigation(norm: str) -> bool:
    """`norm` (déjà `fold`é) est une navigation explicite vers la liste des besoins récurrents."""
    return norm in NAVIGATION_INTENTS["GET_MY_NEEDS"]


def resolve_menu_action(norm: str, actions: Mapping[str, str], *, allow_digits: bool) -> Optional[str]:
    """Action du menu propriétaire visée par `norm` (chiffre du menu ou alias fermé) ; `None` sinon."""
    if allow_digits and norm in actions:
        return str(actions[norm])
    allowed = set(actions.values())
    for action, aliases in MENU_ACTION_ALIASES.items():
        if action in allowed and norm in aliases:
            return action
    return None


def stale_context_purge_patch(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Patch d'état qui abandonne TOUT contexte interactif périmé (réutilise les univers de clés de `cleanup`)."""
    wm: Dict[str, Any] = dict(state.get("working_memory") or {})
    mark_deleted(wm, "active_goal", "step_index", "recurring_need_menu", *_GENERIC_SELECTION_KEYS, *_MENU_CACHE_KEYS)
    return {
        "current_goal": None,
        "pending_interaction": None,
        "missing_fields": [],
        "last_missing_field": None,
        "expected_candidates": [],
        "available_mapping": {},
        "pending_menu": None,
        "vendor_selection_context": {"__reset__": True},
        "tier_selection_context": {"__reset__": True},
        "working_memory": wm,
        "status": "PLANNING",
    }


def neutral_state_view(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Vue de l'état SANS le contexte interactif périmé, pour (re)classifier le message librement."""
    wm = {k: v for k, v in (state.get("working_memory") or {}).items()
          if k not in {"active_goal", "step_index", "recurring_need_menu", *_GENERIC_SELECTION_KEYS, *_MENU_CACHE_KEYS}}
    return {
        **state,
        "current_goal": None,
        "pending_interaction": None,
        "expected_candidates": [],
        "available_mapping": {},
        "working_memory": wm,
        "vendor_selection_context": {"__reset__": True},
        "tier_selection_context": {"__reset__": True},
    }


def _raw(intent: str, entities: Dict[str, Any], path: str, **analysis: Any) -> Dict[str, Any]:
    return {
        "interpreted_event": "NEW_TASK",
        "detected_intent": intent,
        "interpreter_confidence": 0.99,
        "extracted_entities": entities,
        "raw_analysis": {"path": path, **analysis},
    }


def _recurring_menu_selection(state: Mapping[str, Any], pending: Any, norm: str) -> Optional[tuple]:
    """`(index, action)` si `norm` désigne une entrée du menu récurrent VIVANT (menu de CE tour : même goal, TTL respecté)."""
    if pending.kind != InteractionKind.SELECTION_MENU or str(pending.goal or "") != "GET_MY_NEEDS" or not norm:
        return None
    menu = (state.get("working_memory") or {}).get("recurring_need_menu")
    if not isinstance(menu, dict) or not isinstance(menu.get("actions"), dict):
        return None
    if time.time() - float(menu.get("created_at") or 0) > _RECURRING_MENU_TTL_SECONDS:
        return None
    actions: Mapping[str, str] = menu["actions"]
    if norm.isdigit():
        return (int(norm), str(actions[norm])) if norm in actions else None
    for action, aliases in RECURRING_MENU_TEXT_ALIASES.items():
        starts = {"CONFIRM": "accepter ", "REJECT": "refuser "}.get(action)
        if norm in aliases or (starts and norm.startswith(starts)):
            for key, value in actions.items():
                if value == action:
                    return int(key), action
    return None


def live_menu_view(state: Mapping[str, Any], *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """B23 — le menu récurrent VIVANT vu comme une ATTENTE : `{"title", "labels": [...], "actions": {index: action}}`.

    Il guide l'interprétation d'un message libre (le micro-prompt SELECTION voit ce que l'écran propose) ; il ne décide
    jamais de l'intention. `None` : pas de menu récurrent vivant (autre goal, périmé, ou aucun)."""
    pending = get_pending_interaction(dict(state))
    if pending.kind != InteractionKind.SELECTION_MENU or str(pending.goal or "") != "GET_MY_NEEDS":
        return None
    menu = (state.get("working_memory") or {}).get("recurring_need_menu")
    if not isinstance(menu, dict) or not isinstance(menu.get("actions"), dict):
        return None
    now = time.time() if now is None else now
    if now - float(menu.get("created_at") or 0) > _RECURRING_MENU_TTL_SECONDS:
        return None
    labels: Dict[str, Any] = menu["labels"] if isinstance(menu.get("labels"), dict) else {}
    ordered = sorted((k for k in menu["actions"] if str(k).isdigit()), key=int)
    return {
        "title": str(menu.get("title") or ""),
        "labels": [str(labels.get(k) or menu["actions"][k]) for k in ordered],
        "actions": {str(k): str(menu["actions"][k]) for k in ordered},
    }


#: Actions d'un menu récurrent qui MODIFIENT l'état métier (accepter/refuser une proposition) : jamais déclenchées par une
#: réponse en langage libre — uniquement par un numéro ou un alias FERMÉ du menu (étape 1b de l'arbitrage).
MUTATING_MENU_ACTIONS: FrozenSet[str] = frozenset({"CONFIRM", "REJECT"})


def guard_free_text_selection(state: Mapping[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """Fail-safe B23 : une SÉLECTION issue du micro-prompt (langage libre) qui désigne une entrée MUTANTE d'un menu récurrent
    est marquée `closed_reply_required` (le flow demande alors un numéro/alias fermé). Hors cas : `result` inchangé."""
    if str(result.get("interpreted_event") or "").upper() != "SELECTION":
        return result
    view = live_menu_view(state)
    index = (result.get("extracted_entities") or {}).get("selection_index")
    if view is None or index is None or view["actions"].get(str(index)) not in MUTATING_MENU_ACTIONS:
        return result
    return {**result, "extracted_entities": {**result["extracted_entities"], "closed_reply_required": True},
            "raw_analysis": {**(result.get("raw_analysis") or {}), "guard": "mutation_requires_closed_reply"}}


# ── La primitive centrale ───────────────────────────────────────────────────────────────────────────────────────────
def resolve_conversation_context(
    state: Mapping[str, Any],
    text: str,
    *,
    role: str,
    outbound: Optional[InteractiveOutbound] = None,
    now: Optional[float] = None,
    buyer_capable: bool = False,
) -> ArbitrationDecision:
    """Décide à quel contexte appartient `text`. Pure : `outbound` et `buyer_capable` sont fournis par l'appelant (lecture
    DB). `buyer_capable` (B21.1) : l'acteur possède la CAPACITÉ acheteur (profil acheteur, ou administrateur) alors que le
    graphe courant n'est pas le graphe BUYER — voir `is_recurring_navigation`."""
    now = time.time() if now is None else now
    norm = fold(text)
    pending = get_pending_interaction(dict(state))
    live = pending.kind != InteractionKind.NONE
    ghost = (not live) and _has_ghost_menu(state)
    old_ctx = pending.kind.value if live else ("GHOST_MENU" if ghost else None)
    role_up = str(role or "").upper()

    # 1. Interaction SORTANTE interactive la plus récente (plus récente que tout ce que l'état a posé).
    #    Un contexte d'état DÉRIVÉ (ex. menu producteurs reconstruit depuis `vendor_selection_context`) n'a pas d'horodatage
    #    (`created_at == 0`) : son âge est inconnu, il n'est jamais supplanté par âge — seul un alias TEXTUEL fermé du menu
    #    sortant peut alors lui être préféré (jamais un chiffre nu).
    state_ts = float(pending.created_at or 0) if live else 0.0
    age_known = (not live) or state_ts > 0
    newer = (not live) or (age_known and outbound is not None and outbound.sent_at > state_ts)
    if outbound is not None and norm and (newer or not age_known):
        if outbound.actions:
            allow_digits = newer and ((not live) or pending.kind in MENU_KINDS)
            action = resolve_menu_action(norm, outbound.actions, allow_digits=allow_digits)
            if action == "VIEW_DETAILS":
                entities = {"digest_action": "VIEW_DETAILS", "recurring_need_ids": list(outbound.recurring_need_ids),
                            "menu_id": outbound.menu_id}
                return ArbitrationDecision(
                    ArbitrationKind.ACTIVE_MENU_ACTION, "outbound_menu_reply:VIEW_DETAILS",
                    raw=_raw("GET_MY_NEEDS", entities, "context_arbitration_outbound_menu", owner=outbound.owner_type,
                             action=action),
                    purge=live or ghost, old_context=old_ctx, new_context=outbound.owner_type,
                )
            if action == "MY_NEEDS":
                return ArbitrationDecision(
                    ArbitrationKind.ACTIVE_MENU_ACTION, "outbound_menu_reply:MY_NEEDS",
                    raw=_raw("GET_MY_NEEDS", {"digest_action": "MY_NEEDS", "menu_id": outbound.menu_id},
                             "context_arbitration_outbound_menu", owner=outbound.owner_type, action=action),
                    purge=live or ghost, old_context=old_ctx, new_context=outbound.owner_type,
                )
        # Message sortant plus récent qu'un ancien MENU : l'ancien menu ne possède plus la réponse suivante (sauf si le
        # message est un chiffre nu valide pour ce menu : on ne casse pas « mes commandes -> 2 »).
        if newer and ((live and pending.kind in MENU_KINDS and not _is_bare_digit(norm)) or ghost):
            return ArbitrationDecision(
                ArbitrationKind.SUPERSEDE_STALE_CONTEXT, "newer_interactive_outbound", reclassify=True, purge=True,
                old_context=old_ctx, new_context=outbound.owner_type,
            )

    # 1b. Menu RÉCURRENT vivant (liste des besoins / écran d'un besoin) : chiffre ou alias fermé -> l'entrée du menu,
    #     SANS LLM. Une navigation explicite (« mes commandes ») garde la priorité sur les alias (voir 2).
    recurring = _recurring_menu_selection(state, pending, norm) if live else None
    if recurring is not None and norm not in {p for ps in NAVIGATION_INTENTS.values() for p in ps}:
        index, action = recurring
        return ArbitrationDecision(
            ArbitrationKind.ACTIVE_MENU_ACTION, f"recurring_menu_reply:{action}",
            raw={"interpreted_event": "SELECTION", "detected_intent": "GET_MY_NEEDS", "interpreter_confidence": 0.99,
                 "extracted_entities": {"selection_index": index},
                 "raw_analysis": {"path": "context_arbitration_recurring_menu", "action": action}},
            old_context=old_ctx, new_context="RECURRING_MENU",
        )

    # 2. Navigation / nouvelle demande EXPLICITE. Une navigation acheteur (« mes besoins », « mes commandes ») est
    #    déterministe QUEL QUE SOIT le contexte : elle interrompt un ancien menu/slot, et évite sinon un jugement LLM.
    interruptible = ghost or (live and pending.kind in INTERRUPTIBLE_KINDS)
    if norm and role_up == "BUYER" and (interruptible or not live):
        for intent, phrases in NAVIGATION_INTENTS.items():
            if norm in phrases:
                return ArbitrationDecision(
                    ArbitrationKind.INTERRUPT_WITH_NEW_GOAL, f"explicit_navigation:{intent}",
                    raw=_raw(intent, {}, "context_arbitration_navigation", navigation=intent),
                    purge=bool(old_ctx), old_context=old_ctx, new_context=intent,
                )
    # B21.1 — « mes besoins » est une fonction ACHETEUR, pas une propriété du graphe courant : un utilisateur de profil
    # ADMIN (graphe PRODUCER par défaut, voir `orchestrator._run_market`) ou producteur ET acheteur qui possède la
    # capacité acheteur obtient la même route déterministe. Les autres navigations acheteur (« mes commandes »,
    # ambiguë côté producteur) restent réservées au graphe BUYER. Un slot de données producteur (prix, quantité...) n'est
    # jamais interrompu ici : seuls un menu ou l'absence de contexte le sont.
    if norm and role_up != "BUYER" and buyer_capable and is_recurring_navigation(norm):
        if ghost or not live or pending.kind in MENU_KINDS:
            return ArbitrationDecision(
                ArbitrationKind.INTERRUPT_WITH_NEW_GOAL, "explicit_navigation:GET_MY_NEEDS:buyer_capability",
                raw=_raw("GET_MY_NEEDS", {}, "context_arbitration_navigation", navigation="GET_MY_NEEDS"),
                purge=bool(old_ctx), old_context=old_ctx, new_context="GET_MY_NEEDS",
            )
    if norm and interruptible and role_up in {"BUYER", "PRODUCER"}:
        if (ghost or pending.kind in GENERIC_MENU_KINDS) and (_NEW_GOAL_BUY.match(norm) or _NEW_GOAL_SELL.match(norm)):
            return ArbitrationDecision(
                ArbitrationKind.INTERRUPT_WITH_NEW_GOAL, "explicit_new_goal_request", reclassify=True, purge=True,
                old_context=old_ctx, new_context="NEW_REQUEST",
            )

    # 2b. « annuler » face à un menu générique : sortie propre (REJECT du goal du menu ; le planificateur purge le menu).
    if norm in MENU_CANCEL_WORDS and live and pending.kind in GENERIC_MENU_KINDS:
        return ArbitrationDecision(
            ArbitrationKind.ACTIVE_MENU_ACTION, "menu_cancel",
            raw={"interpreted_event": "REJECT", "detected_intent": str(pending.goal or state.get("current_goal") or "UNKNOWN").upper(),
                 "interpreter_confidence": 0.99, "extracted_entities": {},
                 "raw_analysis": {"path": "context_arbitration_menu_cancel"}},
            old_context=old_ctx, new_context="CANCEL",
        )

    # 3. Reliquat de menu sans pending vivant : jamais ressuscité.
    if ghost:
        return ArbitrationDecision(
            ArbitrationKind.SUPERSEDE_STALE_CONTEXT, "ghost_menu_without_live_pending", reclassify=True, purge=True,
            old_context=old_ctx, new_context=None,
        )

    if live:
        return ArbitrationDecision(ArbitrationKind.ACTIVE_SLOT, "live_pending_context", old_context=old_ctx)
    return ArbitrationDecision(ArbitrationKind.GENERIC_CLASSIFICATION, "no_interactive_context")


def log_decision(decision: ArbitrationDecision, *, outbound: Optional[InteractiveOutbound], now: Optional[float] = None) -> None:
    """Observabilité (aucune PII : jamais le texte utilisateur, seulement types/décisions/âges)."""
    now = time.time() if now is None else now
    age = round(now - outbound.sent_at, 1) if outbound is not None else None
    logger.info(
        "CONVERSATION_CONTEXT_ARBITRATED decision=%s reason=%s old_context=%s new_context=%s outbound_age_s=%s",
        decision.kind.value, decision.reason, decision.old_context, decision.new_context, age,
    )
    if decision.kind == ArbitrationKind.INTERRUPT_WITH_NEW_GOAL:
        logger.info("EXPLICIT_GOAL_INTERRUPTION old_context=%s new_context=%s", decision.old_context, decision.new_context)
    if decision.purge and decision.old_context:
        logger.info("MENU_CONTEXT_SUPERSEDED old_context=%s by=%s", decision.old_context, decision.new_context)
    if decision.kind == ArbitrationKind.SUPERSEDE_STALE_CONTEXT:
        logger.info("STALE_MENU_IGNORED old_context=%s reason=%s", decision.old_context, decision.reason)
    if decision.kind == ArbitrationKind.ACTIVE_MENU_ACTION:
        logger.info("MENU_CONTEXT_CONSUMED owner=%s", decision.new_context)


def log_intent_arbitration(state: Mapping[str, Any], *, semantic_intent: Any, relation: str, route: str, reason: str) -> None:
    """B23 — journal de décision STRUCTURÉ « attente vs intention » (aucune PII : jamais le texte, seulement des codes).

    `relation` : ANSWER (réponse à l'attente) | NEW_TASK (nouvelle intention qui supersede le menu) | UNRELATED (hors
    domaine) | UNRESOLVED (clarification). `expected_action` : ce que l'écran actif attend."""
    pending = get_pending_interaction(dict(state))
    view = live_menu_view(state)
    expected = "|".join(sorted(set(view["actions"].values()))) if view else pending.kind.value
    goal = state.get("current_goal") or pending.goal or "NONE"
    logger.info(
        "INTENT_ARBITRATION current_goal=%s expected_action=%s semantic_intent=%s relation_to_expectation=%s "
        "selected_route=%s reason=%s",
        goal, expected, semantic_intent or "UNKNOWN", relation, route, reason,
    )


__all__ = [
    "ArbitrationDecision",
    "MUTATING_MENU_ACTIONS",
    "guard_free_text_selection",
    "live_menu_view",
    "log_intent_arbitration",
    "ArbitrationKind",
    "InteractiveOutbound",
    "is_recurring_navigation",
    "log_decision",
    "neutral_state_view",
    "resolve_conversation_context",
    "resolve_menu_action",
    "stale_context_purge_patch",
]
