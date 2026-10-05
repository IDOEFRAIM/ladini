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
from datetime import date, timedelta
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

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


class RelationToContext(str, Enum):
    """B24 — comment CE message se rapporte à la tâche courante. Dérivé de signaux STRUCTURÉS (événement, intention, cible),
    jamais d'une liste de phrases. Mêmes concepts que l'existant (SELECTION/ANSWER, UPDATE, NEW_TASK, INTERRUPTION,
    OUT_OF_SCOPE, UNKNOWN), nommés une seule fois :

    ``ANSWER``       réponse directe à l'attente courante (index/alias fermé, action contextuelle sur la cible affichée) ;
    ``CORRECTION``   modifie une information du goal/de la cible courants (« finalement 5 », « plutôt chaque mois ») ;
    ``NEW_TASK``     nouvelle tâche métier indépendante — supersede l'attente ;
    ``INTERRUPTION`` navigation explicite qui quitte le flux courant ;
    ``UNRELATED``    hors domaine ;
    ``AMBIGUOUS``    plusieurs lectures raisonnables : on clarifie, on ne choisit pas."""

    ANSWER = "ANSWER"
    CORRECTION = "CORRECTION"
    NEW_TASK = "NEW_TASK"
    INTERRUPTION = "INTERRUPTION"
    UNRELATED = "UNRELATED"
    AMBIGUOUS = "AMBIGUOUS"


#: Intentions qui s'appliquent à la CIBLE de l'écran affiché (pas de produit à redonner) -> relation implicite.
CONTEXT_TARGETED_INTENTS: Dict[str, RelationToContext] = {
    "UPDATE_RECURRING_NEED": RelationToContext.CORRECTION,
    "REFRESH_RECURRING_MATCHING": RelationToContext.ANSWER,
}

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
    "VIEW": frozenset({"voir la prochaine livraison", "prochaine livraison", "non", "non merci", "ne rien changer"}),
    "EXEC": frozenset({"oui", "confirmer", "je confirme", "oui je confirme"}),
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


_CLOSED_INDEX = re.compile(r"^(?:(?:option|choix|numero|n)\s+)?(\d{1,2})$")


def closed_menu_index(norm: str) -> Optional[str]:
    """B24 — DÉTECTEUR DE RÉPONSE FERMÉE : l'index de menu visé par `norm` (déjà `fold`é), ou `None`.

    Fermé = le message ENTIER est un numéro (« 1 », « 01 », « 1. », « 1) ») ou « option/choix/numéro N ». Tout le reste —
    « je veux 1 chèvre », « mets-en 1 », « j'en veux 1 de plus » — contient un chiffre mais N'EST PAS une réponse fermée :
    il passe par l'interprétation sémantique. Jamais un « contient un chiffre »."""
    match = _CLOSED_INDEX.match(norm or "")
    return str(int(match.group(1))) if match else None


def _is_bare_digit(norm: str) -> bool:
    return closed_menu_index(norm) is not None


def is_recurring_navigation(norm: str) -> bool:
    """`norm` (déjà `fold`é) est une navigation explicite vers la liste des besoins récurrents."""
    return norm in NAVIGATION_INTENTS["GET_MY_NEEDS"]


def resolve_menu_action(norm: str, actions: Mapping[str, str], *, allow_digits: bool) -> Optional[str]:
    """Action du menu propriétaire visée par `norm` (chiffre du menu ou alias fermé) ; `None` sinon."""
    index = closed_menu_index(norm)
    if allow_digits and index is not None and index in actions:
        return str(actions[index])
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
    index = closed_menu_index(norm)
    if index is not None:
        return (int(index), str(actions[index])) if index in actions else None
    for action, aliases in RECURRING_MENU_TEXT_ALIASES.items():
        starts = {"CONFIRM": "accepter ", "REJECT": "refuser "}.get(action)
        if norm in aliases or (starts and norm.startswith(starts)):
            for key, value in actions.items():
                if value == action:
                    return int(key), action
    return None


