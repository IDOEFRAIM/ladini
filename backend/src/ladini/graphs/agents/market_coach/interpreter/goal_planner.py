"""Goal Planner — deterministic state machine for intent lifecycle.

Extracted from ``interpreter/routing.py`` so that the planner logic
(~400 lines of pure state-machine rules) lives in its own module.
"""

from __future__ import annotations

import logging
import re as _re
from typing import Any, Dict, Optional

from ladini.agents.reducers import mark_deleted
from ladini.graphs.agents.market_coach.core.conversation_decision import (
    ConversationAction,
)
from ladini.graphs.agents.market_coach.core.draft_registry import draft_reset_patch
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    DISAMBIGUATION_MENU_GOAL_SHIM,
    InteractionKind,
    clear_pending_interaction,
    get_pending_interaction,
    set_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.core.state import (
    clear_goal_lock,
    lock_goal,
    resolve_current_goal,
)
from ladini.graphs.agents.market_coach.core.tunnel_manager import tunnel_manager
from ladini.graphs.agents.market_coach.interpreter.intent import (
    INTENT_CONFIG,
    INTENT_DISAMBIGUATION,
)

# Source UNIQUE des clés de menu/sélection volatiles (nodes/cleanup.py) : le
# purge de verrou sur REJECT DOIT vider exactement le même univers de clés que
# le nettoyage de fin de tour, sinon un menu périmé survit à une annulation
# d'intention. Importer d'ici évite une 2e liste qui dériverait.
from ladini.graphs.agents.market_coach.nodes.cleanup import (
    _GENERIC_SELECTION_KEYS as _MENU_SELECTION_KEYS,
)
from ladini.graphs.agents.market_coach.nodes.cleanup import (
    _MENU_CACHE_KEYS,
)
from ladini.graphs.agents.market_coach.utils import MarketRuntime

logger = logging.getLogger("Ladini.Market.GoalPlanner")

# (2026-09-08) `locked_intent` retiré — pur doublon de `active_goal`, écrit à
# l'identique sur CHAQUE site (audit : aucune divergence trouvée nulle part
# dans le repo) — voir `core/state.py::resolve_current_goal`.
#
# (Phase 2 hardening, commit 6, mandat §18) : le verrou de tunnel lui-même
# (`active_goal`/`step_index`) est désormais posé/effacé par l'API canonique
# `core/state.py::lock_goal`/`clear_goal_lock` (voir `_lock`/`_clear_goal_lock`
# ci-dessous) — les anciennes constantes `_TUNNEL_LOCK_KEYS`/
# `_GOAL_LOCK_CLEAR_KEYS` de ce module ont été retirées car elles ne feraient
# plus que dupliquer, sans jamais diverger, la liste que ces deux fonctions
# encodent déjà. L'union purgée sur REJECT reste : ce verrou canonique + tout
# l'état de menu/sélection propre à ce module (importé ci-dessus).


# ── Shared constants (also used by routing.py interpreter) ──────────

INTENT_TO_GOAL_MAP: Dict[str, str] = {}

# Alias de compat — source canonique : core/goals.py (flag `breakout`
# d'INTENT_CONFIG). Re-export SANS decision : depuis 2026-09-09 (Bloc 2,
# Invariant A) le SEUL consommateur decisionnel de cet ensemble est
# `nodes/cognitive.py` (interruption). Ici il ne sert qu'a `interpreter/
# routing.py::make_route_after_validator`, qui l'importe depuis ce module
# pour un usage DIFFERENT (tolerance aux missing_fields sur un goal de
# navigation), jamais pour decider d'une interruption.
from ladini.graphs.agents.market_coach.core.goals import (  # noqa: E402
    NAVIGATION_BREAKOUT_GOALS as _NAVIGATION_INTENTS,
)


def _init_intent_to_goal_map(
    producer_intents: frozenset, buyer_intents: frozenset, common_intents: frozenset
) -> None:
    """Populate INTENT_TO_GOAL_MAP once role-based sets are available.

    Called from routing.py at module-load time to avoid a circular import
    (this module must not import role sets directly from routing).
    """
    INTENT_TO_GOAL_MAP.clear()
    for intent_key in producer_intents | buyer_intents | common_intents:
        INTENT_TO_GOAL_MAP[intent_key] = intent_key


# ── Buyer product request heuristic ────────────────────────────────

_BUYER_PRODUCT_HINTS = (
    "je veux",
    "je voudrais",
    "j'aimerais",
    "je cherche",
    "je recherche",
    "il me faut",
    "besoin de",
    "j'ai besoin",
    "cherche",
)
# Marqueurs de SUIVI/gestion d'une commande existante (≠ acte d'achat). Ils
# désactivent le repli "demande produit". IMPORTANT : ces marqueurs sont
# comparés au MOT ENTIER (borne \b), pas en sous-chaîne — sinon "commander"
# (= acheter) était capté par "commande" (= ma commande à suivre) et l'achat
# retombait à tort sur le suivi de commandes.
_BUYER_PRODUCT_EXCLUDES = (
    "commande",
    "commandes",
    "suivre",
    "statut",
    "paiement",
    "appels",
    "appel",
    "prix",
)
_BUYER_FILLER_WORDS = frozenset(
    {
        "je",
        "veux",
        "voudrais",
        "cherches",
        "cherche",
        "recherche",
        "du",
        "de",
        "des",
        "de la",
        "d",
        "un",
        "une",
        "le",
        "la",
        "les",
        "il",
        "me",
        "faut",
        "besoin",
        "avoir",
        "jai",
        "j",
        "ai",
        "pour",
        "acheter",
        # "commander"/"commandez" = verbe d'achat, jamais un produit : filtré de
        # l'extraction pour que "je veux commander des tomates" donne "tomates".
        "commander",
        "commandez",
    }
)


