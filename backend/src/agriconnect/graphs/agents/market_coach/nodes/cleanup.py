from __future__ import annotations

from typing import Any, Dict

from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    CART_TUNNEL_KINDS,
    InteractionKind,
    get_pending_interaction,
)

# NB : on N'IMPORTE PLUS `CLEANABLE_AFTER_RESPONSE`/`build_reset_patch` ici.
# Ce nœud est le DERNIER du graphe : l'état qu'il retourne EST celui que
# l'orchestrateur lit pour envoyer la réponse (final.get("final_response"),
# response_strategy, ag_ui_component). Réinitialiser ces champs de réponse
# ICI les remettait à None AVANT la lecture de l'orchestrateur → le bot
# envoyait littéralement "None" sur WhatsApp. Le nettoyage de ces champs est
# assuré au tour SUIVANT par input_normalizer (mécanisme historique).


# Bookkeeping keys the generic SELECTION-resolution machinery needs
# regardless of which specific menu is on screen — always safe/cheap to keep
# while a selection is pending.
_GENERIC_SELECTION_KEYS = (
    "available_mapping_kind",
    "disambiguation_pending",
    "disambiguation_trigger_id",
)

# Per-flow rendered-menu TEXT caches. Each one can only legitimately be
# "the menu currently awaiting a reply" for the specific goal(s) that write
# it — see `_MENU_CACHE_OWNERS` below.
_MENU_CACHE_KEYS = (
    "auction_menu",
    "bids_menu",
    "stocks_menu",
    "generic_menu",
)

_VOLATILE_WORKING_KEYS = _GENERIC_SELECTION_KEYS + _MENU_CACHE_KEYS

# Goal(s) that legitimately own each menu-cache key. Previously ALL FOUR
# keys were blanket-preserved whenever ANY selection menu was pending
# (`keep_selection_channel`), regardless of which one was actually shown
# this turn — so a stale menu cache set by an unrelated flow earlier in a
# long conversation (e.g. `bids_menu` from viewing offers) never got
# cleared as long as *some* selection menu kept coming up later (a cart
# menu, a disambiguation menu...). Combined with the raw-data caches that
# used to ride alongside these text menus (`bids_cache`/`stocks_cache`,
# removed at the source — see procurement.py/flow.py), this single-turn
# checkpoint state ballooned to 665KB and blew the WorkspaceCheckpointer's
# 480KB limit, wiping the whole tunnel (cart/goal/draft) for the next turn.
# Scoping preservation to the goal that actually owns the key closes the
# general leak, not just the one instance that happened to be raw data.
_MENU_CACHE_OWNERS: Dict[str, frozenset] = {
    "bids_menu": frozenset({"MARKET_GET_REQUEST_DETAIL", "SALES_ACCEPT_CONTRACT"}),
    "stocks_menu": frozenset(
        {
            "STOCK_ADJUST",
            "STOCK_REMOVE_PARTIAL",
            "STOCK_RECORD_MOVEMENT",
            "STOCK_DELETE",
        }
    ),
    # auction_menu / generic_menu: no reachable code path currently writes
    # them with real content (dead references kept only for backward
    # compatibility with any checkpoint that still carries them) — never
    # preserved.
}

_MERGE_DICT_RESET = {"__reset__": True}

# merge_dict fields that are recomputed every turn and must NOT accumulate
# across turns. Without this reset, the workspace grows ~5-10 KB per turn
# and hits the 480 KB WorkspaceCheckpointer limit after ~6 turns, causing
# state truncation that loses vendor_selection_context and breaks flows.
_EPHEMERAL_MERGE_DICT_FIELDS = (
    "extracted_entities",
    "raw_analysis",
    "cognitive_decision",
    "execution_result",
    "selected_tool_args",
    "interruption_payload",
    "volatile_entities",
)