def live_menu_view(
    state: Mapping[str, Any], *, now: Optional[float] = None, require_pending: bool = True
) -> Optional[Dict[str, Any]]:
    """B23 — le menu récurrent VIVANT vu comme une ATTENTE : `{"title", "labels": [...], "actions": {index: action}}`.

    Il guide l'interprétation d'un message libre (le micro-prompt SELECTION voit ce que l'écran propose) ; il ne décide
    jamais de l'intention. `None` : pas de menu récurrent vivant (autre goal, périmé, ou aucun)."""
    if require_pending:  # `False` : au rendu d'un flow, le pending est déjà consommé — le menu (daté) fait foi
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
    raw_facts = menu.get("facts")
    facts: Dict[str, Any] = raw_facts if isinstance(raw_facts, dict) else {}
    raw_shown = menu.get("shown_numbers")
    shown: List[Any] = raw_shown if isinstance(raw_shown, list) else []
    return {
        "title": str(menu.get("title") or ""),
        "labels": [str(labels.get(k) or menu["actions"][k]) for k in ordered],
        "actions": {str(k): str(menu["actions"][k]) for k in ordered},
        # B27 — FAITS métier de chaque option (dates ISO affichées) et nombres montrés par l'écran : ce qui permet au DOMAINE
        # (jamais au modèle) de résoudre « celui du 5 octobre », « celle de demain », et de refuser « oui mais mets 100 ».
        "facts": {str(k): dict(v) for k, v in facts.items() if isinstance(v, dict)},
        "shown_numbers": [float(x) for x in shown if isinstance(x, (int, float))],
    }


#: Actions d'un menu récurrent qui MODIFIENT l'état métier (accepter/refuser une proposition) : jamais déclenchées par une
#: réponse en langage libre — uniquement par un numéro ou un alias FERMÉ du menu (étape 1b de l'arbitrage).
MUTATING_MENU_ACTIONS: FrozenSet[str] = frozenset({"CONFIRM", "REJECT", "EXEC"})
#: Actions d'écran à effet de bord ou de navigation (recherche = moteur de matching, retour/commandes = changement d'écran) :
#: en langage LIBRE, une « sélection » du micro-prompt ne suffit pas à les déclencher — la relation avec l'attente doit être
#: justifiée par l'interprétation sémantique (reclassification NEW_TASK, B24). Un numéro/alias fermé reste direct (1b).
FREE_TEXT_GUARDED_ACTIONS: FrozenSet[str] = frozenset({"REFRESH", "LIST", "ORDERS", "VIEW", "ASK"})

GUARD_MUTATION_CLOSED_REPLY = "mutation_requires_closed_reply"
GUARD_FREE_TEXT_ACTION = "free_text_selection_rejected"


_ORDINALS: Mapping[str, int] = {
    "premier": 1, "premiere": 1, "1er": 1, "deuxieme": 2, "second": 2, "seconde": 2, "troisieme": 3, "quatrieme": 4,
    "cinquieme": 5,
}


_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def numbers_in(text: Any) -> List[float]:
    """Les nombres DITS dans le message (« 75 kg » -> 75.0, « 1,5 » -> 1.5). Lecture structurelle, sans vocabulaire."""
    out: List[float] = []
    for raw in _NUMBER_RE.findall(str(text or "")):
        try:
            out.append(float(raw.replace(",", ".")))
        except ValueError:  # pragma: no cover - le motif garantit un nombre
            continue
    return out


def _stem(token: str) -> str:
    """Repli de pluriel minimal (« chevres » ~ « chevre »), sans dictionnaire : un mot long finissant par s/x perd cette lettre."""
    return token[:-1] if len(token) > 4 and token[-1] in "sx" else token


def _label_tokens(label: str) -> set:
    return {_stem(t) for t in fold(label).split() if len(t) >= 4 and not t.isdigit()}


def _text_tokens(text: str) -> set:
    return {_stem(t) for t in fold(text).split()}


def reference_candidates(view: Mapping[str, Any], text: str) -> List[str]:
    """Indices des options dont le libellé partage un mot significatif avec le message (« mes chèvres » -> les deux Chèvre)."""
    tokens = _text_tokens(text)
    keys = [str(k) for k in view["actions"]]
    return [k for k, label in zip(keys, view["labels"], strict=False) if _label_tokens(label) & tokens]