def _looks_like_buyer_product_request(clean_text: str) -> bool:
    if not clean_text:
        return False
    if not any(hint in clean_text for hint in _BUYER_PRODUCT_HINTS):
        return False
    if any(
        _re.search(rf"\b{_re.escape(ex)}\b", clean_text)
        for ex in _BUYER_PRODUCT_EXCLUDES
    ):
        return False
    tokens = _re.findall(r"[a-zàâçéèêëîïôûùüÿñæœ']+", clean_text)
    meaningful = [t for t in tokens if t not in _BUYER_FILLER_WORDS]
    return bool(meaningful)


def _extract_buyer_product(clean_text: str) -> Optional[str]:
    """Extraction déterministe du produit d'une demande d'achat.

    Utilisée UNIQUEMENT en repli quand le LLM est indisponible (429/timeout) :
    retire les mots de remplissage et les verbes d'achat, garde les tokens
    porteurs de sens. Ce n'est PAS le chemin nominal — dès que le LLM répond,
    c'est lui qui extrait le produit.
    """
    if not clean_text:
        return None
    tokens = _re.findall(r"[a-zàâçéèêëîïôûùüÿñæœ']+", clean_text)
    meaningful = [t for t in tokens if t not in _BUYER_FILLER_WORDS]
    return " ".join(meaningful) if meaningful else None


# ── Goal planner node ──────────────────────────────────────────────