# replace_value ephemeral fields safe to clear after response is sent
_EPHEMERAL_REPLACE_FIELDS = {
    "interpreted_event": None,
    "detected_intent": None,
    "interpreter_confidence": None,
    "validation_status": None,
    "pending_goal": None,
    "current_goal": None,
    "selected_tool": None,
    "retry_count": 0,
    "confirmation_summary": None,
    # Suivent `confirmation_summary` dans TOUS les cas (même préservation,
    # même reset) — voir `_confirmation_preserved` ci-dessous et l'incident
    # "champignons/chèvres" (2026-08-27, nodes/rendering/confirm.py).
    "confirmation_summary_goal": None,
    "confirmation_summary_payload": None,
    "confirmation_raised_at": None,
    # Note d'écart LLM (voir confirmation_gate.py::_llm_deviation_reply) —
    # strictement mono-tour, JAMAIS préservée même quand la confirmation elle-
    # même continue : sinon la même remarque reviendrait collée devant le
    # récap à chaque tour suivant.
    "confirmation_deviation_note": None,
    "execution_authorized": False,
    "is_certified": False,
    "is_locked": False,
    "should_interrupt": False,
    "interruption_detected": False,
    "interruption_type": None,
    "security_reason": None,
    "requires_human": False,
    # (2026-09-08, P1-4 audit architectural) : `replace_value` — une clé
    # ABSENTE du patch d'un nœud qui n'a pas crashé/bloqué ce tour laisse la
    # valeur ANCIENNE inchangée (contrairement à `merge_dict`, rien ne
    # "vide" implicitement un `replace_value`). Sans ce reset explicite, le
    # message d'un crash ou le texte d'une tentative d'injection resterait
    # collé dans l'état pour TOUS les tours suivants, réussis ou non —
    # maintenant qu'ils sont déclarés (donc RÉELLEMENT persistés par le
    # graphe compilé), cette hygiène de fin de tour devient nécessaire.
    "blocked_user_query": None,
    "error_message": None,
    "technical_details": None,
    "pending_menu": None,
    "reply_audio_url": None,
    "proactive_hint": None,
    # Payload de clic interactif : strictement mono-tour. Sans reset, un clic
    # persisté re-court-circuiterait l'interpréteur au tour suivant (texte libre
    # ignoré). Voir input_interpreter bypass + [[market-coach-turn-boundary-state]].
    "interactive_selection": None,
    # Position GPS reçue ce tour (webhook) : strictement mono-tour, même
    # principe que interactive_selection — sans reset, un True persisté
    # ferait croire à l'onboarding qu'une position vient d'arriver à un tour
    # ultérieur sans rapport.
    "location_shared": False,
    # (2026-09-02) Même mono-tour que location_shared ci-dessus — l'issue de
    # CE tour ne doit jamais être relue comme si elle décrivait un tour
    # ultérieur.
    "location_outcome": None,
    "location_lat": None,
    "location_lon": None,
    # (2026-09-08, P1-4 audit architectural) : consommés UNE FOIS par
    # `rendering/success.py` dans le tour même où `ensure_farm_node` les
    # produit — voir core/state_profile.py (déclarés EPHEMERAL). Sans ce
    # reset, un avertissement "j'ai configuré votre ferme" pourrait
    # ressurgir sur un tour ultérieur sans rapport.
    "auto_farm_notice": None,
    "error_creating_farm": False,
    # (2026-09-08, P1-3 audit architectural) : voir core/state.py — décision
    # mono-tour, ne doit jamais survivre pour influencer un tour ultérieur
    # sans rapport.
    "slot_enrichment_force_clarification": None,
    "clarification_reasons": None,
}