def selection_is_evidenced(view: Mapping[str, Any], index: Any, text: str) -> bool:
    """B24/B27 — un choix d'entité en langage LIBRE (« le boeuf », « le premier », « celui du 5 octobre ») est ÉTAYÉ par le texte :
    un ordinal qui désigne cet index, un mot significatif du libellé qui DISTINGUE cette option des autres (un mot partagé par
    plusieurs options — « chèvre » quand il y a deux Chèvre — ne prouve rien), ou, quand il n'y a qu'une seule option, tout mot
    de son libellé. Un chiffre isolé (« mets-en 3 ») n'est PAS une preuve : c'est une quantité, pas un numéro d'écran. Ce n'est
    pas un routeur métier : c'est la vérification, hors LLM, qu'un index proposé par le micro-prompt est justifié par le message."""
    norm = fold(text)
    tokens = set(norm.split())
    if any(_ORDINALS.get(t) == int(index) for t in tokens if str(index).isdigit()):
        return True
    tokens = _text_tokens(text)
    keys = [str(k) for k in view["actions"]]
    labels = view["labels"]
    if str(index) not in keys:
        return False
    position = keys.index(str(index))
    mine = _label_tokens(labels[position])
    others: set = set()
    for i, label in enumerate(labels):
        if i != position:
            others |= _label_tokens(label)
    return bool((mine - others) & tokens) or (not others and bool(mine & tokens))


def resolve_date_reference(
    view: Mapping[str, Any], reference: Mapping[str, Any], text: str, *, today: Optional[date] = None
) -> Tuple[str, List[str]]:
    """B27 — RÉFÉRENCE TEMPORELLE -> option. Le modèle a extrait ce qui est DIT (`offset_days`, ou `day`/`month`) ; ICI le domaine
    calcule la date (`today` + décalage) et la compare aux dates réellement affichées (`view["facts"][i]["starts"|"deliveries"]`).
    `("one", [i])` : une seule option correspond ; `("many", [...])` : plusieurs (clarifier) ; `("none", [])` : aucune.
    Un jour (`day`) que le message ne contient pas n'est pas pris en compte (aucune date inventée par le modèle)."""
    today = today or date.today()
    wanted_day = reference.get("day")
    if wanted_day is not None and float(wanted_day) not in numbers_in(text):
        return "none", []
    role = str(reference.get("role") or "").upper()
    fact_keys = {"START": ("starts",), "DELIVERY": ("deliveries",)}.get(role, ("starts", "deliveries"))
    hits: List[str] = []
    for key in (str(k) for k in view["actions"]):
        option_facts = (view.get("facts") or {}).get(key, {})
        for iso in [d for fk in fact_keys for d in option_facts.get(fk) or []]:
            try:
                d = date.fromisoformat(str(iso)[:10])
            except ValueError:
                continue
            offset = reference.get("offset_days")
            if offset is not None and d == today + timedelta(days=int(offset)):
                hits.append(key)
                break
            if wanted_day is not None and d.day == int(wanted_day) and (
                reference.get("month") is None or d.month == int(reference["month"])
            ):
                hits.append(key)
                break
    if len(hits) == 1:
        return "one", hits
    return ("many", hits) if hits else ("none", [])


#: B27 — une acceptation/un refus LIBRE d'une entrée MUTANTE n'est admis qu'à ces conditions, en plus de la double lecture
#: sémantique (SELECTION puis NEW_TASK d'accord, voir `routing.py`) : certitude du modèle, message court, et AUCUN nombre que
#: l'écran n'a pas montré (« oui mais mets 100 » n'est pas une confirmation de 75 : c'est une correction).
NATURAL_CONFIRM_MIN_CONFIDENCE = 0.9
NATURAL_CONFIRM_MAX_TOKENS = 10
GUARD_NATURAL_CONFIRMATION = "natural_confirmation_candidate"
GUARD_ACCEPTANCE_WITH_NEW_VALUES = "acceptance_with_unshown_values"