async def goal_planner(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    """Machine à états pure pour la gestion du cycle de vie des intentions."""
    event = str(state.get("interpreted_event") or "UNKNOWN").upper()
    detected_intent = str(state.get("detected_intent") or "UNKNOWN").upper()
    working = state.get("working_memory") or {}
    current_goal = resolve_current_goal(state)
    # (2026-09-02, refonte "no legacy shim") : `expected_input` n'est plus lu
    # depuis `state` — dérivé de `pending_interaction`, seule source
    # canonique, via `to_tunnel_category` (traduction vers le vocabulaire
    # grossier attendu par TunnelManager, catégorie DISTINCTE du kind précis).
    # Un seul point de traduction : tout le reste de cette fonction (in_tunnel,
    # libellés de statut, les deux appels tunnel_manager.evaluate ci-dessous)
    # consomme cette même variable, plus jamais `state.get("expected_input")`.
    pending_interaction = get_pending_interaction(state)
    expected_input = to_tunnel_category(pending_interaction)
    # (2026-09-14, incident WhatsApp #8) : voir RÈGLE 5 plus bas pour le
    # contexte complet — calculé ICI, avant la RÈGLE 1bis, parce que celle-ci
    # doit explicitement laisser passer ce cas plutôt que de reverrouiller
    # aveuglément le tunnel actuel avant que la RÈGLE 5 n'ait pu agir.
    _breakout_confirm = (
        event in ("CONFIRM", "REJECT")
        and detected_intent in _NAVIGATION_INTENTS
        and detected_intent != str(current_goal or "").upper()
    )
    # (2026-09-09, audit Bloc 2, fermeture Blocker B) : la RÈGLE 0bis
    # ci-dessous se déclenche désormais EXCLUSIVEMENT sur ce signal —
    # `pending_interaction` (DURABLE, survit nativement au checkpoint, voir
    # nodes/cleanup.py::keep_selection_channel) — jamais plus sur un pseudo-
    # goal écrit dans `current_goal`. Avant ce correctif, `current_goal`
    # servait de DEUXIÈME canal pour la même information (« une
    # désambiguïsation est en cours »), avec son propre mécanisme de
    # restauration cross-tour (`working_memory.disambiguation_pending`) —
    # une source de vérité concurrente à `pending_interaction`, exactement
    # la classe de fragmentation que ce module a éliminée pour tout le
    # reste (voir docstring de `core/pending_interaction.py`). Conséquence
    # positive : `current_goal` porte maintenant, PENDANT la désambiguïsation,
    # le vrai business goal antérieur (celui du tunnel interrompu pour
    # déclencher le menu), ou `None` s'il n'y en avait aucun — plus jamais un
    # pseudo-goal, sauf dans le patch de SORTIE du cas "sélection toujours en
    # attente" ci-dessous (shim de compatibilité `validator`, documenté sur
    # `DISAMBIGUATION_MENU_GOAL_SHIM`).
    in_disambiguation_menu = (
        pending_interaction.kind == InteractionKind.SELECTION_MENU
        and pending_interaction.context_ref == "intent_disambiguation"
    )
    goal_stack = list(state.get("goal_stack") or [])
    in_tunnel = bool(current_goal and expected_input and expected_input != "NONE")
    text = (
        str(state.get("normalized_text") or state.get("user_query") or "")
        .strip()
        .lower()
    )
    if not text:
        messages = state.get("messages") or []
        if isinstance(messages, list):
            for msg in reversed(messages):
                if not isinstance(msg, dict):
                    continue
                role2 = str(msg.get("role") or "").lower().strip()
                if role2 != "user":
                    continue
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    text = content.strip().lower()
                    break
    is_short = (
        len(text) <= 4 or text.isdigit() or text in {"oui", "non", "ok", "yes", "no"}
    )

    updates: Dict[str, Any] = {
        "status": "PLANNING",
        "interruption_detected": False,
    }

    logger.info(
        "[GoalPlanner IN] event=%s intent=%s current_goal=%s expected=%s",
        event,
        detected_intent,
        current_goal,
        expected_input,
    )

    def _lock(
        goal: Optional[str], extra: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        # (Phase 2 hardening, commit 6, mandat §18) : le SET/TRANSITION de
        # `active_goal`/`step_index` passe par l'API canonique
        # `core/state.py::lock_goal` — une seule fonction pour cette
        # représentation, au lieu d'une réimplémentation locale à ce fichier.
        wm: Dict[str, Any] = lock_goal(working, goal)
        if extra:
            wm.update(extra)
        return wm

    def _clear_goal_lock() -> Dict[str, Any]:
        """Sur REJECT/annulation : supprime le verrou de tunnel canonique
        (`active_goal`/`step_index`, via `core/state.py::clear_goal_lock`) ET
        tout l'état de menu/sélection (univers de clés partagé avec
        `nodes/cleanup.py`, source unique), pour ne pas laisser un menu périmé
        actif après l'abandon de l'intention en cours."""
        # `merge_dict` ignore une clé ABSENTE : retirer la clé (`pop`) laissait
        # `active_goal` intact et ressuscitait le goal rejeté au tour suivant
        # (audit B1). DELETE efface réellement.
        wm: Dict[str, Any] = clear_goal_lock(working)
        mark_deleted(wm, *_MENU_SELECTION_KEYS, *_MENU_CACHE_KEYS)
        return wm

    def _purge_transaction_state() -> Dict[str, Any]:
        """Purge totale des champs transactionnels/AG-UI lors d'un switch d'intention."""
        return {
            "transaction_payload": {"__reset__": True},
            "draft_payload": {"__reset__": True},
            "stable_entities": {"__reset__": True},
            "missing_fields": [],
            "completed_fields": [],
            "last_missing_field": None,
            "expected_candidates": [],
            "available_mapping": {},
            "confirmation_summary": None,
            "selected_tool": None,
            "selected_tool_args": {"__reset__": True},
            "execution_result": {"__reset__": True},
            "retry_count": 0,
            "vendor_selection_context": {"__reset__": True},
            # (2026-09-02, refonte state canonique, G-2) : symétrique de la
            # ligne au-dessus — un switch de goal/intention doit invalider le
            # palier choisi exactement comme il invalide le vendeur choisi.
            # Absent jusqu'ici : un `tier_selection_context` pouvait survivre
            # à un changement d'intention et être réutilisé pour un NOUVEAU
            # produit sans aucun rapport (confirmé par audit — voir
            # `domain/selection_actions.py::build_selection_context`, qui
            # lit ce champ tel quel sans jamais vérifier qu'il correspond
            # encore au vendeur/produit courant).
            "tier_selection_context": {"__reset__": True},
            "negotiation_context": {"__reset__": True},
            "preorder_workflow": {"__reset__": True},
            # A form belongs to exactly one goal instance — leaving it set
            # while switching to an unrelated goal can misroute the NEXT
            # turn straight back into form_node and leaks the abandoned
            # form's slots via `form_data` (merge_dict).
            "active_form": None,
            "form_step": None,
            "form_data": {"__reset__": True},
            # (2026-09-09, bug réel en production — audit Bloc 2) : les 3
            # drafts transactionnels (`replace_value`, jamais `merge_dict` —
            # voir core/state.py) n'étaient JAMAIS purgés ici. Incident
            # confirmé : un `SALES_PUBLISH_PRODUCT` déjà mené à terme
            # (`sales_publish_draft.status == PUBLISHED`) pour un produit A
            # restait posé dans l'état ; une nouvelle demande de publication,
            # sans rapport, pour un produit B ("je veux vendre mon lait")
            # atteignait `confirmation_gate.py::_resolve_sales_draft_based_
            # confirmation`, qui réutilise INCONDITIONNELLEMENT tout draft
            # déjà présent (`if state.get("sales_publish_draft") is not
            # None: ...`, sans jamais vérifier qu'il correspond au produit en
            # cours) — la tentative pour B se heurtait au draft FINALISÉ de A
            # ("Cette publication est déjà publiée — rien à modifier ici.").
            # Même schéma partagé par `procurement_draft`/`preorder_draft`
            # (voir `confirmation_gate.py::_resolve_draft_based_confirmation`,
            # strictement le même `if ... is not None: reuse` sans contrôle
            # de correspondance) — les trois sont donc purgés symétriquement
            # ici, au même titre que `draft_payload`/`vendor_selection_context`
            # ci-dessus, sur tout VRAI changement de goal.
            **draft_reset_patch(),  # les 4 drafts déclarés dans core/draft_registry.py
            # (2026-09-02) Politique d'invalidation centralisée (mandat §8) :
            # un switch de goal/intention efface aussi le discriminant
            # canonique — sans ceci, un `pending_interaction` persisté
            # (CONFIRM_ACTION/ENTER_FIELD/...) pouvait survivre à un
            # changement d'intention et fuiter dans le nouveau parcours.
            **clear_pending_interaction("goal_changed"),
        }

    def _with_goal_metadata(
        payload: Dict[str, Any], goal_hint: Optional[str] = None
    ) -> Dict[str, Any]:
        target_goal = goal_hint
        if target_goal is None:
            target_goal = resolve_current_goal(payload) or state.get("current_goal")
        goal_key = str(target_goal or "").upper().strip()
        cfg = INTENT_CONFIG.get(goal_key, {})
        lifecycle = str(cfg.get("lifecycle_mode") or "").upper().strip()
        if lifecycle not in {"CREATE", "UPDATE", "READ"}:
            lifecycle = "READ"
        payload["goal_metadata"] = {
            "lifecycle_mode": lifecycle,
            "update_mode": lifecycle == "UPDATE",
        }
        return payload

    # RÈGLE 0bis — RÉSOLUTION DE DÉSAMBIGUÏSATION
    if in_disambiguation_menu:
        override_goal = INTENT_TO_GOAL_MAP.get(detected_intent)
        if (
            event in {"INTERRUPTION", "NEW_TASK"}
            and override_goal
            and override_goal != DISAMBIGUATION_MENU_GOAL_SHIM
        ):
            logger.info(
                "[Disambiguation Override] event=%s intent=%s -> current_goal=%s",
                event,
                detected_intent,
                override_goal,
            )
            return _with_goal_metadata(
                {
                    "status": "PLANNING",
                    "current_goal": override_goal,
                    "goal_status": "ACTIVE",
                    "interruption_detected": event == "INTERRUPTION",
                    "working_memory": {
                        **_lock(override_goal),
                        "disambiguation_pending": False,
                        "available_mapping_kind": None,
                    },
                    **_purge_transaction_state(),
                },
                override_goal,
            )

        extracted = state.get("extracted_entities") or {}
        mapping = dict(state.get("available_mapping") or {})

        trigger_id = str(
            (state.get("working_memory") or {}).get("disambiguation_trigger_id") or ""
        ).strip()
        if trigger_id:
            entry = INTENT_DISAMBIGUATION.get(trigger_id)
            if entry:
                rebuilt: Dict[str, str] = {}
                for i, opt in enumerate(entry.get("options") or [], start=1):
                    intent_key = None
                    if isinstance(opt, (tuple, list)) and len(opt) >= 2:
                        intent_key = opt[0]
                    elif isinstance(opt, dict):
                        intent_key = opt.get("intent")
                    if intent_key:
                        rebuilt[str(i)] = str(intent_key)
                if rebuilt:
                    if mapping and mapping != rebuilt:
                        logger.warning(
                            "[Disambiguation] stale mapping overridden by trigger=%s",
                            trigger_id,
                        )
                    mapping = rebuilt
                    logger.info(
                        "[Disambiguation] rebuilt mapping from trigger=%s with %d options",
                        trigger_id,
                        len(mapping),
                    )

        sel_idx = extracted.get("selection_index")
        sel_val = extracted.get("selected_value")

        resolved_intent: Optional[str] = None
        if sel_idx is not None:
            resolved_intent = mapping.get(str(sel_idx))
        if resolved_intent is None and sel_val is not None:
            resolved_intent = mapping.get(str(sel_val))

        if (
            resolved_intent is None
            and detected_intent in INTENT_TO_GOAL_MAP
            and detected_intent not in {"", "UNKNOWN", DISAMBIGUATION_MENU_GOAL_SHIM}
        ):
            resolved_intent = detected_intent

        if resolved_intent and resolved_intent in INTENT_TO_GOAL_MAP:
            logger.info(
                "[Disambiguation Resolved] selection=%s promoted to current_goal=%s",
                sel_idx or sel_val,
                resolved_intent,
            )
            return _with_goal_metadata(
                {
                    "status": "PLANNING",
                    "current_goal": resolved_intent,
                    "goal_status": "ACTIVE",
                    "interruption_detected": False,
                    "expected_candidates": [],
                    "available_mapping": {},
                    "missing_fields": [],
                    "completed_fields": [],
                    "working_memory": {
                        **_lock(resolved_intent),
                        "disambiguation_pending": False,
                        "available_mapping_kind": None,
                    },
                    **clear_pending_interaction("disambiguation_resolved"),
                },
                resolved_intent,
            )
        # Sélection invalide ou pas encore reçue : on garde le menu actif.
        # `current_goal` ci-dessous n'est PLUS le signal qui redéclenchera
        # cette règle au tour suivant (c'est `pending_interaction`,
        # réaffirmé juste en dessous, qui en est responsable) — c'est
        # désormais un pur shim de compatibilité pour `validator`/
        # `DomainRouter` (gelés), voir `DISAMBIGUATION_MENU_GOAL_SHIM`.
        updates["current_goal"] = DISAMBIGUATION_MENU_GOAL_SHIM
        updates["goal_status"] = "WAITING_INPUT"
        updates.update(
            set_pending_interaction(
                InteractionKind.SELECTION_MENU,
                context_ref="intent_disambiguation",
            )
        )
        updates["confirmation_summary"] = None
        updates["response_strategy"] = "SELECTION_MENU"
        updates["working_memory"] = {
            **dict(working),
            "disambiguation_pending": True,
            "available_mapping_kind": "intent_disambiguation",
        }
        return _with_goal_metadata(updates)

    # RÈGLE 1 — CANCEL/REJECT (Annulation explicite)
    if event == "REJECT":
        # Source unique : `expected_input` ci-dessus est déjà dérivé de
        # `pending_interaction` (voir plus haut) — plus de lecture directe de
        # `waiting_for_confirmation`.
        waiting_confirm = str(expected_input or "").upper() == "CONFIRMATION"
        if not waiting_confirm:
            return _with_goal_metadata(
                {
                    "status": "WAITING_INPUT",
                    "current_goal": None,
                    "goal_status": "IDLE",
                    "interruption_detected": False,
                    "response_strategy": "CLARIFICATION",
                    "working_memory": _clear_goal_lock(),
                    **_purge_transaction_state(),
                }
            )

        # Rejet pendant confirmation : laisser confirmation_gate gérer la logique.
        updates["current_goal"] = current_goal
        updates["goal_status"] = "ACTIVE"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 1bis — TUNNEL LOCKING (Maintien des formulaires d'IHM)
    #
    # Incident réel (2026-09-08) : cette règle s'appliquait au SEUL vu de
    # l'événement, SANS vérifier qu'un tunnel existe réellement à verrouiller.
    # « j'ai 6000 poulets » (hors tunnel) était classé
    # `event=ANSWER intent=STOCK_REGISTER_HARVEST current_goal=None` — la
    # règle réaffectait alors `current_goal = None` (soit : rien), JETAIT
    # l'intention pourtant correctement détectée, et le tour finissait en
    # « Je n'ai pas bien saisi ». Une minute plus tard, « je veux vendre mes
    # poulets » — situation identique (aucun tunnel, intention sûre) —
    # fonctionnait, uniquement parce que le LLM avait cette fois étiqueté
    # l'événement `NEW_TASK` (RÈGLE 5) au lieu d'`ANSWER`. Or cette
    # étiquette n'est pas fiable par nature : une phrase qui ÉNONCE une
    # donnée (« j'ai 6000 poulets ») ressemble légitimement à une réponse de
    # slot. Faire dépendre l'accès à TOUTE la machine à états d'un label que
    # le LLM ne peut pas trancher de façon déterministe est structurellement
    # fragile — d'où le garde `current_goal` ici : « verrouiller le tunnel »
    # n'a de sens que s'il y a un tunnel. Sans tunnel, ANSWER/UPDATE
    # retombent sur la RÈGLE 5, qui sait promouvoir l'intention en goal.
    if (
        current_goal
        and event in {"CONFIRM", "SELECTION", "ANSWER", "UPDATE"}
        and not _breakout_confirm
    ):
        updates["current_goal"] = current_goal
        updates["goal_status"] = "ACTIVE"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 1ter — PERSISTENCE PAR DÉFAUT
    if (
        current_goal
        and detected_intent == "UNKNOWN"
        and event in {"UNKNOWN", "NEW_TASK"}
    ):
        updates["current_goal"] = current_goal
        updates["detected_intent"] = str(current_goal).upper()
        updates["goal_status"] = (
            "ACTIVE" if expected_input in (None, "", "NONE") else "WAITING_INPUT"
        )
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 1quater — VERROUILLAGE PENDANT SLOT-FILLING
    if (
        current_goal
        and event == "NEW_TASK"
        and expected_input not in (None, "", "NONE")
    ):
        # (2026-09-09, Bloc 2 passe finale — Invariant A) : ce bloc
        # interrogeait `tunnel_manager.evaluate()` pour savoir s'il pouvait
        # basculer de goal. Il ne le fait plus : atteindre CE point avec
        # `event == "NEW_TASK"` signifie par construction que
        # `cognitive_guard` — seul propriétaire de la décision — a REFUSÉ
        # d'interrompre (s'il avait approuvé, il aurait réécrit l'événement
        # en "INTERRUPTION", traité par la RÈGLE 4). Réinterroger un second
        # arbitre ici ne pouvait que contredire ce refus. Le planner APPLIQUE
        # la décision : tunnel verrouillé.
        updates["current_goal"] = current_goal
        updates["detected_intent"] = str(current_goal).upper()
        updates["goal_status"] = "WAITING_INPUT"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # (Phase 2 hardening, bug B3) : `event == "INTERRUPTION"` ne peut arriver ici QUE
    # réécrit par `cognitive_guard` — SEUL propriétaire de la décision d'interruption
    # (voir la docstring de `cognitive_guard`) — quand il a explicitement APPROUVÉ
    # l'interruption. Cette heuristique (longueur du message, ni plus ni moins) doit
    # toujours s'effacer devant une décision déjà prise en amont : avant ce correctif,
    # un message court ("maïs", "riz", "prix") ré-verrouillait ICI l'ANCIEN goal sans
    # même regarder `event`, annulant silencieusement une interruption pourtant déjà
    # approuvée — RULE 4 (juste en dessous) n'était alors jamais atteinte.
    if is_short and current_goal and event != "INTERRUPTION":
        updates["current_goal"] = current_goal
        updates["detected_intent"] = str(current_goal).upper()
        updates["goal_status"] = (
            "ACTIVE" if expected_input in {None, "NONE"} else "WAITING_INPUT"
        )
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 2 — POLLUTION PENDANT SLOT-FILLING
    if event == "UNKNOWN" and in_tunnel:
        updates["current_goal"] = current_goal
        updates["goal_status"] = "WAITING_INPUT"
        updates["detected_intent"] = str(current_goal).upper()
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 3 — OUT_OF_SCOPE
    if event == "OUT_OF_SCOPE":
        updates["current_goal"] = current_goal
        updates["goal_status"] = "ACTIVE" if current_goal else "IDLE"
        updates["response_strategy"] = "CLARIFICATION"
        updates["status"] = "WAITING_INPUT"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 4 — INTERRUPTION (Suspension d'un workflow IHM en cours)
    if event == "INTERRUPTION":
        confidence = float(state.get("interpreter_confidence") or 0.0)
        # (2026-09-09, Bloc 2 passe finale — Invariant A) : `policy_approved`
        # relaie la décision DÉJÀ prise par `cognitive_guard`
        # (`cognitive_decision.action == INTERRUPT_ACTIVE_GOAL`) — le planner
        # ne la rejuge pas, il la transmet. `TunnelManager` ne conserve alors
        # que son droit d'INTERDICTION structurelle (OTP inviolable).
        # Une INTERRUPTION produite AILLEURS que par `cognitive_guard`
        # (`input_interpreter` en émet une lors d'une dérive pendant
        # SELECTION/CONFIRMATION, Bloc 1 gelé) arrive ici avec
        # `policy_approved=False` : elle reste soumise au seuil de confiance
        # historique de `TunnelManager`, comportement inchangé.
        policy_approved = (
            str((state.get("cognitive_decision") or {}).get("action") or "")
            == ConversationAction.INTERRUPT_ACTIVE_GOAL
        )
        td = tunnel_manager.evaluate(
            current_goal=current_goal,
            expected_input=expected_input,
            incoming_event=event,
            incoming_intent=detected_intent,
            confidence=confidence,
            policy_approved=policy_approved,
        )
        new_goal = INTENT_TO_GOAL_MAP.get(detected_intent)
        # `allow_interrupt` protège un tunnel ACTIF d'un déraillement. S'il n'y
        # a PAS de tunnel actif (`current_goal` vide — ex: un menu orphelin
        # affiché sans goal de rattachement, comme une liste de commandes
        # restée à l'écran), il n'y a rien à protéger : une « interruption » de
        # rien = une nouvelle tâche → on bascule directement. Sans ce
        # `or not current_goal`, l'utilisateur restait piégé dans le menu, sa
        # nouvelle demande (« je veux des tomates ») ignorée en boucle.
        if (
            new_goal
            and new_goal != current_goal
            and (td.allow_interrupt or not current_goal)
        ):
            if current_goal:
                goal_stack.append(current_goal)
            return _with_goal_metadata(
                {
                    "status": "PLANNING",
                    "current_goal": new_goal,
                    "goal_stack": goal_stack,
                    "goal_status": "ACTIVE",
                    "interruption_detected": True,
                    "suspended_goal": current_goal,
                    "suspended_payload": state.get("transaction_payload") or {},
                    **_purge_transaction_state(),
                    "working_memory": _lock(new_goal),
                },
                new_goal,
            )
        if td.allow_interrupt and new_goal and new_goal == current_goal:
            # Same goal, new entities: user is mid-confirmation for one
            # instance of this goal (e.g. BUYER_REQUEST "poulets") and just
            # re-triggered the SAME goal with DIFFERENT entities ("je veux
            # des poussins"). `new_goal != current_goal` above is False so
            # this never restarted — the stale payload survived and the
            # recap kept showing the old product forever. Purge the
            # transaction so the freshly extracted entities populate a
            # clean slate instead of merging onto an already-confirmed-
            # looking recap.
            return _with_goal_metadata(
                {
                    "status": "PLANNING",
                    "current_goal": new_goal,
                    "goal_status": "ACTIVE",
                    "interruption_detected": True,
                    **_purge_transaction_state(),
                    "working_memory": _lock(new_goal),
                },
                new_goal,
            )
        updates["current_goal"] = current_goal
        updates["response_strategy"] = "CLARIFICATION"
        updates["status"] = "WAITING_INPUT"
        updates["goal_status"] = "ACTIVE" if current_goal else "IDLE"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 4bis — RESUME (Reprise d'une tâche suspendue dans goal_stack)
    if event == "RESUME":
        if goal_stack:
            resumed = goal_stack.pop()
            restored_payload = dict(state.get("suspended_payload") or {})
            return _with_goal_metadata(
                {
                    "status": "PLANNING",
                    "current_goal": resumed,
                    "goal_stack": goal_stack,
                    "goal_status": "ACTIVE",
                    "interruption_detected": False,
                    "suspended_goal": None,
                    "suspended_payload": {"__reset__": True},
                    # Full canonical purge FIRST — clears whatever the
                    # interrupting goal accumulated in its own
                    # transaction_payload/vendor_selection_context/
                    # negotiation_context/preorder_workflow/selected_tool_args/
                    # execution_result/draft_payload/form_data (previously this
                    # branch hand-rolled a shorter reset list and missed all of
                    # these, letting the interrupting goal's fields leak into
                    # the resumed one) — THEN restore the resumed goal's own
                    # payload via the combined reset+populate sentinel: a plain
                    # merge of `restored_payload` onto the just-purged (but not
                    # yet actually empty, since it's one reducer call) old value
                    # would let any key present in the interrupting goal's
                    # payload but absent from `restored_payload` survive the
                    # merge — this sentinel guarantees a true replace instead.
                    **_purge_transaction_state(),
                    "transaction_payload": {"__reset__": True, **restored_payload},
                    "working_memory": _lock(resumed),
                },
                resumed,
            )
        # Aucun goal suspendu — traiter comme clarification
        updates["current_goal"] = current_goal
        updates["response_strategy"] = "CLARIFICATION"
        updates["status"] = "WAITING_INPUT"
        updates["goal_status"] = "ACTIVE" if current_goal else "IDLE"
        updates["working_memory"] = _lock(current_goal)
        return _with_goal_metadata(updates)

    # RÈGLE 4ter — CONFIRM ORPHELIN SUR UN PANIER NON VIDE
    #
    # Incident réel (2026-09-09) : après l'ajout au panier, les nœuds de
    # nettoyage de fin de tour remettent `current_goal` à None (l'ajout est
    # terminé). Le récap panier invite pourtant explicitement « Répondez
    # *précommander* pour valider ». Quand l'utilisateur répond « je valide »
    # (classé `event=CONFIRM`, `intent=UNKNOWN`), aucune règle ne s'applique
    # (RÈGLE 1bis/1ter exigent un `current_goal`, RÈGLE 5 exclut CONFIRM) et le
    # tour retombait sur le menu générique « Que souhaitez-vous faire ? » — le
    # panier était toujours là mais l'agent n'y touchait plus.
    #
    # `detect_cart_action` (flows/buyer/helpers.py) porte déjà exactement
    # cette lecture (CONFIRM + panier non vide → PREORDER) mais vit DANS
    # `cart_management`, qui n'est jamais atteint quand le planner court-
    # circuite vers `response_strategy`. On promeut donc ici vers
    # `BUYER_PREORDER_CONFIRM` : `_route_after_planner` (status="PLANNING",
    # pas WAITING_INPUT) laisse alors le pipeline continuer jusqu'à
    # `buyer_context_resolver`, qui sait ouvrir la précommande.
    #
    # (2026-09-11) Promu vers `BUYER_PREORDER_CONFIRM`, PAS `..._INIT` — ce
    # `event=CONFIRM` porte une vraie intention de confirmation ("je valide
    # le panier"), pas juste une ouverture de tunnel. Avec `..._INIT`,
    # `create_preorder` (flows/buyer/preorder.py) ne dérive `resolved_id`
    # que si `goal == "BUYER_PREORDER_CONFIRM"` (voir son garde dédié) : sans
    # ça, le draft fraîchement créé (`bootstrap_preorder_draft`) s'arrêtait
    # TOUJOURS sur un second « Confirmez-vous ? » redondant — l'utilisateur
    # devait confirmer deux fois pour la même décision avant même d'arriver
    # à l'étape GPS. `BUYER_PREORDER_CONFIRM` fait chaîner directement
    # bootstrap → `resolve_preorder_confirmation` en un seul tour.
    # (2026-09-14, incident WhatsApp #11 — correction du correctif #7) :
    # `state.get("active_cart")` (CE tour) et `working.get("last_active_cart")`
    # (un SNAPSHOT d'un tour antérieur, potentiellement vieux de plusieurs
    # jours) ne portent PAS la même fiabilité de signal — les fusionner sous
    # un seul `_orphan_cart` masquait cette différence. Le garde de rôle posé
    # en #7 (bloquer cette promotion pour un rôle PRODUCER) était trop large :
    # il bloquait aussi un producteur en train d'ACHETER, panier fraîchement
    # rempli CE MÊME tour ("je veux 9" → panier → "okay" quelques secondes
    # plus tard) — un signal parfaitement légitime quel que soit le rôle
    # résolu (un dual-rôle peut acheter ET vendre). Seul le repli sur le
    # SNAPSHOT (jamais le panier ACTUEL) doit rester restreint au rôle BUYER :
    # c'est LUI, pas un panier frais, qui a causé l'incident #7 (un panier
    # acheteur périmé resurgissant chez un producteur qui tapait "confirmer"
    # pour tout autre chose).
    _fresh_cart = state.get("active_cart") or []
    _stale_cart_snapshot = working.get("last_active_cart") or []
    _role_up = str(state.get("user_role") or "").upper()
    _orphan_cart = _fresh_cart or (_stale_cart_snapshot if _role_up != "PRODUCER" else [])
    if (
        not current_goal
        and event == "CONFIRM"
        and _orphan_cart
        and str(expected_input or "NONE").upper() in {"NONE", "SELECTION"}
    ):
        logger.info(
            "[GoalPlanner] orphan CONFIRM on non-empty cart (%d items) — promoting to BUYER_PREORDER_CONFIRM",
            len(_orphan_cart),
        )
        updates["current_goal"] = "BUYER_PREORDER_CONFIRM"
        updates["detected_intent"] = "BUYER_PREORDER_CONFIRM"
        updates["goal_status"] = "ACTIVE"
        updates["working_memory"] = _lock("BUYER_PREORDER_CONFIRM")
        return _with_goal_metadata(updates, "BUYER_PREORDER_CONFIRM")

    # RÈGLE 5 — NEW_TASK (Instanciation et purge des champs AG-UI)
    #
    # (2026-09-08) `ANSWER`/`UPDATE` SANS tunnel actif entrent ici aussi —
    # voir le garde ajouté en RÈGLE 1bis. Hors tunnel, « répondre » ou
    # « corriger » n'a aucun référent : il n'y a rien à quoi répondre. Le
    # seul contenu exploitable du tour est alors l'intention détectée, et la
    # traiter comme une nouvelle tâche est la SEULE lecture cohérente — le
    # même traitement que RÈGLE 5 applique déjà à `NEW_TASK`, réutilisé tel
    # quel (purge transactionnelle + verrou incluses), jamais dupliqué.
    # `CONFIRM`/`SELECTION` restent volontairement EXCLUS dans le cas
    # général : « oui » ou « 2 » sans rien à confirmer ni menu affiché ne
    # portent aucune intention métier — les promouvoir inventerait une
    # tâche que l'utilisateur n'a pas demandée. Si `detected_intent` n'est
    # pas mappable (UNKNOWN), rien n'est promu et le tour retombe sur le
    # repli par défaut, inchangé.
    #
    # (2026-09-14, incident WhatsApp #8) : EXCEPTION étroite — `CONFIRM`/
    # `REJECT` visant un intent de `NAVIGATION_BREAKOUT_GOALS` DIFFÉRENT du
    # tunnel actuel EST promu. Observé en prod : après une interruption déjà
    # confirmée par la route SELECTION ("confirmer" sans rapport avec le
    # menu affiché), le classifieur de repli (legacy, moins fiable que le
    # micro-prompt) retourne parfois `disposition=CONFIRM` au lieu de
    # `NEW_TASK`, MAIS avec le bon intent identifié (`PRODUCER_CONFIRM_ORDER`)
    # — pas un intent inventé, une classification réelle du LLM, simplement
    # portée par la mauvaise étiquette de disposition. Rejeter ce résultat
    # renvoyait l'utilisateur au tunnel périmé en boucle. Restreint aux
    # intents déjà vetés pour l'interruption (jamais un intent arbitraire) :
    # même garde-fou que `nodes/cognitive.py`, pas une nouvelle catégorie de
    # confiance. (`_breakout_confirm` calculé en tête de fonction — RÈGLE
    # 1bis, plus haut, doit aussi le connaître pour ne pas reverrouiller le
    # tunnel avant que cette règle-ci ne puisse agir.)
    if (
        event == "NEW_TASK"
        or (not current_goal and event in {"ANSWER", "UPDATE"})
        or _breakout_confirm
    ):
        new_goal = INTENT_TO_GOAL_MAP.get(detected_intent)
        if new_goal:
            updates["current_goal"] = new_goal
            updates["goal_status"] = "ACTIVE"
            updates["working_memory"] = _lock(new_goal)
            # Une NOUVELLE tâche repart TOUJOURS d'un état transactionnel PROPRE.
            # Les slots de la tâche précédente (produit, quantité, prix...) ne
            # doivent JAMAIS fuiter dans la nouvelle — les entités du message
            # courant sont ré-appliquées juste après par memory_update
            # (extracted_entities), donc rien de légitime n'est perdu.
            #
            # Auparavant on ne purgeait que si le goal changeait, ou si le
            # produit différait. Deux failles : (1) deux demandes d'achat de
            # suite = même goal BUYER_REQUEST → pas de purge ; (2) une fois
            # "234 kg" fuité dans le payload sauvegardé AVEC le produit tomates,
            # la comparaison produit==produit ne purgeait plus jamais → fuite
            # auto-entretenue dans l'état persistant. Purge inconditionnelle =
            # résilience : impossible qu'un slot mort survive à une nouvelle
            # tâche. Une purge sur un payload déjà vide est sans effet.
            updates.update(_purge_transaction_state())
            logger.info("[GoalPlanner OUT] new_goal=%s", new_goal)
            logger.debug("[GoalPlanner OUT] new_goal=%s", new_goal)
            return _with_goal_metadata(updates)

    # Fallback par défaut vers clarification
    updates["current_goal"] = current_goal
    updates["response_strategy"] = "CLARIFICATION"
    updates["status"] = "WAITING_INPUT"
    updates["goal_status"] = "ACTIVE" if current_goal else "IDLE"
    if current_goal:
        updates["detected_intent"] = str(current_goal).upper()
    updates["working_memory"] = _lock(current_goal)
    return _with_goal_metadata(updates)


__all__ = [
    "INTENT_TO_GOAL_MAP",
    "_NAVIGATION_INTENTS",
    "_init_intent_to_goal_map",
    "_looks_like_buyer_product_request",
    "_extract_buyer_product",
    "_BUYER_PRODUCT_HINTS",
    "_BUYER_PRODUCT_EXCLUDES",
    "_BUYER_FILLER_WORDS",
    "goal_planner",
]