async def post_response_cleanup(
    state: Dict[str, Any], mc_runtime: Any
) -> Dict[str, Any]:
    pending = state.get("pending_cleanup")
    status = str(state.get("status") or "").upper().strip()
    # (2026-09-02, "no legacy shim") : plus de lecture de `expected_input` —
    # `pending_interaction` (déjà DURABLE, survit nativement au checkpoint)
    # est la seule source pour savoir QUEL canal garder vivant.
    pending_kind = get_pending_interaction(state).kind

    patch: Dict[str, Any] = {}

    keep_selection_channel = status == "WAITING_INPUT" and (
        pending_kind == InteractionKind.SELECTION_MENU
        or pending_kind in CART_TUNNEL_KINDS
    )

    # When the turn parks the transaction awaiting explicit confirmation, the
    # confirmation channel (waiting_for_confirmation / confirmation_summary) MUST
    # survive to the next turn — otherwise confirmation_gate re-asks forever and
    # the user's "Oui" never triggers execution.
    keep_confirmation_channel = status == "WAITING_CONFIRMATION"

    # BUG RÉEL, CONFIRMÉ PAR LOGS (2026-08-15) : ce garde-fou ne couvrait QUE
    # les canaux SELECTION et CONFIRMATION — pas le cas, bien plus courant,
    # d'un formulaire générique en attente d'UN CHAMP précis (prix, quantité,
    # date limite...) via ASK_MISSING_FIELD (`pending_interaction.kind ==
    # ENTER_FIELD`, status="WAITING_INPUT"). Sans ce troisième canal,
    # `current_goal` ci-dessous était effacé après CHAQUE tour d'appel
    # d'offres/vente/déclaration de culture — le tour suivant démarrait avec
    # goal_planner voyant `current_goal=None`, alors même que l'opération
    # était en plein milieu. Le formulaire semblait limper (une question
    # suivante cohérente pouvait quand même s'afficher via d'autres champs
    # survivants) mais ne pouvait jamais réellement s'exécuter, et tournait
    # en boucle entre les mêmes questions. Même famille de bug que
    # [[market-coach-turn-boundary-state]], nouvelle instance jamais corrigée
    # jusqu'ici. Voir [[precommande-architecture-consolidation-2026-08]].
    keep_field_channel = (
        status == "WAITING_INPUT" and pending_kind == InteractionKind.ENTER_FIELD
    )

    working = dict(state.get("working_memory") or {})
    if working:
        current_goal = str(state.get("current_goal") or "").upper().strip()
        preserved: set = set()
        if keep_selection_channel:
            preserved.update(_GENERIC_SELECTION_KEYS)
            for key, owner_goals in _MENU_CACHE_OWNERS.items():
                if current_goal in owner_goals:
                    preserved.add(key)
        for key in _VOLATILE_WORKING_KEYS:
            if key not in preserved:
                working[key] = None
        patch["working_memory"] = working

    if not keep_selection_channel:
        patch["available_mapping"] = {}
        patch["expected_candidates"] = []

    if isinstance(pending, dict) and pending:
        patch.update(dict(pending))

    patch["pending_cleanup"] = None

    # Reset merge_dict ephemeral fields to prevent unbounded state growth.
    for field in _EPHEMERAL_MERGE_DICT_FIELDS:
        current = state.get(field)
        if isinstance(current, dict) and current and not current.get("__reset__"):
            patch[field] = _MERGE_DICT_RESET

    # Reset replace_value ephemeral fields
    # NB : `current_goal` DOIT survivre tant qu'une confirmation générique OU
    # un tunnel auto-géré basé sur un menu (SELECTION — ex: précommande
    # panier) est légitimement en attente — sans ça, le tour suivant repart
    # avec goal="" : confirmation_gate reconstruit un récap vide ("Validation
    # de l'opération : (quantité : 973 KG...)"), ou un tunnel SELECTION comme
    # la précommande n'est plus reconnu par le DomainRouter (goal absent des
    # règles) et tombe dans confirmation_gate avec un goal vide — même bug,
    # deux portes d'entrée. `confirmation_raised_at` doit survivre pour le
    # garde-fou de péremption (voir confirmation_gate.py).
    _confirmation_preserved = {
        "confirmation_summary",
        "confirmation_summary_goal",
        "confirmation_summary_payload",
        "current_goal",
        "confirmation_raised_at",
    }
    _keep_goal_channel = (
        keep_confirmation_channel or keep_selection_channel or keep_field_channel
    )
    for field, default in _EPHEMERAL_REPLACE_FIELDS.items():
        # `retry_count` suit EXACTEMENT le sort de `current_goal` : c'est le
        # compteur d'échecs de compréhension DU TUNNEL COURANT (voir
        # cognitive_guard::recover_active_tunnel). Le remettre à 0 en fin de
        # CHAQUE tour — ce qui était le cas — rendait le seuil d'abandon
        # (`retry_count >= 2`) inatteignable, donc la seule sortie de secours
        # automatique de l'agent était du CODE MORT : un utilisateur dont les
        # messages ne sont pas classifiables restait piégé indéfiniment dans
        # le même tunnel. Il doit vivre tant que le tunnel vit, et disparaître
        # avec lui (goal terminé/changé → le reset générique ci-dessous
        # s'applique de nouveau et remet le compteur à 0 pour l'opération
        # suivante).
        if field in ("current_goal", "retry_count"):
            if _keep_goal_channel:
                continue
        elif keep_confirmation_channel and field in _confirmation_preserved:
            continue
        current = state.get(field)
        if current is not None and current != default:
            patch[field] = default

    # IMPORTANT : NE PAS réinitialiser ici les champs de génération de réponse
    # (final_response, ag_ui_component, response_strategy, onboarding_prompt,
    # reply_audio_url). Ce nœud tourne en DERNIER : l'orchestrateur lit ces
    # champs sur l'état qu'on retourne pour ENVOYER la réponse. Les vider ici
    # ferait envoyer "None" à l'utilisateur. Ils sont nettoyés au tour suivant
    # par input_normalizer.

    return patch