def assess_natural_mutation(view: Mapping[str, Any], index: Any, result: Mapping[str, Any], text: str) -> str:
    """`"candidate"` : une acceptation/un refus naturel plausible (à confirmer par la 2e lecture) ; `"correction"` : le message
    apporte une valeur que l'écran n'a pas montrée -> ce n'est pas la confirmation de ce qui est affiché ; `"closed_required"` :
    pas assez de preuves -> numéro ou alias fermé exigé (comportement B23/B24)."""
    raw = result.get("raw_analysis") or {}
    shown = {float(x) for x in view.get("shown_numbers") or []} | {float(k) for k in view["actions"] if str(k).isdigit()}
    extra = [n for n in numbers_in(text) if n not in shown]
    if extra:
        return "correction"
    confidence = raw.get("selection_confidence")
    if not isinstance(confidence, (int, float)) or float(confidence) < NATURAL_CONFIRM_MIN_CONFIDENCE:
        return "closed_required"
    if len(fold(text).split()) > NATURAL_CONFIRM_MAX_TOKENS:
        return "closed_required"
    return "candidate"


def guard_free_text_selection(state: Mapping[str, Any], result: Dict[str, Any], text: str = "") -> Dict[str, Any]:
    """Fail-safe B23/B24/B27 : une SÉLECTION issue du micro-prompt (donc d'un langage LIBRE : les entrées fermées sont résolues
    avant, par l'arbitrage 1b) sur un menu récurrent vivant n'est JAMAIS exécutée à l'aveugle.

    * entrée MUTANTE (accepter/refuser/exécuter)   -> B27 : acceptation NATURELLE candidate (`GUARD_NATURAL_CONFIRMATION`, à
      confirmer par une 2e lecture sémantique indépendante) si le modèle est certain, le message court et sans valeur nouvelle ;
      valeur non montrée -> correction (`GUARD_ACCEPTANCE_WITH_NEW_VALUES`, reclassification) ; sinon `closed_reply_required` ;
    * action d'écran (rechercher, retour, ...)     -> marquée `free_text_selection_rejected` : l'appelant reclassifie le
      message par l'interprétation sémantique (NEW_TASK) au lieu d'exécuter l'index ;
    * choix d'une entité de la liste (SELECT)       -> accepté seulement s'il est ÉTAYÉ par le texte (`selection_is_evidenced`),
      sinon `free_text_selection_rejected` (+ `reference_candidates` quand plusieurs options correspondent).
    Hors menu récurrent vivant : `result` inchangé."""
    if str(result.get("interpreted_event") or "").upper() != "SELECTION":
        return result
    view = live_menu_view(state)
    index = (result.get("extracted_entities") or {}).get("selection_index")
    if view is None or index is None:
        return result
    action = view["actions"].get(str(index))
    raw = result.get("raw_analysis") or {}
    if action in MUTATING_MENU_ACTIONS:
        verdict = assess_natural_mutation(view, index, result, text)
        if verdict == "candidate":
            return {**result, "raw_analysis": {**raw, "guard": GUARD_NATURAL_CONFIRMATION, "natural_action": action}}
        if verdict == "correction":
            return {**result, "raw_analysis": {**raw, "guard": GUARD_FREE_TEXT_ACTION, "decision_reason": GUARD_ACCEPTANCE_WITH_NEW_VALUES}}
        return {**result, "extracted_entities": {**result["extracted_entities"], "closed_reply_required": True},
                "raw_analysis": {**raw, "guard": GUARD_MUTATION_CLOSED_REPLY}}
    if action in FREE_TEXT_GUARDED_ACTIONS:
        return {**result, "raw_analysis": {**raw, "guard": GUARD_FREE_TEXT_ACTION}}
    if action == "SELECT" and raw.get("reference_resolved"):
        return result  # B27 : option désignée par le DOMAINE (date calculée et comparée aux dates affichées), pas par le modèle
    if action == "SELECT" and text and not selection_is_evidenced(view, index, text):
        candidates = reference_candidates(view, text)
        out = {**result, "raw_analysis": {**raw, "guard": GUARD_FREE_TEXT_ACTION}}
        if len(candidates) >= 2:
            out["raw_analysis"]["reference_candidates"] = candidates
        return out
    return result


def live_menu_target(
    state: Mapping[str, Any], *, now: Optional[float] = None, require_pending: bool = True
) -> Optional[Dict[str, Any]]:
    """B24 — la CIBLE métier de l'écran récurrent vivant (`{"type": "RECURRING_NEED", "id", "product", ...}`), publiée par
    l'écran de détail dans `recurring_need_menu.target`. `None` : pas d'écran de détail vivant (liste, périmé, autre goal).
    C'est un *indice de résolution* : le flow la revalide (propriété) contre la base avant toute action ; jamais un index."""
    if live_menu_view(state, now=now, require_pending=require_pending) is None:
        return None
    target = ((state.get("working_memory") or {}).get("recurring_need_menu") or {}).get("target")
    if isinstance(target, dict) and target.get("id"):
        return {str(k): v for k, v in target.items()}
    return None


def targeted_menu_clarification(state: Mapping[str, Any], *, require_pending: bool = True) -> Optional[str]:
    """B24 — clarification MÉTIER (au lieu du « j'ai juste besoin de cette information » générique) quand un message n'a pu
    être rattaché ni à l'écran récurrent vivant ni à une autre tâche : on rappelle ce que l'écran propose et ce qui peut y
    être modifié. `None` : pas d'écran récurrent vivant (la clarification générique s'applique)."""
    view = live_menu_view(state, require_pending=require_pending)
    if view is None:
        return None
    target = live_menu_target(state, require_pending=require_pending)
    options = "\n".join(f"{k}. {label}" for k, label in zip(view["actions"], view["labels"], strict=False))
    # B27 — UNE question discriminante : plusieurs options correspondent à ce que l'utilisateur a désigné -> on ne liste QUE celles-là.
    candidates = [str(k) for k in ((state.get("extracted_entities") or {}).get("reference_candidates") or [])]
    narrowed = [(k, label) for k, label in zip(view["actions"], view["labels"], strict=False) if k in candidates]
    if len(narrowed) >= 2:
        shown = "\n".join(f"{k}. {label}" for k, label in narrowed)
        return f"Plusieurs options correspondent :\n\n{shown}\n\nLaquelle voulez-vous ? Répondez avec le numéro, ou précisez (par exemple une date)."
    if target is None:
        return f"Je n'ai pas compris ce choix.\n\n{view['title']}\n{options}\n\nRépondez avec un numéro, ou dites « mes commandes »."
    return (
        f"Je n'ai pas bien compris votre demande pour le besoin de {target.get('product') or 'ce besoin'}.\n\n{options}\n\n"
        "Répondez avec un numéro, ou dites ce que vous voulez modifier : la quantité, la fréquence ou la prochaine livraison."
    )


def recovery_target(state: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """B27 — CONTEXTE DE RÉCUPÉRATION : la livraison récurrente dont une notification récente a signalé l'échec (producteur sans
    réponse), quand elle désigne UN SEUL besoin. Sert de cible à « trouve-moi quelqu'un d'autre » ; revalidée par le flow contre
    les besoins de l'acheteur courant (jamais un identifiant pris au mot). `None` : aucun, ou plusieurs besoins (on clarifie)."""
    recovery = state.get("recovery_context")
    if not isinstance(recovery, dict):
        return None
    needs = [str(n) for n in recovery.get("recurring_need_ids") or []]
    if len(set(needs)) != 1:
        return None
    occurrences = [str(o) for o in recovery.get("occurrence_ids") or []]
    return {"type": "RECURRING_NEED", "id": needs[0], "occurrence_id": occurrences[0] if len(occurrences) == 1 else None,
            "source": "recovery_notification"}


def recovery_hint(state: Mapping[str, Any]) -> Optional[str]:
    """Description (générée par l'application, jamais le texte utilisateur) de la notification d'échec récente, pour le micro-prompt."""
    recovery = state.get("recovery_context")
    if not isinstance(recovery, dict) or not recovery.get("recurring_need_ids"):
        return None
    dates = ", ".join(str(d) for d in recovery.get("dates") or [])
    return "notification récente : le producteur n'a pas confirmé une livraison récurrente" + (f" ({dates})" if dates else "")


def derive_relation(state: Mapping[str, Any], result: Mapping[str, Any]) -> RelationToContext:
    """B24 — relation du message à l'attente, déduite des signaux STRUCTURÉS du résultat d'interprétation (pas du texte)."""
    event = str(result.get("interpreted_event") or "UNKNOWN").upper()
    intent = str(result.get("detected_intent") or "UNKNOWN").upper()
    if result.get("interruption_unresolved") or event in {"UNKNOWN", "AMBIGUOUS"}:
        return RelationToContext.AMBIGUOUS
    if event == "OUT_OF_SCOPE":
        return RelationToContext.UNRELATED
    if str((result.get("raw_analysis") or {}).get("path") or "") == "context_arbitration_outbound_menu":
        return RelationToContext.ANSWER  # réponse à l'action d'un message sortant interactif (digest)
    if event in {"SELECTION", "CONFIRM", "REJECT", "ANSWER"}:
        return RelationToContext.ANSWER
    if event == "UPDATE":
        return RelationToContext.CORRECTION
    if intent in NAVIGATION_INTENTS:
        return RelationToContext.INTERRUPTION
    if intent in CONTEXT_TARGETED_INTENTS and (live_menu_target(state) is not None or recovery_target(state) is not None):
        return CONTEXT_TARGETED_INTENTS[intent]
    return RelationToContext.NEW_TASK


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


def log_intent_arbitration(
    state: Mapping[str, Any],
    *,
    semantic_intent: Any,
    relation: Any,
    route: str,
    reason: str,
    target_type: Optional[str] = None,
    target_resolution: Optional[str] = None,
    deterministic_path: bool = False,
) -> None:
    """B23/B24 — journal de décision STRUCTURÉ « attente vs intention » (aucune PII : jamais le texte ni un identifiant,
    seulement des codes).

    `relation` : une `RelationToContext` (ANSWER | CORRECTION | NEW_TASK | INTERRUPTION | UNRELATED | AMBIGUOUS).
    `expected_action` : ce que l'écran actif attend. `target_type`/`target_resolution` : nature de la cible visée
    (RECURRING_NEED...) et issue de sa résolution (context | by_name | not_unique | not_found | none).
    `relation_to_expectation` = ancien nom B23 de `relation_to_context`, conservé pour les tableaux de bord existants."""
    pending = get_pending_interaction(dict(state))
    view = live_menu_view(state)
    expected = "|".join(sorted(set(view["actions"].values()))) if view else pending.kind.value
    goal = state.get("current_goal") or pending.goal or "NONE"
    rel = relation.value if isinstance(relation, RelationToContext) else str(relation)
    # B27 : `deterministic_path` (entrée fermée, 0 LLM) / `semantic_path` (lecture du modèle) et `clarification_reason` : codes
    # seulement, jamais le texte utilisateur ni un identifiant — de quoi mesurer taux de clarification et de repli.
    clarification = reason if rel == RelationToContext.AMBIGUOUS.value else "none"
    logger.info(
        "INTENT_ARBITRATION current_goal=%s expected_action=%s semantic_intent=%s relation_to_context=%s "
        "relation_to_expectation=%s target_type=%s target_resolution=%s selected_route=%s decision_reason=%s reason=%s "
        "deterministic_path=%s semantic_path=%s clarification_reason=%s",
        goal, expected, semantic_intent or "UNKNOWN", rel, rel, target_type or "NONE", target_resolution or "none",
        route, reason, reason, str(bool(deterministic_path)).lower(), str(not deterministic_path).lower(), clarification,
    )


__all__ = [
    "ArbitrationDecision",
    "CONTEXT_TARGETED_INTENTS",
    "FREE_TEXT_GUARDED_ACTIONS",
    "GUARD_FREE_TEXT_ACTION",
    "GUARD_MUTATION_CLOSED_REPLY",
    "RelationToContext",
    "closed_menu_index",
    "derive_relation",
    "selection_is_evidenced",
    "live_menu_target",
    "targeted_menu_clarification",
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
    "resolve_date_reference",
    "recovery_target",
    "recovery_hint",
    "reference_candidates",
    "assess_natural_mutation",
    "numbers_in",
    "GUARD_NATURAL_CONFIRMATION",
    "GUARD_ACCEPTANCE_WITH_NEW_VALUES",
    "resolve_menu_action",
    "stale_context_purge_patch",
]
