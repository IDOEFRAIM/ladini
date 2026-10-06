"""Buyer recurring supply flow — CREATE_RECURRING_NEED / UPDATE_RECURRING_NEED / GET_MY_NEEDS
(Phase 2). Point d'entrée du tunnel `recurring_need` (`core/goals.py`), appelé depuis
`buyer_context_resolver` (`flows/buyer/flow.py`).

Même discipline transactionnelle que PREORDER (`preorder_confirmation.py`) — pas de nœud
`mcp_tool_executor` séparé pour la création : `RecurringSupplyGateway.create_recurring_need` est
appelé DIRECTEMENT dans ce nœud, dans la MÊME fonction que la décision de confirmation.
`EXECUTING` est persisté (CAS, `RecurringNeedDraft.version`) AVANT l'appel MCP, jamais après.

`UPDATE_RECURRING_NEED`/`GET_MY_NEEDS` n'ont pas de draft (l'objet modifié/lu est déjà persisté,
identifié conversationnellement — même choix que `PROCUREMENT_UPDATE_REQUEST`/`BUYER_LIST_AUCTIONS`,
voir le rapport de Phase 2) : l'action est appliquée directement via le même gateway.

Mandat §18 : aucune réponse utilisateur ne mentionne occurrence/CAS/recurring_need/version.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, cast

from ladini.core.idempotency import release
from ladini.domain.quantity_unit import default_unit_for_product
from ladini.domain.recurring_supply.ambiguous_group import (
    AmbiguousResolutionKind,
    resolve_ambiguous_group_reply,
)
from ladini.domain.recurring_supply.correction_scope import (
    CorrectionScopeResolutionKind,
    resolve_correction_scope_reply,
)
from ladini.domain.recurring_supply.digest import AllocationLine, build_detail_text
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    clear_pending_interaction,
    get_pending_interaction,
    resolve_pending_interaction,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.core.state import (
    entities_said_this_turn,
    resolve_current_goal,
)
from ladini.graphs.agents.market_coach.core.turn_policy import (
    TurnAction,
    decide_active_draft_reply,
)
from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
    CancelRecurringNeedDraft,
    ConfirmRecurringNeedDraft,
    NoRecurringNeedAction,
    RecurringNeedDraft,
    RecurringNeedDraftStatus,
    RecurringNeedExecutionResult,
    RecurringNeedOutcome,
    RecurringNeedOutcomeKind,
    UpdateRecurringNeedDraft,
    apply_domain_action,
    build_response_plan,
    confirm_claim_key,
    execution_key,
    finalize_after_execution,
    plan_correction,
    resolve_domain_action,
)
from ladini.graphs.agents.market_coach.interpreter.entities import (
    _sanitize_product_candidate,
)
from ladini.graphs.agents.market_coach.services.mcp.gateway import (
    MCPCallError,
    RecurringSupplyGateway,
)
from ladini.graphs.agents.market_coach.utils import (
    MarketRuntime,
    canonical_unit_label,
    slot_has_value,
)
from ladini.services.database import recurring_need_draft_store
from ladini.services.database.draft_store_support import cas_finalize
from ladini.services.database.recurring_supply import MATCH_RESPONSE_ACTIONS

logger = logging.getLogger("Ladini.Market.RecurringNeed")

# Menu "mes besoins" / détail (mandat Phase 4 §6) — même durée que la confirmation générique
# (`nodes/confirmation_gate.py::_CONFIRMATION_TTL_SECONDS`) : passé ce délai, une réponse numérique
# ne cible plus rien de fiable, on réaffiche la liste plutôt que d'appliquer un choix périmé.
_MENU_TTL_SECONDS = 600.0
#: B23 — réponses TEXTUELLES FERMÉES (comparées en entier, jamais en sous-chaîne) qui ramènent à la liste. Avant B23 une
#: sous-chaîne (« besoin ») capturait « j'ai besoin de 2 chèvres chaque semaine » comme un « retour ».
_BACK_WORDS = frozenset({"retour", "mes besoins", "voir mes besoins", "liste de mes besoins", "mes besoins recurrents"})
#: Libellés des entrées d'un menu (action -> libellé) : ils sont publiés avec le menu pour que l'interprétation d'un message
#: libre voie CE que l'écran propose (le menu est une ATTENTE qui guide l'interprétation, jamais le moteur d'intention).
_ACTION_LABELS = {
    "REFRESH": "Rechercher maintenant", "LIST": "Retour", "CONFIRM": "Accepter la proposition",
    "REJECT": "Refuser la proposition", "ORDERS": "Voir les commandes", "VIEW": "Voir la prochaine livraison",
    "EXEC": "Confirmer", "ASK": "Continuer",
}

_DRAFT_FIELDS = (
    "product",
    "quantity",
    "unit",
    "additional_items",
    "recurrence_type",
    "weekly_days",
    "excluded_weekdays",
    "starts_at",
    "ends_at",
    "max_price_per_unit",
)


def _clean_additional_items(raw: Any) -> List[Dict[str, Any]]:
    """Ne garde que les items `{product, quantity, unit}` exploitables (chantier
    multi-produits, 2026-09-23) — un item sans produit ni quantité n'a rien de
    fiable à créer comme besoin récurrent à part entière et reste silencieusement
    ignoré ICI (même principe que `additional_products`, jamais pire), mais un
    item avec produit+quantité n'est plus jamais perdu — c'était le bug réel :
    "10 kg de tomate et 20 kg d'oignon tous les jours" ne créait qu'un draft
    tomate.

    Une unité manquante n'est PLUS un motif de rejet (bug réel production
    2026-09-24 : "14 coqs et 20 chèvres chaque semaine" perdait la chèvre —
    l'interpréteur ne devine jamais d'unité littéralement absente du message,
    voir `new_task_prompts.py`, mais le slot `unit` de premier niveau bénéficie
    déjà d'un défaut par produit via `default_unit_for_product`/`core/slots.py`
    quand l'animal se compte en TÊTE ; un item `additional_items` n'a jamais
    reçu ce même filet, alors qu'il suit exactement la même règle métier)."""
    if not isinstance(raw, list):
        return []
    cleaned: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        product, quantity, unit = item.get("product"), item.get("quantity"), item.get("unit")
        if slot_has_value(product) and slot_has_value(quantity):
            # (Phase 2 hardening, commit 8, P1 audit 2026-09-24) : un `unit` littéral extrait
            # par le LLM pour un item ADDITIONNEL passait tel quel (`str(unit).strip()`), sans
            # jamais traverser `canonical_unit_label` — le MÊME normalisateur que l'item
            # PRINCIPAL (via `nodes/memory.py::_resolve_unit_value`, qui l'appelle aussi). Deux
            # items du même draft pouvaient donc porter deux graphies différentes de la même
            # unité ("tête" brut vs "TETE" canonique), invisibles à toute comparaison littérale
            # ultérieure (regroupement, dédup, tests d'unité partagée).
            resolved_unit = (
                canonical_unit_label(unit) if slot_has_value(unit) else default_unit_for_product(product)
            )
            cleaned.append({"product": str(product).strip(), "quantity": quantity, "unit": resolved_unit})
    return cleaned


def _fmt_ambiguous_num(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return str(value or "")


# Lifecycle de clarification `ambiguous_groups` (mandat 2026-09-24, suite du correctif
# "coq/moutons/chèvres") : la première question ("TOTAL ou DE CHAQUE ?") était déjà correcte,
# mais RIEN dans ce fichier ne savait consommer la réponse qui la suit — `state_router.py`
# envoie TOUJOURS ce genre de réponse libre vers la route NEW_TASK (le nom de champ
# "ambiguous_quantity" n'est pas dans `core/slots.py::SLOT_FILLING_INPUTS`, donc jamais éligible
# à ACTIVE_SLOT), donc la réponse est reclassée comme un message totalement neuf, sans aucun lien
# avec la question posée. Trois conséquences observées : (1) `ambiguous_groups` restait "collant"
# d'un tour à l'autre (fixé séparément dans `interpreter/new_task_micro.py::_finalize` — un canal
# `merge_dict` qui n'efface jamais une clé omise) ; (2) une réponse qui résolvait pourtant
# clairement l'ambiguïté ("50 moutons et 7 chèvres") tombait sur le prompt générique "il me faut
# juste ambiguous_quantity" ; (3) une tâche autonome totalement neuve pendant la clarification
# restait bloquée derrière elle.
#
# Design : le groupe ambigu (total + candidats) est stocké dans `PendingInteraction.target` —
# structuré (mandat §3), jamais aplati dans un slot scalaire — au moment où la question est
# posée. Au tour suivant, `_resolve_ambiguous_group_reply` (appelé AVANT toute autre logique de
# `_create_flow`) tente de résoudre le texte libre contre CE groupe précis
# (`domain/recurring_supply/ambiguous_group.py`, mécanique générale pour N candidats — jamais un
# patch textuel "mouton/chèvre"). `None` signifie "ce message ne répond pas à la clarification" :
# l'appelant le traite alors comme une tâche autonome distincte (mandat §8), sans qu'aucune
# action explicite ne soit nécessaire ici pour "laisser gagner" la nouvelle tâche.
_ABANDON_PHRASES = ("laisse tomber", "laisse", "annule", "oublie ça", "oublie ca")


def _is_bare_abandon(text: str) -> bool:
    """Un abandon PUR ("laisse tomber", seul) — pas "laisse, je veux 30 poulets..." (mandat §9),
    qui doit au contraire retomber sur le traitement NEW_TASK normal ci-dessous pour construire
    le nouveau draft, sans message d'annulation dédié."""
    folded = text.strip().lower().strip(" .!")
    for phrase in sorted(_ABANDON_PHRASES, key=len, reverse=True):
        if folded == phrase:
            return True
    return False


def _ambiguous_quantity_clarification(
    payload: Dict[str, Any], draft: Optional[RecurringNeedDraft]
) -> Optional[Dict[str, Any]]:
    """Bug réel production (2026-09-24) : "14 coq et 57 moutons chèvres chaque semaine"
    retombait sur le catalogue — cause racine distincte (voir `new_task_contract.py::
    NewTaskAmbiguousGroup`), mais la donnée elle-même ("57 moutons chèvres") reste ambiguë
    même une fois correctement classée CREATE_RECURRING_NEED : UNE quantité pour PLUSIEURS
    produits, sans mot indiquant un total ou une quantité par produit. Ni fusionnée en un
    produit incohérent, ni répartie en devinant — on demande, `None` si rien d'ambigu.

    `draft` : le draft DÉJÀ mis à jour avec le reste du message (produit principal, quantité,
    récurrence...) AVANT cet appel — persisté tel quel dans la réponse pour que le tour suivant
    (résolution de l'ambiguïté) le retrouve intact, au lieu de le construire seulement APRÈS
    résolution (qui perdait alors le produit principal, jamais rattaché à aucun draft)."""
    groups = payload.get("ambiguous_groups")
    if not isinstance(groups, list) or not groups:
        return None
    group = groups[0]
    if not isinstance(group, dict):
        return None
    candidates = [str(c).strip() for c in (group.get("candidates") or []) if slot_has_value(c)]
    if len(candidates) < 2:
        return None

    total_quantity = group.get("quantity")
    qty_text = _fmt_ambiguous_num(total_quantity)
    unit = group.get("unit")
    unit_text = f" {unit}" if slot_has_value(unit) else ""
    labels = [c.capitalize() for c in candidates]
    message = (
        f"{qty_text}{unit_text} pour {' et '.join(labels)} — c'est {qty_text} au TOTAL à "
        f"répartir entre les deux, ou {qty_text} DE CHAQUE ({', '.join(labels)}) ?\n\n"
        "Merci de préciser une quantité pour chaque produit."
    )
    return {
        "final_response": message,
        "response_strategy": "CLARIFICATION",
        "status": "WAITING_INPUT",
        "recurring_need_draft": draft.to_dict() if draft is not None else None,
        **set_pending_interaction(
            InteractionKind.ENTER_FIELD,
            goal="CREATE_RECURRING_NEED",
            field_name="ambiguous_quantity",
            candidates=tuple(candidates),
            target={"total_quantity": total_quantity, "unit": unit, "candidates": candidates},
        ),
    }


async def _resolve_ambiguous_group_reply(
    state: Dict[str, Any], pending: Dict[str, Any], conversation_id: str
) -> Optional[Dict[str, Any]]:
    """Traite un tour où `PendingInteraction` attend une résolution `ambiguous_quantity` (mandat
    §1-§10). Retourne un patch state si CE message concerne la clarification (résolu, invalide,
    ou abandon explicite) ; `None` si le message n'y répond manifestement pas — l'appelant le
    traite alors comme une tâche autonome distincte (mandat §8 : "le PendingInteraction ne doit
    pas gagner automatiquement")."""
    target = pending.get("target") if isinstance(pending, dict) else None
    if not isinstance(target, dict):
        return None
    candidates = [str(c).strip() for c in (target.get("candidates") or []) if slot_has_value(c)]
    total_quantity = target.get("total_quantity")
    if len(candidates) < 2 or not slot_has_value(total_quantity):
        return None

    text = str(state.get("normalized_text") or state.get("user_query") or "")
    draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))

    if _is_bare_abandon(text):
        if draft is not None and draft.status.value == "DRAFT":
            await _persist(draft, apply_domain_action(draft, CancelRecurringNeedDraft()).draft, conversation_id)
        # (Phase 2 hardening, commit 7) : un draft CANCELLED (terminal, C4) n'a plus vocation à
        # rester "le" draft actif de l'état — sinon il ressurgit et se fait réutiliser par erreur
        # au tour suivant (voir exactement la même règle appliquée par `_apply_response_plan`,
        # `keep_draft = plan.draft is not None and not plan.draft.is_terminal()` — ce chemin
        # d'annulation, écrit à la main plutôt que via `build_response_plan`, avait divergé de
        # cette convention en conservant le dict du draft déjà terminal).
        return {
            "final_response": "D'accord, j'annule cette demande.",
            "response_strategy": "SUCCESS",
            "status": "COMPLETED",
            "recurring_need_draft": None,
            **clear_pending_interaction("ambiguous_group_abandoned"),
        }

    resolution = resolve_ambiguous_group_reply(
        text, total_quantity=float(total_quantity), candidates=candidates
    )
    if resolution.kind == AmbiguousResolutionKind.NOT_A_RESOLUTION:
        return None

    if resolution.kind == AmbiguousResolutionKind.INVALID:
        return {
            "final_response": resolution.message,
            "response_strategy": "CLARIFICATION",
            "status": "WAITING_INPUT",
            "recurring_need_draft": draft.to_dict() if draft is not None else None,
            **set_pending_interaction(
                InteractionKind.ENTER_FIELD,
                goal="CREATE_RECURRING_NEED",
                field_name="ambiguous_quantity",
                candidates=tuple(candidates),
                target=target,
            ),
        }

    # RESOLVED — fusionne les allocations dans `additional_items`, sur le draft EXISTANT (produit
    # principal déjà présent depuis le tour de la question, mandat §10 "reprise du draft" : jamais
    # un second draft indépendant). L'unité du GROUPE ambigu ("unite", générique — voir le prompt
    # `new_task_prompts.py`, jamais une unité littérale PAR produit) n'est jamais utilisée telle
    # quelle : même règle que `_clean_additional_items` — l'unité canonique dépend du PRODUIT
    # résolu (TETE pour l'élevage, KG sinon), pas d'un champ générique posé avant même de savoir
    # de quels produits il s'agissait.
    existing_items = list(draft.additional_items or []) if draft is not None else []
    new_items = [
        {"product": product, "quantity": qty, "unit": default_unit_for_product(product)}
        for product, qty in resolution.allocations.items()
    ]
    action = UpdateRecurringNeedDraft(fields={"additional_items": existing_items + new_items})
    outcome = apply_domain_action(draft, action)
    await _persist(draft, outcome.draft, conversation_id)
    plan = build_response_plan(outcome)
    return _apply_response_plan(plan)


def _orphan_quantity_clarification(
    payload: Dict[str, Any], draft: Optional[RecurringNeedDraft]
) -> Optional[Dict[str, Any]]:
    """Bug réel production (2026-09-26, "150 kg tomate et 200 kg chaque semaine") : une
    DEUXIÈME quantité mentionnée dans le message SANS AUCUN produit qui lui soit rattaché (voir
    `new_task_contract.py::NewTaskOrphanQuantity`) — ni fusionnée dans la quantité du produit
    déjà connu (l'ancien bug : 150+200 devenait 350 de tomates), ni silencieusement perdue. On
    demande à quel produit elle correspond, `None` si rien d'orphelin dans ce tour.

    `draft` : même contrat que `_ambiguous_quantity_clarification` — le draft DÉJÀ mis à jour
    avec le reste du message (produit principal, quantité, récurrence...) AVANT cet appel,
    persisté tel quel dans la réponse pour que le tour suivant (le produit manquant) le
    retrouve intact plutôt que de repartir d'un draft vide."""
    orphans = payload.get("orphan_quantities")
    if not isinstance(orphans, list) or not orphans:
        return None
    orphan = orphans[0]
    if not isinstance(orphan, dict):
        return None
    quantity = orphan.get("quantity")
    if not slot_has_value(quantity):
        return None
    unit = orphan.get("unit")
    qty_text = _fmt_ambiguous_num(quantity)
    unit_text = f" {unit}" if slot_has_value(unit) else ""

    known_prefix = ""
    if draft is not None and slot_has_value(draft.product) and slot_has_value(draft.quantity):
        known_unit = f" {draft.unit}" if slot_has_value(draft.unit) else ""
        known_prefix = (
            f"J'ai bien noté {_fmt_ambiguous_num(draft.quantity)}{known_unit} de "
            f"{draft.product}. "
        )
    message = f"{known_prefix}À quel produit correspondent les {qty_text}{unit_text} ?"
    return {
        "final_response": message,
        "response_strategy": "CLARIFICATION",
        "status": "WAITING_INPUT",
        "recurring_need_draft": draft.to_dict() if draft is not None else None,
        **set_pending_interaction(
            InteractionKind.ENTER_FIELD,
            goal="CREATE_RECURRING_NEED",
            field_name="orphan_quantity",
            target={"quantity": quantity, "unit": unit},
        ),
    }


async def _resolve_orphan_quantity_reply(
    state: Dict[str, Any], pending: Dict[str, Any], conversation_id: str
) -> Optional[Dict[str, Any]]:
    """Traite un tour où `PendingInteraction` attend une résolution `orphan_quantity` (mandat
    §6-§7, 2026-09-26). Même contrat que `_resolve_ambiguous_group_reply` : `None` si le message
    ne répond manifestement pas à la clarification — l'appelant le traite alors comme une tâche
    autonome distincte (ex: "je veux 30 poulets chaque semaine" pendant la clarification,
    mandat §5 — jamais lu comme un nom de produit)."""
    target = pending.get("target") if isinstance(pending, dict) else None
    if not isinstance(target, dict):
        return None
    quantity = target.get("quantity")
    if not slot_has_value(quantity):
        return None
    unit = target.get("unit")

    text = str(state.get("normalized_text") or state.get("user_query") or "")
    draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))

    if _is_bare_abandon(text):
        if draft is not None and draft.status.value == "DRAFT":
            await _persist(draft, apply_domain_action(draft, CancelRecurringNeedDraft()).draft, conversation_id)
        return {
            "final_response": "D'accord, j'annule cette demande.",
            "response_strategy": "SUCCESS",
            "status": "COMPLETED",
            "recurring_need_draft": None,
            **clear_pending_interaction("orphan_quantity_abandoned"),
        }

    # Un chiffre dans la réponse est un signal fiable et GÉNÉRIQUE (jamais un mot précis en dur)
    # qu'il ne s'agit PAS d'un simple nom de produit répondant "à quel produit ?" — une tâche
    # autonome nouvelle en porte quasi toujours un (une quantité, une fréquence...), alors qu'un
    # nom de produit seul ("oignons", "des oignons") n'en porte jamais.
    if any(ch.isdigit() for ch in text):
        return None

    product = _sanitize_product_candidate(text)
    if not product:
        qty_text = _fmt_ambiguous_num(quantity)
        unit_text = f" {unit}" if slot_has_value(unit) else ""
        return {
            "final_response": (
                "Je n'ai pas reconnu de produit dans votre réponse. À quel produit "
                f"correspondent les {qty_text}{unit_text} ?"
            ),
            "response_strategy": "CLARIFICATION",
            "status": "WAITING_INPUT",
            "recurring_need_draft": draft.to_dict() if draft is not None else None,
            **set_pending_interaction(
                InteractionKind.ENTER_FIELD,
                goal="CREATE_RECURRING_NEED",
                field_name="orphan_quantity",
                target=target,
            ),
        }

    # RESOLVED — ajoute le produit nommé à `additional_items`, sur le draft EXISTANT (produit
    # principal déjà présent depuis le tour de la question, même règle que
    # `_resolve_ambiguous_group_reply` : jamais un second draft indépendant).
    existing_items = list(draft.additional_items or []) if draft is not None else []
    resolved_unit = (
        canonical_unit_label(unit) if slot_has_value(unit) else default_unit_for_product(product)
    )
    new_item = {"product": product, "quantity": quantity, "unit": resolved_unit}
    action = UpdateRecurringNeedDraft(fields={"additional_items": existing_items + [new_item]})
    outcome = apply_domain_action(draft, action)
    await _persist(draft, outcome.draft, conversation_id)
    return _apply_response_plan(build_response_plan(outcome))


async def _resolve_correction_scope_reply(
    state: Dict[str, Any], pending: Dict[str, Any], conversation_id: str
) -> Optional[Dict[str, Any]]:
    """Traite un tour où `PendingInteraction` attend une résolution `correction_scope` (mandat
    Phase 2 C7 §5/§13 — H2/C7). Même contrat que `_resolve_ambiguous_group_reply` : retourne un
    patch state si CE message répond à la question de portée (résolu ou abandon explicite),
    `None` si le message n'y répond manifestement pas — l'appelant le traite alors comme une
    tâche autonome distincte."""
    target = pending.get("target") if isinstance(pending, dict) else None
    if not isinstance(target, dict):
        return None
    fields = target.get("fields")
    if not isinstance(fields, dict) or not fields:
        return None
    candidates = [str(c).strip() for c in (pending.get("candidates") or []) if slot_has_value(c)]
    if len(candidates) < 2:
        return None

    text = str(state.get("normalized_text") or state.get("user_query") or "")
    draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))

    if _is_bare_abandon(text):
        if draft is not None and draft.status.value == "DRAFT":
            await _persist(draft, apply_domain_action(draft, CancelRecurringNeedDraft()).draft, conversation_id)
        return {
            "final_response": "D'accord, j'annule cette demande.",
            "response_strategy": "SUCCESS",
            "status": "COMPLETED",
            "recurring_need_draft": None,
            **clear_pending_interaction("correction_scope_abandoned"),
        }

    resolution = resolve_correction_scope_reply(text, candidates=candidates)
    if resolution.kind == CorrectionScopeResolutionKind.NOT_A_RESOLUTION:
        return None

    action = plan_correction(draft, fields, scope=resolution.scope)
    outcome = apply_domain_action(draft, action)
    await _persist(draft, outcome.draft, conversation_id)
    return _apply_response_plan(build_response_plan(outcome))


#: Table de dispatch des champs STRUCTURED de ce goal — DOIT couvrir exactement
#: `core/field_registry.py::STRUCTURED_FIELDS` restreint aux champs propriété de
#: CREATE_RECURRING_NEED (garanti par `tests/architecture/
#: test_pending_field_registry_completeness.py`, pas seulement par convention).
_STRUCTURED_FIELD_RESOLVERS = {
    "ambiguous_quantity": _resolve_ambiguous_group_reply,
    "correction_scope": _resolve_correction_scope_reply,
    "orphan_quantity": _resolve_orphan_quantity_reply,
}


async def recurring_need_flow(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    goal = str(state.get("current_goal") or "").upper()
    if goal == "CREATE_RECURRING_NEED":
        return await _create_flow(state, mc_runtime)
    if goal == "UPDATE_RECURRING_NEED":
        return await _update_flow(state, mc_runtime)
    if goal == "GET_MY_NEEDS":
        return await _get_my_needs_flow(state, mc_runtime)
    if goal == "REFRESH_RECURRING_MATCHING":
        return await _refresh_by_message(state, mc_runtime)
    logger.warning("recurring_need_flow: goal inattendu %s", goal)
    return {"status": "PLANNING", "final_response": "", "ag_ui_component": None}


# =====================================================================
# CREATE_RECURRING_NEED — draft conversationnel (CAS)
# =====================================================================


async def _create_flow(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    phone = state.get("user_phone")
    conversation_id = str(phone or state.get("session_id") or "")
    payload: Dict[str, Any] = state.get("transaction_payload") or {}
    interpreted_event = str(state.get("interpreted_event") or "").upper().strip()
    pending = state.get("pending_interaction") or {}
    pending_target = pending.get("target") if isinstance(pending, dict) else None

    # Réponse à une clarification STRUCTURED en attente (mandat Phase 2 C7 §5/§13 — H2) :
    # `core/field_registry.py::STRUCTURED_FIELDS` déclare QUELS champs de ce goal ont un
    # résolveur dédié capable de traiter la réponse même quand le classifieur générique renvoie
    # UNKNOWN (`nodes/cognitive.py` les exempte symétriquement du RECOVER générique — les deux
    # tables sont verrouillées ensemble par
    # `tests/architecture/test_pending_field_registry_completeness.py`).
    if isinstance(pending, dict) and pending.get("kind") == "ENTER_FIELD":
        resolver = _STRUCTURED_FIELD_RESOLVERS.get(str(pending.get("field") or ""))
        if resolver is not None:
            resolution_patch = await resolver(state, pending, conversation_id)
            if resolution_patch is not None:
                return resolution_patch

    draft = RecurringNeedDraft.from_dict(state.get("recurring_need_draft"))
    extracted = {k: v for k, v in payload.items() if k in _DRAFT_FIELDS and slot_has_value(v)}
    if "additional_items" in extracted:
        cleaned_items = _clean_additional_items(extracted["additional_items"])
        if cleaned_items:
            extracted["additional_items"] = cleaned_items
        else:
            extracted.pop("additional_items")

    said = _said_this_turn(state)
    if draft is not None and draft.status == RecurringNeedDraftStatus.DRAFT and _pending_targets(pending, draft):
        # (Phase 2.5, H7) : frontière AUTORITAIRE unique — voir
        # `core/turn_policy.py::decide_active_draft_reply`. Avant, `_is_correction`
        # ne regardait QUE l'absence de `recurrence_type` dans `said`, aveugle à la
        # confiance et à l'intent réellement détecté : un message hors-sujet à
        # faible confiance ("maïs", `confidence=0.4`, `detected_intent` différent du
        # goal actif) passait pour une correction et corrompait le draft actif.
        # `decide_active_draft_reply` a maintenant en main la classification RÉELLE
        # (jamais le fast-path pré-LLM, désactivé pour ce cas — voir
        # `interpreter/routing.py::_interpret_fast_path`) : CORRECT applique la
        # correction comme avant, CLARIFY ne mute RIEN (redemande confirmation du
        # draft INCHANGÉ plutôt que de deviner), et tout le reste retombe sur la
        # branche NEW_TASK ci-dessous (nouvelle tâche indépendante, ancien draft clos).
        #
        # `cognitive_decision.intent` — PAS `state["detected_intent"]` : dès que
        # `cognitive_guard` choisit CONTINUE_ACTIVE_GOAL (goal non interrompu),
        # `goal_planner` (RULE 2, plusieurs branches "re-verrouille le goal")
        # RÉÉCRIT `detected_intent` sur `current_goal` dans le patch d'état — un
        # choix délibéré pour le reste du tour, mais qui EFFACE, au moment où ce
        # flow s'exécute, le signal même dont cette décision a besoin (l'intent
        # ORIGINAL, avant tout verrouillage). `cognitive_decision.intent` est
        # écrit une fois par `cognitive_guard` et jamais retouché ensuite — seule
        # source encore fidèle à la classification réelle de CE tour.
        _turn_action = decide_active_draft_reply(
            interpreted_event=interpreted_event,
            said_entities=said,
            detected_intent=(state.get("cognitive_decision") or {}).get("intent"),
            current_goal=resolve_current_goal(state),
        )
        if _turn_action == TurnAction.CORRECT:
            # B24 : « finalement 5 », « plutôt chaque mois » — même goal, MÊME draft (aucun nouveau goal/draft).
            _arbitration_log(state, intent="CREATE_RECURRING_NEED", relation="CORRECTION", reason="contextual_correction",
                             resolution="current_draft", route="recurring_create", target_type="RECURRING_NEED_DRAFT")
            corrected = await _correct(draft, said, state, conversation_id)
            corrected["relation_to_context"] = "CORRECTION"
            return corrected
        if _turn_action == TurnAction.CLARIFY:
            _arbitration_log(state, intent="CREATE_RECURRING_NEED", relation="AMBIGUOUS",
                             reason="ambiguous_correction_vs_new_task", resolution="current_draft",
                             route="recurring_create", target_type="RECURRING_NEED_DRAFT")
            outcome = apply_domain_action(
                draft, NoRecurringNeedAction(reason="ambiguous_correction_vs_new_task")
            )
            clarify_patch = _apply_response_plan(build_response_plan(outcome))
            clarify_patch["relation_to_context"] = "AMBIGUOUS"
            return clarify_patch
        if _turn_action == TurnAction.NEW_TASK:
            _arbitration_log(state, intent="CREATE_RECURRING_NEED", relation="NEW_TASK", reason="explicit_new_task",
                             resolution="superseded_draft", route="recurring_create", target_type="RECURRING_NEED_DRAFT")

    if (
        interpreted_event in ("NEW_TASK", "INTERRUPTION")
        and draft is not None
        and draft.status == RecurringNeedDraftStatus.DRAFT
        and _repeats_live_draft(draft, extracted)
    ):
        # B22 : la MÊME demande rejouée (double envoi, message re-livré sous un autre identifiant) n'est pas une
        # nouvelle tâche — ni nouveau draft, ni nouveau récapitulatif : la confirmation déjà émise reste LA confirmation.
        domain_event = "ANSWER"
    elif interpreted_event in ("NEW_TASK", "INTERRUPTION"):
        if draft is not None and draft.status == RecurringNeedDraftStatus.DRAFT:
            # Nouvelle demande autonome : l'ancien draft éditable est CLOS durablement
            # (jamais laissé orphelin en DRAFT dans la table).
            await _persist(draft, apply_domain_action(draft, CancelRecurringNeedDraft()).draft, conversation_id)
        draft = None
        domain_event = "UPDATE" if extracted else "ANSWER"
    else:
        domain_event = interpreted_event or ("UPDATE" if extracted else "ANSWER")

    action = resolve_domain_action(
        interpreted_event=domain_event,
        # Un refus ne se juge que sur ce qui est dit MAINTENANT : le payload accumulé
        # (produit/quantité des tours précédents) ferait passer un simple « non » pour
        # une correction porteuse de valeurs.
        extracted_entities=said if domain_event == "REJECT" else extracted,
        pending_target=pending_target,
    )

    if isinstance(action, ConfirmRecurringNeedDraft) and draft is not None:
        # PostgreSQL est l'AUTORITÉ au moment de confirmer : l'état LangGraph n'est qu'une
        # projection (il peut être en retard d'un tour si un tour précédent a été perdu
        # après un COMMIT métier).
        authoritative = await recurring_need_draft_store.load(draft.draft_id)
        if authoritative is not None:
            draft = authoritative
        return await _confirm(draft, action, state, mc_runtime, conversation_id)

    outcome = apply_domain_action(draft, action)
    outcome, start_notice = await _apply_start_policy(outcome, mc_runtime)
    await _persist(draft, outcome.draft, conversation_id)
    if isinstance(action, UpdateRecurringNeedDraft):
        ambiguous_response = _ambiguous_quantity_clarification(payload, outcome.draft)
        if ambiguous_response is not None:
            return ambiguous_response
        orphan_response = _orphan_quantity_clarification(payload, outcome.draft)
        if orphan_response is not None:
            return orphan_response
    patch = _apply_response_plan(build_response_plan(outcome))
    if start_notice:
        # La confirmation affichée est rendue par `nodes/rendering/confirm.py`, qui place cette note AVANT le récapitulatif.
        patch["confirmation_deviation_note"] = start_notice
        if patch.get("final_response"):
            patch["final_response"] = f"{start_notice}\n\n{patch['final_response']}"
    return patch


async def _apply_start_policy(
    outcome: RecurringNeedOutcome, mc_runtime: MarketRuntime
) -> tuple[RecurringNeedOutcome, str]:
    """Aligne `starts_at` d'un brouillon COMPLET sur le délai minimal admin AVANT de l'afficher.

    Le domaine (`get_recurring_start_policy`) décide : la date annoncée dans le récapitulatif est la vraie date de
    première livraison, jamais « demain ». Une date demandée trop proche est repoussée et l'utilisateur en est
    informé ; sinon aucune explication superflue. Si la lecture échoue, le brouillon reste inchangé : le service de
    création applique de toute façon la même règle à l'écriture (autorité unique)."""
    draft = outcome.draft
    if draft is None or draft.status != RecurringNeedDraftStatus.DRAFT or not draft.is_complete():
        return outcome, ""
    try:
        raw = await RecurringSupplyGateway(mc_runtime).get_recurring_start_policy(starts_at=draft.starts_at)
    except Exception as exc:  # noqa: BLE001 - jamais bloquant : le service reste l'autorité à la création
        logger.warning("recurring_need.start_policy_unavailable | %s", exc)
        return outcome, ""
    data = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), dict) else raw
    if not isinstance(data, dict) or _is_business_failure(data) or not data.get("effective_start"):
        return outcome, ""
    effective = str(data["effective_start"])[:10]
    notice = ""
    if data.get("adjusted") and data.get("requested_start"):
        notice = (
            "Pour laisser le temps d'organiser l'approvisionnement, la première livraison peut commencer "
            f"au plus tôt le {_fmt_date_fr(effective) or effective}. Je la programme à cette date."
        )
    if draft.starts_at and str(draft.starts_at)[:10] == effective:
        return outcome, notice
    updated = apply_domain_action(draft, UpdateRecurringNeedDraft(fields={"starts_at": effective}))
    if updated.draft is None:
        return outcome, notice
    return RecurringNeedOutcome(kind=outcome.kind, draft=updated.draft, detail=outcome.detail), notice


def _same_slot(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return bool(a == b)


def _repeats_live_draft(draft: RecurringNeedDraft, extracted: Dict[str, Any]) -> bool:
    """Le message ne dit RIEN de plus que le draft en attente : produit, quantité, unité et fréquence identiques, et
    aucun autre champ dit qui diffère. Un message plus pauvre (« chèvre 3 unités » sans fréquence) ou différent reste
    une nouvelle tâche."""
    if not draft.is_complete():
        return False
    core = ("product", "quantity", "unit", "recurrence_type")
    if not all(slot_has_value(extracted.get(k)) and _same_slot(extracted.get(k), getattr(draft, k)) for k in core):
        return False
    return all(_same_slot(v, getattr(draft, k, None)) for k, v in extracted.items() if k not in core)


def _said_this_turn(state: Dict[str, Any]) -> Dict[str, Any]:
    """Champs du draft DITS dans CE message : `extracted_entities` du tour, moins les clés
    héritées par `cognitive_guard` (`cognitive_decision.carried_entities`). Jamais
    `transaction_payload`, qui accumule les tours précédents (fréquence, ancienne unité...)."""
    entities = entities_said_this_turn(state)
    said = {k: v for k, v in entities.items() if k in _DRAFT_FIELDS and slot_has_value(v)}
    if "additional_items" in said:
        cleaned = _clean_additional_items(said["additional_items"])
        if cleaned:
            said["additional_items"] = cleaned
        else:
            said.pop("additional_items")
    return said


def _pending_targets(pending: Any, draft: RecurringNeedDraft) -> bool:
    """La question en attente porte-t-elle sur CE draft ?"""
    if not isinstance(pending, dict):
        return False
    pending_dict: Dict[str, Any] = pending
    raw_target = pending_dict.get("target")
    target: Dict[str, Any] = raw_target if isinstance(raw_target, dict) else {}
    if pending_dict.get("kind") == "CONFIRM_ACTION":
        return bool(target.get("draft_id") == draft.draft_id)
    return pending_dict.get("kind") == "ENTER_FIELD" and pending_dict.get("goal") == "CREATE_RECURRING_NEED"


async def _correct(
    draft: RecurringNeedDraft, said: Dict[str, Any], state: Dict[str, Any], conversation_id: str
) -> Dict[str, Any]:
    scope = (state.get("extracted_entities") or {}).get("correction_scope")
    action = plan_correction(draft, said, scope=scope)
    outcome = apply_domain_action(draft, action)
    await _persist(draft, outcome.draft, conversation_id)
    patch = _apply_response_plan(build_response_plan(outcome))
    if outcome.kind == RecurringNeedOutcomeKind.NEEDS_CORRECTION_SCOPE:
        # La correction proposée est conservée dans la question (jamais jetée) : la réponse
        # ne fera que désigner sa portée.
        patch.update(
            set_pending_interaction(
                InteractionKind.ENTER_FIELD,
                goal="CREATE_RECURRING_NEED",
                field_name="correction_scope",
                candidates=tuple(getattr(action, "candidates", ()) or ()),
                target={"draft_id": draft.draft_id, "fields": said},
            )
        )
    return patch


async def _persist(
    before: Optional[RecurringNeedDraft], after: Optional[RecurringNeedDraft], conversation_id: str
) -> bool:
    """Persiste la transition `before -> after` (INSERT d'un nouveau draft, CAS sinon).
    `True` si l'état `after` est durable. Un échec n'est BLOQUANT que pour le passage en
    exécution (voir `_confirm`) ; pour une simple édition, le tour continue (mode dégradé,
    journalisé par le store)."""
    if after is None or after is before:
        return True
    if before is None or before.draft_id != after.draft_id:
        return bool(await recurring_need_draft_store.insert(after, conversation_id=conversation_id))
    if before.version == after.version:
        return True
    swapped = await recurring_need_draft_store.compare_and_swap(
        after.draft_id, expected_version=before.version, new_draft=after
    )
    if swapped:
        return True
    if await recurring_need_draft_store.load(after.draft_id) is None:
        # Draft né avant la persistance (ou insertion initiale perdue) : on l'enregistre.
        return bool(await recurring_need_draft_store.insert(after, conversation_id=conversation_id))
    return False


async def _confirm(
    draft: RecurringNeedDraft,
    action: ConfirmRecurringNeedDraft,
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
    conversation_id: str,
) -> Dict[str, Any]:
    outcome = apply_domain_action(draft, action)
    if outcome.kind == RecurringNeedOutcomeKind.CONFIRMED_READY_FOR_EXECUTION:
        # EXECUTING est durable AVANT tout appel MCP : sans registre durable, aucune
        # exécution (la garde PostgreSQL de `recurring_supply.py` l'exige de toute façon).
        if not await _persist(draft, outcome.draft, conversation_id):
            current = await recurring_need_draft_store.load(draft.draft_id)
            if current is not None and current.is_in_doubt():
                return await _execute(current, state, mc_runtime)  # une autre confirmation a gagné
            if current is not None and current.status == RecurringNeedDraftStatus.EXECUTED:
                return _apply_response_plan(
                    build_response_plan(
                        RecurringNeedOutcome(kind=RecurringNeedOutcomeKind.ALREADY_EXECUTED, draft=current)
                    )
                )
            release(confirm_claim_key(draft))  # sinon chaque « oui » suivant répondrait « en cours »
            logger.error("recurring_need.executing_not_durable | draft=%s", draft.draft_id)
            return {
                "final_response": (
                    "Un incident technique m'empêche d'enregistrer votre confirmation pour le moment. "
                    "Répondez *oui* dans un instant pour réessayer."
                ),
                "response_strategy": "CONFIRMATION",
                "status": "WAITING_INPUT",
                "recurring_need_draft": draft.to_dict(),
            }
        return await _execute(outcome.draft, state, mc_runtime)
    if outcome.kind == RecurringNeedOutcomeKind.RESUME_EXECUTION:
        return await _execute(outcome.draft, state, mc_runtime)
    return _apply_response_plan(build_response_plan(outcome))


def _is_business_failure(result: Any) -> bool:
    """Réponse MCP `{"status": "error"}` : le service a levé AVANT commit (transaction
    annulée) — échec DÉFINITIF, jamais un succès (bug réel : la réponse était lue comme
    un succès et l'utilisateur recevait « C'est noté ! » sans rien en base)."""
    return not isinstance(result, dict) or str(result.get("status") or "").lower() in {"error", "failed", "failure"}


def _created_ids(result: Dict[str, Any]) -> Optional[str]:
    if result.get("items"):
        return ",".join(str(it.get("recurring_need_id")) for it in result.get("items") or []) or None
    return result.get("recurring_need_id")


async def _execute(
    executing: RecurringNeedDraft, state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    """Exécute (ou reprend) LA confirmation `(draft_id, execution_version)`. Idempotent :
    la garde PostgreSQL rejoue le résultat si cette confirmation a déjà abouti."""
    phone = state.get("user_phone")
    gw = RecurringSupplyGateway(mc_runtime)
    common = {
        "phone": str(phone),
        "recurrence_type": executing.recurrence_type,
        "weekly_days": executing.weekly_days,
        "excluded_weekdays": executing.excluded_weekdays,
        "starts_at": executing.starts_at,
        "ends_at": executing.ends_at,
        "max_price_per_unit": executing.max_price_per_unit,
        "idempotency_key": execution_key(executing),
        "draft_id": executing.draft_id,
        "draft_version": executing.execution_version,
    }
    try:
        if executing.additional_items:
            items = [{"product_query": executing.product, "quantity": executing.quantity, "unit": executing.unit}] + [
                {"product_query": it.get("product"), "quantity": it.get("quantity"), "unit": it.get("unit")}
                for it in executing.additional_items
            ]
            mcp_result = await gw.create_recurring_needs(items=items, **common)
        else:
            mcp_result = await gw.create_recurring_need(
                product_query=executing.product, quantity=executing.quantity, unit=executing.unit, **common
            )
    except Exception as exc:  # transport/timeout : l'issue côté base est INCONNUE
        logger.warning("recurring_need.execution_ambiguous | draft=%s | %s", executing.draft_id, exc)
        return await _settle(executing, RecurringNeedExecutionResult(success=False, ambiguous=True, error=str(exc)))

    if _is_business_failure(mcp_result):
        logger.warning(
            "recurring_need.create_failed | draft=%s | %s", executing.draft_id, (mcp_result or {}).get("message")
        )
        return await _settle(
            executing, RecurringNeedExecutionResult(success=False, error=str((mcp_result or {}).get("message") or ""))
        )
    real_start = str(mcp_result.get("starts_at") or "")[:10]
    return await _settle(
        executing,
        RecurringNeedExecutionResult(success=True, external_id=_created_ids(mcp_result)),
        real_start=real_start or None,
    )


async def _settle(
    executing: RecurringNeedDraft, result: RecurringNeedExecutionResult, *, real_start: Optional[str] = None
) -> Dict[str, Any]:
    """Enregistre l'issue d'une exécution. La base peut déjà la connaître (la garde du
    service passe le draft à EXECUTED dans la transaction des besoins) : en cas de course
    perdue, la version relue fait foi."""
    if result.ambiguous and executing.status == RecurringNeedDraftStatus.EXECUTION_UNKNOWN:
        finalized = executing  # déjà en doute : rien de nouveau à écrire
    else:
        finalized = finalize_after_execution(executing, result)
    _persisted, final = await cas_finalize(
        compare_and_swap=recurring_need_draft_store.compare_and_swap,
        load=recurring_need_draft_store.load,
        original=executing,
        finalized=finalized,
    )
    if real_start and final.status == RecurringNeedDraftStatus.EXECUTED and str(final.starts_at or "")[:10] != real_start:
        # Le réglage admin a changé entre le récapitulatif et la confirmation : le service a retenu la date COURANTE ;
        # le message final affiche la vraie date (projection d'affichage — le besoin créé fait foi en base).
        final = dataclasses.replace(final, starts_at=real_start)
    kind = {
        RecurringNeedDraftStatus.EXECUTED: RecurringNeedOutcomeKind.RECURRING_NEED_CREATED,
        RecurringNeedDraftStatus.FAILED: RecurringNeedOutcomeKind.RECURRING_NEED_FAILED,
    }.get(final.status, RecurringNeedOutcomeKind.RECURRING_NEED_EXECUTION_UNKNOWN)
    return _apply_response_plan(build_response_plan(RecurringNeedOutcome(kind=kind, draft=final)))


def _apply_response_plan(plan) -> Dict[str, Any]:
    # Un draft TERMINAL (EXECUTED/FAILED/EXECUTION_UNKNOWN/CANCELLED) n'a plus
    # vocation à rester "le" draft actif du state — sinon il ressurgit et se
    # fait réutiliser par erreur sur le tour suivant (voir le repli NEW_TASK
    # ci-dessus, qui protège déjà contre ce cas ; ce reset est la seconde
    # ligne de défense, et empêche aussi l'état de grossir indéfiniment avec
    # de vieux drafts déjà finalisés).
    keep_draft = plan.draft is not None and not plan.draft.is_terminal()
    patch: Dict[str, Any] = {
        "final_response": plan.final_response,
        "response_strategy": plan.response_strategy,
        "status": plan.graph_status,
        "recurring_need_draft": plan.draft.to_dict() if keep_draft else None,
    }
    if plan.terminal_goal_reset:
        patch["current_goal"] = None
    if not plan.pending_untouched:
        if plan.pending_kind is None:
            patch.update(clear_pending_interaction("recurring_need_response_plan"))
        else:
            patch.update(
                set_pending_interaction(
                    InteractionKind[plan.pending_kind],
                    goal="CREATE_RECURRING_NEED",
                    field_name=plan.pending_field,
                    context_ref="confirmation" if plan.pending_kind == "CONFIRM_ACTION" else None,
                    target=plan.pending_target,
                )
            )
    return patch


# =====================================================================
# UPDATE_RECURRING_NEED — action structurée sur un besoin déjà créé
# =====================================================================


async def _update_flow(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    payload: Dict[str, Any] = state.get("transaction_payload") or {}

    # Tours SUIVANTS du mini-flow "modifier -> menu -> quantité -> override" (mandat digest
    # §5/§12) : ancrés par un `PendingInteraction(RECURRING_SUPPLY_DIGEST_ACTION)` durable —
    # VÉRIFIÉ EN PREMIER, avant tout `transaction_payload["action"]` ci-dessous. Raison
    # structurelle : `transaction_payload` n'est PAS réinitialisé entre les tours d'un même
    # goal verrouillé (seul un changement de goal le purge, `goal_planner.py::
    # _purge_transaction_state`) — le sentinel `action="DIGEST_MODIFY_MENU"` posé au tour où
    # "modifier" a été tapé restait donc visible aux tours SUIVANTS ("1", "40 kg"), et sans
    # cette priorité, `_update_flow` rouvrait le menu à chaque tour au lieu de faire progresser
    # le mini-flow déjà en cours (bug constaté en écrivant le test E2E du mandat §16 — voir
    # tests/integration/test_recurring_supply_digest_routing.py). Une fois ce `PendingInteraction`
    # actif, il fait autorité — jamais le sentinel `action`, qui ne décrit que le tour d'ORIGINE.
    _digest_pending = get_pending_interaction(state)
    if _digest_pending.kind == InteractionKind.RECURRING_SUPPLY_DIGEST_ACTION:
        return await _digest_modify_flow_continue(state, mc_runtime, _digest_pending)

    # Réponse au digest (mandat digest, VS4 pilote) : "CONFIRM_MATCH"/"REJECT_MATCH" — émis
    # UNIQUEMENT par `interpreter/routing.py::_bare_confirmation_for_recurring_supply_digest`,
    # jamais un intent séparé (budget de tokens du prompt LLM déjà saturé, voir son docstring).
    # Bifurque AVANT toute résolution par nom de produit : "CONFIRMER TOUT" s'applique à TOUS
    # les besoins actionnables de l'acheteur, jamais à un seul résolu par ambiguïté de nom.
    _payload_action = str(payload.get("action") or "").upper().strip()
    if _payload_action in ("CONFIRM_MATCH", "REJECT_MATCH"):
        return await _respond_to_digest_flow(
            state, mc_runtime, action="CONFIRM" if _payload_action == "CONFIRM_MATCH" else "REJECT"
        )

    # "modifier"/"changer" en réponse au digest (mandat digest §5, 2026-09-26) : ouvre le menu
    # numéroté des besoins de DEMAIN (émis par `interpreter/routing.py::
    # _bare_confirmation_for_recurring_supply_digest`, jamais un intent séparé — même
    # sentinel-dans-`action` que CONFIRM_MATCH/REJECT_MATCH ci-dessus).
    if _payload_action == "DIGEST_MODIFY_MENU":
        return await _digest_modify_flow_start(state, mc_runtime)

    # "pas demain pour l'oignon" (mandat digest §9) : skip d'UN SEUL besoin nommé, jamais tous
    # les besoins actionnables (`REJECT_MATCH` ci-dessus reste le chemin "pas demain" bare).
    if _payload_action == "DIGEST_SKIP_PRODUCT":
        return await _digest_skip_named_product(state, mc_runtime, str(payload.get("product") or ""))

    return await _update_from_message(state, mc_runtime)


# B24 — UPDATE depuis un message libre : intention (LLM) -> cible (contexte, revalidée) -> validation -> service.
# Le service `update_recurring_need` reste la seule autorité qui modifie la base ; il reçoit un ordre déterministe
# (`recurring_need_id`, action, valeurs validées), jamais le texte.
_UPDATE_FIELDS = ("product", "quantity", "recurrence_type", "weekly_days", "excluded_weekdays", "update_action")
#: B24 — actions « portée occurrence / statut du besoin » comprises depuis un message libre : elles ne s'exécutent JAMAIS sur
#: le message. Le flow publie un menu FERMÉ dont l'entrée « Oui » porte l'ordre exact (snapshot) ; seul un numéro/alias fermé
#: l'exécute, par le service existant `update_recurring_need`.
_COMMAND_ACTIONS = {
    "PAUSE": "PAUSE", "RESUME": "RESUME", "SKIP_OCCURRENCE": "OCCURRENCE_SKIP", "OVERRIDE_OCCURRENCE": "OCCURRENCE_OVERRIDE",
    "CANCEL": "CANCEL",
}
_PERMANENT_RECURRENCES = ("DAILY", "WEEKLY", "MONTHLY", "WEEKLY_DAYS")


def _update_request(state: Dict[str, Any]) -> Dict[str, Any]:
    """Ce que l'utilisateur dit dans CE message (jamais `transaction_payload`, qui accumule les tours précédents : une
    ancienne quantité y survit et serait réappliquée à l'aveugle)."""
    said = entities_said_this_turn(state)
    return {k: said[k] for k in _UPDATE_FIELDS if slot_has_value(said.get(k))}


# ── B26 : contrat de version (intention périmée) ───────────────────────────────────────────────────────────────────
# La version qui protège une mutation est celle de l'état PRÉSENTÉ à l'acheteur (écran de détail, liste, confirmation),
# jamais une relecture juste avant d'écrire. Elle vit dans `working_memory.recurring_need_menu` — `target.need_version` (+
# `occurrence_*`) pour un écran de détail, `versions` pour la liste, `commands[k].expected_version` pour un ordre à confirmer —
# donc indépendamment du TTL du menu (validité conversationnelle) : une réponse tardive reste comparée à l'écran vu.
# Sans écran présenté (demande « à froid »), la version lue dans CE tour est transmise (`same_turn_read`) : la fenêtre
# lecture -> écriture reste couverte par le compare-and-swap du service, mais il n'existe alors aucune intention périmée.
def _presented_need_version(state: Dict[str, Any], need_id: str) -> Optional[int]:
    menu = (state.get("working_memory") or {}).get("recurring_need_menu")
    if not isinstance(menu, dict):
        return None
    target = menu.get("target")
    if isinstance(target, dict) and str(target.get("id")) == need_id and target.get("need_version") is not None:
        return int(target["need_version"])
    versions = menu.get("versions")
    if isinstance(versions, dict) and versions.get(need_id) is not None:
        return int(versions[need_id])
    return None


def _presented_occurrence(state: Dict[str, Any], need_id: str) -> Optional[Dict[str, Any]]:
    """Livraison modifiable (OPEN/MATCHED) montrée par l'écran de détail de CE besoin : `{date, version}`, sinon `None`."""
    menu = (state.get("working_memory") or {}).get("recurring_need_menu")
    target = menu.get("target") if isinstance(menu, dict) else None
    if (isinstance(target, dict) and str(target.get("id")) == need_id and target.get("occurrence_version") is not None
            and target.get("occurrence_date") and target.get("occurrence_status") in ("OPEN", "MATCHED")):
        return {"date": str(target["occurrence_date"])[:10], "version": int(target["occurrence_version"])}
    return None


def _version_snapshot_patch(state: Dict[str, Any], need_id: str, need_version: Any) -> Dict[str, Any]:
    """Après une mutation RÉUSSIE de ce besoin, l'écran/la liste affichés sont périmés : le message de succès montre le nouvel
    état, donc la version présentée devient celle-là (sinon la demande suivante, légitime, serait refusée). Les versions
    d'occurrence présentées sont invalidées (la mutation les a réécrites). Rien n'est créé si aucun écran n'était présenté."""
    menu = (state.get("working_memory") or {}).get("recurring_need_menu")
    if not isinstance(menu, dict) or need_version is None:
        return {}
    patch: Dict[str, Any] = {}
    target = menu.get("target")
    if isinstance(target, dict) and str(target.get("id")) == need_id:
        patch["target"] = {"need_version": int(need_version), "occurrence_id": None, "occurrence_date": None,
                           "occurrence_status": None, "occurrence_version": None}
    versions = menu.get("versions")
    if isinstance(versions, dict) and need_id in versions:
        patch["versions"] = {need_id: int(need_version)}
    return {"working_memory": {"recurring_need_menu": patch}} if patch else {}


def _arbitration_log(
    state: Dict[str, Any], *, intent: str, relation: str, reason: str, resolution: str, route: str = "recurring_flow",
    target_type: str = "RECURRING_NEED",
) -> None:
    from ladini.graphs.agents.market_coach.interpreter.context_arbitration import (
        log_intent_arbitration,
    )

    log_intent_arbitration(
        state, semantic_intent=intent, relation=relation, route=route, reason=reason,
        target_type=target_type, target_resolution=resolution,
    )


def _active_needs(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [i for i in items if i.get("status") in ("ACTIVE", "PAUSED")]


def _pick_need(active: List[Dict[str, Any]], *, product_hint: str, context_target: Any) -> tuple[str, Any]:
    """Cible métier d'une modification/relance. `(kind, valeur)` :
    ``("context"|"name"|"single", item)`` cible exacte ; ``("not_found", libellés)`` produit nommé inconnu ;
    ``("not_unique", items)`` plusieurs candidats — jamais une devinette. Les `items` viennent de la liste de l'acheteur
    courant : un identifiant (du contexte, d'un menu périmé, forgé) qui n'y figure pas n'est JAMAIS une cible."""
    hint = str(product_hint or "").strip().lower()
    if hint:
        matches = [i for i in active if _same_product(hint, str(i.get("product") or ""))]
        if len(matches) == 1:
            return "name", matches[0]
        if len(matches) > 1:
            return "not_unique", matches
        return "not_found", [str(i.get("product") or "") for i in active]
    ctx_id = str((context_target or {}).get("id") or "") if isinstance(context_target, dict) else ""
    if ctx_id:
        for item in active:
            if str(item.get("recurring_need_id")) == ctx_id:
                return "context", item
    if len(active) == 1:
        return "single", active[0]
    return "not_unique", active


async def _need_choice_menu(
    state: Dict[str, Any], mc_runtime: MarketRuntime, items: List[Dict[str, Any]], question: str
) -> Dict[str, Any]:
    """NEED_SELECTION : le menu des SEULS besoins candidats (le choix ouvre l'écran du besoin choisi)."""
    patch = await _render_needs_list(
        state, mc_runtime, notice=f"{question}\n\n", only_ids={str(i["recurring_need_id"]) for i in items}
    )
    # Le choix est une réponse de menu de la LISTE (goal GET_MY_NEEDS) : la réponse « 2 » ouvre l'écran du besoin choisi,
    # elle ne rejoue ni la modification ni la relance qui ont produit ce menu.
    patch["current_goal"] = "GET_MY_NEEDS"
    patch["response_strategy"] = "CLARIFICATION"  # le texte du flow fait foi (pas le rendu générique d'interruption)
    return patch


def _unknown_product_reply(hint: str, labels: List[str]) -> str:
    owned = ", ".join(labels)
    return (f"Je ne trouve pas de besoin récurrent « {hint} » (vous avez : {owned}). "
            f"Pour en créer un nouveau, dites par exemple « j'ai besoin de 2 {hint} chaque semaine ».")


async def _update_from_message(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    intent = "UPDATE_RECURRING_NEED"
    phone = state.get("user_phone")
    request = _update_request(state)
    gw = RecurringSupplyGateway(mc_runtime)
    try:
        listing = await gw.list_my_recurring_needs(phone=str(phone))
    except MCPCallError:
        return {"final_response": "Je n'ai pas pu récupérer vos besoins.", "status": "COMPLETED"}
    active = _active_needs(listing.get("items") or [])
    if not active:
        return {"final_response": "Vous n'avez aucun besoin actif pour l'instant.", "status": "COMPLETED"}

    product_hint = str(request.get("product") or "")
    kind, picked = _pick_need(active, product_hint=product_hint, context_target=state.get("context_target"))
    if kind == "not_found":
        # Un produit NOMMÉ qui n'est aucun des besoins n'est pas une modification de l'un d'eux (« 2 chèvres » ne change
        # jamais le besoin Bœuf) : clarification, jamais une mutation par défaut.
        _arbitration_log(state, intent=intent, relation="AMBIGUOUS", reason="named_product_not_owned", resolution="not_found")
        return {"final_response": _unknown_product_reply(product_hint, picked), "status": "COMPLETED",
                "relation_to_context": "AMBIGUOUS"}
    if kind == "not_unique":
        _arbitration_log(state, intent=intent, relation="AMBIGUOUS", reason="target_not_unique", resolution="not_unique")
        label = str(picked[0].get("product") or "") if product_hint else ""
        question = (f"J'ai trouvé {len(picked)} besoins récurrents de {label}. Lequel voulez-vous modifier ?" if label
                    else "Vous avez plusieurs besoins actifs — lequel voulez-vous modifier ?")
        patch = await _need_choice_menu(state, mc_runtime, picked, question)
        patch["relation_to_context"] = "AMBIGUOUS"
        return patch
    need_id, product_label = str(picked["recurring_need_id"]), str(picked.get("product") or "")
    presented = _presented_need_version(state, need_id)
    # B26 : la version est celle de l'écran PRÉSENTÉ ; la liste lue ce tour ne sert qu'à résoudre la cible (et de repli à froid).
    need = {**picked, "need_version": presented if presented is not None else picked.get("need_version")}
    resolution = {"context": "context", "name": "by_name", "single": "single"}[kind]

    action = request.get("update_action")
    if action in _COMMAND_ACTIONS:
        return await _propose_command(state, mc_runtime, gw, need, str(action), request, resolution)

    steps, problem = _plan_permanent_changes(request)
    if problem is not None:
        _arbitration_log(state, intent=intent, relation="AMBIGUOUS", reason="invalid_or_missing_change", resolution=resolution)
        return {"final_response": problem.format(product=product_label), "response_strategy": "CLARIFICATION",
                "status": "WAITING_INPUT", "relation_to_context": "AMBIGUOUS"}

    _arbitration_log(state, intent=intent, relation="CORRECTION", reason="contextual_correction", resolution=resolution)
    result: Dict[str, Any] = {}
    expected_version = need.get("need_version")  # B26 : version de l'écran présenté (repli : lue ce tour, `same_turn_read`)
    for service_action, kwargs in steps:
        try:
            result = await gw.update_recurring_need(
                phone=str(phone), recurring_need_id=need_id, action=service_action, expected_version=expected_version, **kwargs)
        except MCPCallError as exc:
            logger.warning("recurring_need.update_failed | need=%s | action=%s | %s", need_id, service_action, exc)
            return {"final_response": "Je n'ai pas pu appliquer ce changement.", "status": "COMPLETED"}
        if isinstance(result, dict) and result.get("status") == "conflict":
            return await _conflict_response(state, mc_runtime, need_id, product_label, result, service_action,
                                            caller_path="contextual_correction", source="presented" if presented is not None else "same_turn_read")
        if not isinstance(result, dict) or result.get("status") != "success":  # refus du service : jamais « C'est noté »
            logger.warning("recurring_need.update_refused | need=%s | action=%s", need_id, service_action)
            return {"final_response": "Je n'ai pas pu appliquer ce changement : rien n'a été modifié.", "status": "COMPLETED"}
        expected_version = result.get("need_version")  # chaîne de NOTRE propre mutation
    return {"final_response": _render_update_summary(need, steps), "status": "COMPLETED", "result": result,
            "relation_to_context": "CORRECTION", **_version_snapshot_patch(state, need_id, expected_version)}


def _command_menu(
    need: Dict[str, Any], question: str, entries: List[tuple[str, str, Optional[Dict[str, Any]]]]
) -> Dict[str, Any]:
    """Menu FERMÉ d'une commande en attente de confirmation. `entries` : `(libellé, valeur, commande)` ; la valeur est
    `EXEC:<k>` (exécute `commands[k]`), `ASK:<k>` (demande confirmation de `commands[k]`), `VIEW:<besoin>` (retour à l'écran du
    besoin, rien n'est modifié) ou `LIST`. La commande est un SNAPSHOT (action, besoin, date, quantité) : ce qui s'exécute est
    exactement ce qui a été montré. Le menu porte la cible du besoin : une réponse libre reste comprise dans CE contexte."""
    product = str(need.get("product") or "")
    mapping: Dict[str, str] = {}
    commands: Dict[str, Any] = {}
    lines = [question, ""]
    for k, (label, value, command) in enumerate(entries, start=1):
        key = str(k)
        mapping[key] = value.replace("<k>", key)
        if command is not None:
            commands[key] = command
        lines.append(f"{k}. {label}")
    labels = {k: entries[int(k) - 1][0] for k in mapping}
    return {
        "final_response": "\n".join(lines),
        "response_strategy": "CLARIFICATION",
        "status": "WAITING_INPUT",
        "current_goal": "GET_MY_NEEDS",  # la réponse fermée (« 1 ») appartient à CE menu, pas à une nouvelle tâche
        "relation_to_context": "AMBIGUOUS",
        "working_memory": {"recurring_need_menu": {
            "__reset__": True,
            "mapping": mapping, "created_at": time.time(), "actions": {k: _menu_action_of(v) for k, v in mapping.items()},
            "title": f"Confirmation pour le besoin récurrent « {product} »", "labels": labels, "commands": commands,
            "shown_numbers": _shown_numbers(*[c.get("quantity") for c in commands.values()],
                                            *[_day_of(c.get("occurrence_date")) for c in commands.values()],
                                            need.get("quantity")),
            "target": {"type": "RECURRING_NEED", "id": str(need["recurring_need_id"]), "product": product,
                       "quantity": _fmt_qty(need.get("quantity")), "unit": str(need.get("unit") or ""),
                       "frequency": _frequency_label(need), "need_version": need.get("need_version")}}},
        **set_pending_interaction(InteractionKind.SELECTION_MENU, goal="GET_MY_NEEDS"),
    }


async def _next_open_occurrence(
    state: Dict[str, Any], gw: RecurringSupplyGateway, phone: Any, need_id: str
) -> Optional[Dict[str, Any]]:
    """Prochaine livraison MODIFIABLE (`OPEN`/`MATCHED`) du besoin : `{date, version}` ; `None` s'il n'y en a pas. B26 : la
    livraison montrée par l'écran de détail (date ET version) prime ; sans écran, elle est lue du service ce tour."""
    shown = _presented_occurrence(state, need_id)
    if shown is not None:
        return shown
    try:
        await gw.ensure_next_recurring_occurrence(phone=str(phone), recurring_need_id=need_id)
        detail = await gw.get_recurring_need_detail(phone=str(phone), recurring_need_id=need_id)
    except MCPCallError:
        return None
    if isinstance(detail, dict) and detail.get("status") == "success" and detail.get("occurrence_status") in ("OPEN", "MATCHED"):
        date = str(detail.get("occurrence_date") or "")[:10]
        if date and detail.get("occurrence_version") is not None:
            return {"date": date, "version": int(detail["occurrence_version"])}
    return None


async def _propose_command(
    state: Dict[str, Any], mc_runtime: MarketRuntime, gw: RecurringSupplyGateway, need: Dict[str, Any], action: str,
    request: Dict[str, Any], resolution: str,
) -> Dict[str, Any]:
    """B24 — annuler / suspendre / reprendre / ignorer UNE livraison / changer UNE livraison : comprises, jamais exécutées sur
    le message. Besoin (durable) et occurrence (une livraison) ne sont jamais confondus. Le flow résout la cible EXACTE (besoin
    déjà résolu ; date lue du service), publie un menu fermé portant l'ordre exact, et le service n'est appelé que sur la
    réponse fermée. « annuler » est ambigu (ignorer la prochaine ? tout arrêter ?) : le menu propose les deux."""
    need_id, product = str(need["recurring_need_id"]), str(need.get("product") or "")
    phone = state.get("user_phone")
    # B26 : l'ordre est un SNAPSHOT bâti sur l'état présenté. Besoin (`expected_version`) et occurrence
    # (`expected_occurrence_version`) ne sont jamais confondus : un ordre de livraison ne demande pas la version du besoin.
    base: Dict[str, Any] = {"recurring_need_id": need_id, "product": product}
    need_base: Dict[str, Any] = {**base, "expected_version": need.get("need_version")}

    def log(reason: str) -> None:
        _arbitration_log(state, intent="UPDATE_RECURRING_NEED", relation="AMBIGUOUS", reason=reason, resolution=resolution)

    stop = {**need_base, "action": "CANCEL", "summary": f"le besoin récurrent de {product} est arrêté"}
    if action == "CANCEL":
        log("destructive_ambiguity")
        nxt = await _next_open_occurrence(state, gw, phone, need_id)
        entries: List[tuple[str, str, Optional[Dict[str, Any]]]] = []
        if nxt:
            date = nxt["date"]
            skip = {**base, "action": "OCCURRENCE_SKIP", "occurrence_date": date, "expected_occurrence_version": nxt["version"],
                    "summary": f"la livraison de {product} du {_fmt_date_fr(date)} est ignorée (le besoin continue)"}
            entries.append((f"Ignorer seulement la prochaine livraison ({_fmt_date_fr(date)}) — le besoin continue", "EXEC:<k>", skip))
        entries.append(("Arrêter complètement le besoin récurrent", "ASK:<k>", stop))
        entries.append(("Ne rien changer", f"VIEW:{need_id}", None))
        return _command_menu(need, f"Pour votre besoin de {product}, que voulez-vous faire ?", entries)

    keep: tuple[str, str, Optional[Dict[str, Any]]] = ("Non, ne rien changer", f"VIEW:{need_id}", None)
    if action == "PAUSE":
        log("confirmation_required")
        cmd = {**need_base, "action": "PAUSE", "summary": f"le besoin de {product} est suspendu (aucune livraison jusqu'à reprise)"}
        return _command_menu(need, f"Suspendre votre besoin de {product} ? Aucune livraison ne sera planifiée jusqu'à la reprise.",
                             [("Oui, suspendre", "EXEC:<k>", cmd), keep])
    if action == "RESUME":
        if str(need.get("status") or "") != "PAUSED":
            return {"final_response": f"Votre besoin de {product} n'est pas suspendu : rien à reprendre.",
                    "response_strategy": "CLARIFICATION", "status": "COMPLETED", "relation_to_context": "CORRECTION"}
        log("confirmation_required")
        cmd = {**need_base, "action": "RESUME", "summary": f"le besoin de {product} est repris"}
        return _command_menu(need, f"Reprendre votre besoin de {product} ?", [("Oui, reprendre", "EXEC:<k>", cmd), keep])

    # SKIP_OCCURRENCE / OVERRIDE_OCCURRENCE : une livraison précise (la prochaine modifiable), jamais le besoin.
    nxt = await _next_open_occurrence(state, gw, phone, need_id)
    if not nxt:
        return {"final_response": f"Je ne trouve pas de prochaine livraison modifiable pour votre besoin de {product} — "
                                  "rien n'a été modifié.",
                "response_strategy": "CLARIFICATION", "status": "COMPLETED", "relation_to_context": "AMBIGUOUS"}
    date, occurrence_version = nxt["date"], nxt["version"]
    when = _fmt_date_fr(date)
    if action == "SKIP_OCCURRENCE":
        log("confirmation_required")
        cmd = {**base, "action": "OCCURRENCE_SKIP", "occurrence_date": date, "expected_occurrence_version": occurrence_version,
               "summary": f"la livraison de {product} du {when} est ignorée (le besoin continue)"}
        return _command_menu(need, f"Ignorer seulement la livraison de {product} du {when} ? Le besoin continue ensuite.",
                             [("Oui, ignorer cette livraison", "EXEC:<k>", cmd), keep])
    quantity = request.get("quantity")  # OVERRIDE_OCCURRENCE
    try:
        value = float(str(quantity)) if slot_has_value(quantity) else 0.0
    except (TypeError, ValueError):
        value = 0.0
    if not 0 < value < float("inf"):
        log("invalid_or_missing_change")
        return {"final_response": f"Quelle quantité souhaitez-vous pour la seule livraison de {product} du {when} ? "
                                  "(un nombre supérieur à 0)",
                "response_strategy": "CLARIFICATION", "status": "WAITING_INPUT", "relation_to_context": "AMBIGUOUS",
                "current_goal": "GET_MY_NEEDS", **set_pending_interaction(InteractionKind.SELECTION_MENU, goal="GET_MY_NEEDS")}
    log("confirmation_required")
    cmd = {**base, "action": "OCCURRENCE_OVERRIDE", "occurrence_date": date, "quantity": value,
           "expected_occurrence_version": occurrence_version,
           "summary": f"la livraison de {product} du {when} passe à {_fmt_qty(value)} (le besoin ne change pas)"}
    return _command_menu(need, f"Changer seulement la livraison de {product} du {when} à {_fmt_qty(value)} ? "
                               "Le besoin récurrent reste inchangé.", [("Oui, changer cette livraison", "EXEC:<k>", cmd), keep])


def _version_conflict_reply(product: str, conflict: Dict[str, Any]) -> str:
    """Conflit de version (B25) : le besoin a changé depuis ce que l'acheteur a vu. Message métier — jamais d'erreur technique,
    jamais d'application silencieuse : l'état courant est montré et la demande est à REFORMULER sur cette version."""
    current = conflict.get("current") or {}
    if conflict.get("scope") == "OCCURRENCE":
        return (f"La livraison de {product} a été modifiée entre-temps. Rien n'a été changé : "
                "redites votre demande pour l'appliquer sur la livraison à jour.")
    state = ""
    if current:
        state = f" Il est maintenant à {_fmt_qty(current.get('quantity'))} {current.get('unit') or ''} {_frequency_label(current)}".rstrip() + "."
    return (f"Votre besoin de {product} a été modifié entre-temps.{state} Rien n'a été changé : "
            "redites votre demande pour l'appliquer à l'état actuel.")


async def _conflict_response(
    state: Dict[str, Any], mc_runtime: MarketRuntime, need_id: str, product: str, conflict: Dict[str, Any], action: str,
    *, caller_path: str, source: str,
) -> Dict[str, Any]:
    """Conflit de version : RIEN n'a été écrit. Le message dit ce qui a changé, puis l'écran du besoin est RÉAFFICHÉ à l'état
    courant (ce qui remplace la version présentée) : l'acheteur décide de nouveau. Jamais de rejeu automatique de la demande sur la
    nouvelle version — une intention périmée peut ne plus avoir de sens."""
    logger.info("RECURRING_VERSION_CONFLICT | caller_path=%s | action=%s | scope=%s | version_source=%s",
                caller_path, action, conflict.get("scope") or "NEED", source)
    notice = _version_conflict_reply(product, conflict)
    screen = await _show_need_detail(state, mc_runtime, need_id, notice=f"{notice}\n\n")
    if screen.get("status") == "WAITING_INPUT":
        # Le texte du flow fait foi (pas le rendu générique d'interruption), le goal reste celui de l'écran affiché.
        return {**screen, "relation_to_context": "CORRECTION", "response_strategy": "CLARIFICATION", "current_goal": "GET_MY_NEEDS"}
    return {"final_response": notice, "status": "COMPLETED", "relation_to_context": "CORRECTION"}


async def _execute_command(state: Dict[str, Any], mc_runtime: MarketRuntime, command: Dict[str, Any]) -> Dict[str, Any]:
    """Exécute le SNAPSHOT confirmé par une réponse fermée. Le service revalide la propriété (buyer_id) et l'état de
    l'occurrence : un besoin/une date devenus invalides sont refusés, jamais forcés."""
    gw = RecurringSupplyGateway(mc_runtime)
    kwargs: Dict[str, Any] = {k: command[k] for k in ("occurrence_date", "quantity", "expected_version", "expected_occurrence_version")
                              if command.get(k) is not None}
    try:
        outcome = await gw.update_recurring_need(
            phone=str(state.get("user_phone")), recurring_need_id=str(command["recurring_need_id"]),
            action=str(command["action"]), **kwargs)
        if isinstance(outcome, dict) and outcome.get("status") == "conflict":
            return await _conflict_response(state, mc_runtime, str(command["recurring_need_id"]), str(command.get("product") or ""),
                                            outcome, str(command["action"]), caller_path="confirmed_command", source="snapshot")
        if not isinstance(outcome, dict) or outcome.get("status") != "success":
            raise MCPCallError(tool="update_recurring_need", message="refus du service", error_code="BUSINESS_RULE", request_id="n/a")
    except MCPCallError as exc:
        logger.warning("recurring_need.command_failed | action=%s | %s", command.get("action"), exc)
        return {"final_response": "Je n'ai pas pu appliquer ce changement : la livraison ou le besoin a changé entre-temps. "
                                  "Rien n'a été modifié.", "status": "COMPLETED"}
    _arbitration_log(state, intent="UPDATE_RECURRING_NEED", relation="ANSWER", reason="closed_menu_answer",
                     resolution="snapshot")
    return {"final_response": f"C'est noté : {command.get('summary') or 'changement appliqué'}.", "status": "COMPLETED",
            "relation_to_context": "ANSWER",
            **_version_snapshot_patch(state, str(command["recurring_need_id"]), outcome.get("need_version"))}


def _plan_permanent_changes(request: Dict[str, Any]):
    """Changements PERMANENTS du besoin (quantité et/ou fréquence) validés hors du LLM. `(étapes, None)` ou
    `(None, message_de_clarification)`. Quantité > 0 ; fréquence connue ; WEEKLY_DAYS exige les jours."""
    steps: List[tuple[str, Dict[str, Any]]] = []
    quantity = request.get("quantity")
    if slot_has_value(quantity):
        try:
            value = float(quantity)
        except (TypeError, ValueError):
            value = 0.0
        if not 0 < value < float("inf"):
            return None, "Quelle nouvelle quantité souhaitez-vous pour votre besoin de {product} ? (un nombre supérieur à 0)"
        steps.append(("PERMANENT_QUANTITY", {"quantity": value}))
    recurrence = str(request.get("recurrence_type") or "").upper()
    if recurrence:
        if recurrence not in _PERMANENT_RECURRENCES:
            return None, "Quelle fréquence souhaitez-vous pour votre besoin de {product} : chaque jour, chaque semaine ou chaque mois ?"
        days = request.get("weekly_days") or None
        if recurrence == "WEEKLY_DAYS" and not days:
            return None, "Quels jours de la semaine souhaitez-vous pour votre besoin de {product} ?"
        steps.append(("PERMANENT_FREQUENCY", {
            "recurrence_type": recurrence, "weekly_days": days, "excluded_weekdays": request.get("excluded_weekdays") or None}))
    if not steps:
        return None, ("Que voulez-vous modifier pour votre besoin de {product} : la quantité, la fréquence "
                      "ou la prochaine livraison ?")
    return steps, None


def _render_update_summary(need: Dict[str, Any], steps: List[tuple[str, Dict[str, Any]]]) -> str:
    """Confirmation avec ancienne -> nouvelle valeur : l'utilisateur voit exactement ce qui a changé."""
    product = str(need.get("product") or "")
    unit = str(need.get("unit") or "")
    parts: List[str] = []
    for action, kwargs in steps:
        if action == "PERMANENT_QUANTITY":
            parts.append(f"quantité {_fmt_qty(need.get('quantity'))} → {_fmt_qty(kwargs['quantity'])} {unit}".rstrip())
        elif action == "PERMANENT_FREQUENCY":
            new = {"recurrence_type": kwargs["recurrence_type"], "weekly_days": kwargs.get("weekly_days")}
            parts.append(f"fréquence : {_frequency_label(need)} → {_frequency_label(new)}")
    return f"C'est noté, votre besoin de {product} est mis à jour ({' ; '.join(parts)})."


# =====================================================================
# GET_MY_NEEDS — liste (avec disponibilité) + détail d'un besoin (mandat Phase 4 §5/§6/§7)
#
# Pas de nouvel intent GET_MATCH_PROPOSALS : GET_MY_NEEDS sert les deux vues ("mes besoins" ET
# "disponibilité/détail"), reliées par UN SEUL menu numéroté réutilisant `PendingInteraction`
# (`InteractionKind.SELECTION_MENU`) — jamais `InteractionKind.SELECTION`, qui n'existe pas dans ce
# contrat (voir `core/pending_interaction.py`). Le mapping numéro→cible est stocké dans
# `working_memory["recurring_need_menu"]` (même idiome que `order_tracking.py::available_mapping`,
# en plus léger : pas besoin de son `order_tracking_context` dédié pour un menu à 2 niveaux).
# =====================================================================


async def _get_my_needs_flow(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    # B20 : réponse « voir les détails » au menu d'un DIGEST (message proactif, donc sans menu en état) — attribuée par
    # `interpreter/context_arbitration.py` à CE digest : un seul besoin notifié -> son détail ; plusieurs -> la liste avec
    # disponibilité (qui propose ensuite le détail de chacun). « mes besoins » (digest_action=MY_NEEDS) = la liste.
    _payload = state.get("transaction_payload") or {}
    if _payload.get("digest_action") == "VIEW_DETAILS":
        _ids = list(dict.fromkeys(str(i) for i in (_payload.get("recurring_need_ids") or []) if i))
        if len(_ids) == 1:
            return await _show_need_detail(state, mc_runtime, _ids[0])
        return await _render_needs_list(state, mc_runtime)
    if _payload.get("digest_action") == "MY_NEEDS":
        return await _render_needs_list(state, mc_runtime)
    # B27 : « et pour les oignons ? » — une demande NOMMANT un besoin (jamais une réponse de menu) ouvre CE besoin ; la cible est
    # résolue par le domaine (0 -> introuvable, 1 -> exact, N -> choix explicite), jamais choisie par le modèle.
    _named = str(entities_said_this_turn(state).get("product") or "").strip()
    if _named and not _payload.get("selection_index") and str(state.get("interpreted_event") or "").upper() != "SELECTION":
        return await _open_need_by_name(state, mc_runtime, _named)
    resolved = _resolve_menu_reply(state)
    if resolved is not None:
        kind, target = resolved
        if kind == "DETAIL":
            logger.info("RECURRING_SELF_SERVICE_OPENED")
            return await _show_need_detail(state, mc_runtime, target, ensure=True)
        if kind == "REFRESH":
            need_id, occ_id, occ_version = _split_match_target(target)  # B28 : l'écran affiché désigne UNE livraison exacte
            return await _refresh_matching(state, mc_runtime, need_id, occurrence_id=occ_id, occurrence_version=occ_version)
        if kind == "ORDERS":
            from ladini.graphs.agents.market_coach.flows.buyer.order_tracking import (
                list_orders,
            )

            orders_screen: Dict[str, Any] = await list_orders(state, mc_runtime)
            return orders_screen
        if kind == "VIEW":
            return await _show_need_detail(state, mc_runtime, target, ensure=True)
        if kind in ("EXEC", "ASK"):
            if _payload.get("closed_reply_required"):
                logger.info("RECURRING_MUTATION_NEEDS_CLOSED_REPLY action=%s", kind)
                return {"final_response": "Pour confirmer, tapez le numéro affiché (ou « retour » pour ne rien changer).",
                        "status": "WAITING_INPUT", "current_goal": "GET_MY_NEEDS",  # le menu reste actif pour la réponse fermée
                        **set_pending_interaction(InteractionKind.SELECTION_MENU, goal="GET_MY_NEEDS")}
            menu = (state.get("working_memory") or {}).get("recurring_need_menu") or {}
            command = (menu.get("commands") or {}).get(str(target))
            if not isinstance(command, dict) or not command.get("recurring_need_id"):
                return await _render_needs_list(state, mc_runtime, notice="Cette confirmation n'est plus valable.\n\n")
            if kind == "ASK":  # action destructive : seconde confirmation explicite, sur le snapshot exact
                need = {"recurring_need_id": command["recurring_need_id"], "product": (menu.get("target") or {}).get("product"),
                        "need_version": command.get("expected_version")}  # B26 : la 2e confirmation garde la version de la 1re
                return _command_menu(
                    need, f"Confirmer : arrêter complètement votre besoin de {need['product']} ? Cette action est définitive.",
                    [("Oui, arrêter définitivement", "EXEC:<k>", command), ("Non, ne rien changer", f"VIEW:{command['recurring_need_id']}", None)])
            return await _execute_command(state, mc_runtime, command)
        if kind in ("CONFIRM", "REJECT"):
            if _payload.get("closed_reply_required"):
                # B23 : une réponse en langage LIBRE (jamais un numéro/alias fermé du menu) ne valide ni ne refuse jamais une
                # proposition — la mauvaise action vaut moins qu'une clarification. Le menu reste affiché.
                logger.info("RECURRING_MUTATION_NEEDS_CLOSED_REPLY action=%s", kind)
                return {"final_response": "Pour accepter ou refuser cette proposition, répondez « accepter » ou « refuser », "
                                          "ou tapez le numéro affiché.", "status": "WAITING_INPUT",
                        "current_goal": "GET_MY_NEEDS",  # le menu reste actif : « 1 » ensuite est une réponse fermée
                        **set_pending_interaction(InteractionKind.SELECTION_MENU, goal="GET_MY_NEEDS")}
            need_id, occurrence_id, version = _split_match_target(target)
            return await _respond_to_match(
                state, mc_runtime, recurring_need_id=need_id, action=kind,
                occurrence_id=occurrence_id, expected_version=version,
            )
        if kind == "INVALID":
            from ladini.graphs.agents.market_coach.interpreter.context_arbitration import (
                live_menu_target,
                targeted_menu_clarification,
            )

            if live_menu_target(state, require_pending=False) is not None:  # B24 : écran d'un besoin — clarification ciblée
                return {"final_response": targeted_menu_clarification(state, require_pending=False), "status": "WAITING_INPUT",
                        **set_pending_interaction(InteractionKind.SELECTION_MENU, goal="GET_MY_NEEDS")}  # l'écran reste actif
            return await _render_needs_list(state, mc_runtime, notice="Je n'ai pas compris ce choix.\n\n")
        # kind == "LIST" (retour / mes besoins) — retombe sur la liste fraîche ci-dessous.
    return await _render_needs_list(state, mc_runtime)


async def _open_need_by_name(state: Dict[str, Any], mc_runtime: MarketRuntime, hint: str) -> Dict[str, Any]:
    """B27 — ouvre l'écran du besoin NOMMÉ ; 0 correspondance : introuvable (+ la liste), N : menu des seuls candidats."""
    intent = "GET_MY_NEEDS"
    gw = RecurringSupplyGateway(mc_runtime)
    try:
        listing = await gw.list_my_recurring_needs(phone=str(state.get("user_phone")))
    except MCPCallError:
        return {"final_response": "Je n'ai pas pu récupérer vos besoins.", "status": "COMPLETED"}
    owned = [i for i in (listing.get("items") or []) if i.get("status") in ("ACTIVE", "PAUSED")]
    kind, picked = _pick_need(owned, product_hint=hint, context_target=None)
    if kind == "not_found":
        _arbitration_log(state, intent=intent, relation="AMBIGUOUS", reason="named_need_not_found", resolution="not_found")
        patch = await _render_needs_list(
            state, mc_runtime, notice=f"Je ne trouve pas de besoin récurrent « {hint} » parmi vos besoins.\n\n")
        return {**patch, "response_strategy": "CLARIFICATION", "relation_to_context": "AMBIGUOUS"}
    if kind == "not_unique":
        _arbitration_log(state, intent=intent, relation="AMBIGUOUS", reason="target_not_unique", resolution="not_unique")
        patch = await _need_choice_menu(
            state, mc_runtime, picked, f"Vous avez {len(picked)} besoins pour « {hint} ». Lequel voulez-vous voir ?")
        return {**patch, "relation_to_context": "AMBIGUOUS"}
    _arbitration_log(state, intent=intent, relation="ANSWER", reason="named_need", resolution="by_name")
    screen = await _show_need_detail(state, mc_runtime, str(picked["recurring_need_id"]), ensure=True)
    return {**screen, "response_strategy": "CLARIFICATION", "relation_to_context": "ANSWER"}


def _resolve_menu_reply(state: Dict[str, Any]):
    """`None` : aucun menu actif pour CE goal (première visite, ou menu d'un autre tunnel — jamais
    touché). Sinon `("LIST"|"DETAIL"|"CONFIRM"|"REJECT"|"INVALID", target)`. `target` est un
    `recurring_need_id` pour `DETAIL`/`CONFIRM`/`REJECT`, `None` pour `LIST`/`INVALID`."""
    pending = state.get("pending_interaction") or {}
    if not isinstance(pending, dict):
        return None
    menu = (state.get("working_memory") or {}).get("recurring_need_menu")
    if not isinstance(menu, dict):
        return None
    if pending:
        # Un pending vivant d'un AUTRE tunnel n'est jamais touché.
        if pending.get("kind") != "SELECTION_MENU" or pending.get("goal") != "GET_MY_NEEDS":
            return None
        created_at = pending.get("created_at")
    else:
        # B13 : en vraie conversation le pending est CONSOMMÉ (résolu en `selection_index`) avant que le flow
        # ne tourne — exiger un pending vivant rendait « liste -> 1 -> détail » inatteignable. Le menu gardé
        # dans `working_memory` (avec SA propre date) fait foi ; ce flow ne tourne que pour le goal GET_MY_NEEDS.
        created_at = menu.get("created_at")
    if time.time() - float(created_at or 0) > _MENU_TTL_SECONDS:
        return None  # périmé — traité comme une première visite, silencieusement (même convention que confirmation_gate)

    payload = state.get("transaction_payload") or {}
    index = str(payload.get("selection_index") or "").strip()
    text_lower = str(state.get("normalized_text") or state.get("user_query") or "").strip().lower()

    mapping: Dict[str, str] = menu.get("mapping") or {}
    chosen = mapping.get(index)
    if chosen is None:
        chosen = _chosen_by_text_alias(mapping, text_lower)
    if chosen is None and _fold_text(text_lower) in _BACK_WORDS:
        chosen = "LIST"
    if chosen is None:
        return ("INVALID", None)
    if chosen == "LIST":
        return ("LIST", None)
    # "CONFIRM:<recurring_need_id>" / "REJECT:<recurring_need_id>" (VS4) — préfixe fermé, jamais
    # deviné : une valeur de mapping mal formée retombe sur DETAIL, jamais sur une action muette.
    for prefix, kind in (("CONFIRM:", "CONFIRM"), ("REJECT:", "REJECT"), ("REFRESH:", "REFRESH"), ("VIEW:", "VIEW"),
                         ("EXEC:", "EXEC"), ("ASK:", "ASK")):
        if chosen.startswith(prefix):
            return (kind, chosen[len(prefix):])
    if chosen == "ORDERS":
        return ("ORDERS", None)
    return ("DETAIL", chosen)


def _fold_text(text: str) -> str:
    from ladini.graphs.agents.market_coach.interpreter.context_arbitration import fold

    return str(fold(text))


def _chosen_by_text_alias(mapping: Dict[str, str], text_lower: str) -> Optional[str]:
    """Valeur du menu visée par un alias textuel (« accepter les 250 kg », « rechercher maintenant »...), ou `None`.
    Vocabulaire unique : `interpreter/context_arbitration.RECURRING_MENU_TEXT_ALIASES`."""
    from ladini.graphs.agents.market_coach.interpreter.context_arbitration import (
        RECURRING_MENU_TEXT_ALIASES,
        fold,
    )

    norm = fold(text_lower)
    if not norm:
        return None
    for action, aliases in RECURRING_MENU_TEXT_ALIASES.items():
        starts = {"CONFIRM": "accepter ", "REJECT": "refuser "}.get(action)
        if norm in aliases or (starts and norm.startswith(starts)):
            for value in mapping.values():
                if value == action or value.startswith(action + ":"):
                    return value
    return None


async def _render_needs_list(
    state: Dict[str, Any], mc_runtime: MarketRuntime, *, notice: str = "", only_ids: Optional[set] = None
) -> Dict[str, Any]:
    phone = state.get("user_phone")
    gw = RecurringSupplyGateway(mc_runtime)
    try:
        result = await gw.list_my_recurring_needs(phone=str(phone))
    except MCPCallError:
        return {"final_response": "Je n'ai pas pu récupérer vos besoins.", "status": "COMPLETED"}

    items: List[Dict[str, Any]] = result.get("items") or []
    if only_ids is not None:  # B24 : choix parmi les SEULS besoins candidats (cible non unique)
        items = [i for i in items if str(i.get("recurring_need_id")) in only_ids]
    if not items:
        return {"final_response": f"{notice}Vous n'avez pas encore de besoin récurrent enregistré.", "status": "COMPLETED"}

    lines = [f"{notice}🔁 Mes besoins récurrents", ""]
    mapping: Dict[str, str] = {}
    twins = _indistinguishable_needs(items)
    for i, item in enumerate(items, start=1):
        lines += _render_need_block(i, item, show_start=str(item.get("recurring_need_id")) in twins)
        mapping[str(i)] = str(item["recurring_need_id"])
    lines += ["Répondez avec le numéro du besoin."]

    return {
        "final_response": "\n".join(lines),
        # B13 : WAITING_INPUT (et non COMPLETED) — garde `current_goal=GET_MY_NEEDS` vivant
        # (`nodes/cleanup.py::keep_selection_channel`). Avec COMPLETED le goal était effacé et le « 1 » suivant
        # retombait sur le menu générique : liste -> détail -> confirmer était INATTEIGNABLE en vraie conversation.
        "status": "WAITING_INPUT",
        "working_memory": {"recurring_need_menu": {
            "__reset__": True,  # B24 : `working_memory` fusionne en profondeur — un index d'un ancien écran ne doit JAMAIS survivre
            "mapping": mapping, "created_at": time.time(), "actions": {k: "SELECT" for k in mapping},
            "title": "Liste de vos besoins récurrents (répondre avec le numéro du besoin)",
            # B26 : version de chaque besoin AU MOMENT où la liste est montrée (navigation : le détail rafraîchit ; mutation : comparée).
            "versions": {str(i["recurring_need_id"]): i.get("need_version") for i in items if i.get("need_version") is not None},
            "target": None,  # `working_memory` fusionne en profondeur : une cible d'écran précédente ne doit pas survivre à la liste
            "labels": {str(i): _render_need_line(item, show_start=str(item.get("recurring_need_id")) in twins)
                       for i, item in enumerate(items, start=1)},
            # B27 : dates AFFICHÉES de chaque besoin — le domaine (jamais le modèle) résout « celui du 5 », « celle de demain ».
            "facts": {str(i): {"starts": [str(item["starts_on"])[:10]] if item.get("starts_on") else [],
                               "deliveries": [str(item["next_occurrence_date"])[:10]] if item.get("next_occurrence_date") else []}
                      for i, item in enumerate(items, start=1)},
            "occurrences": {str(item["recurring_need_id"]): {"id": item.get("next_occurrence_id"),
                                                             "date": str(item.get("next_occurrence_date") or "")[:10] or None}
                            for item in items}}},
        **set_pending_interaction(InteractionKind.SELECTION_MENU, goal="GET_MY_NEEDS"),
    }


_WEEKDAYS_FR = {1: "lundi", 2: "mardi", 3: "mercredi", 4: "jeudi", 5: "vendredi", 6: "samedi", 7: "dimanche"}
_MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre",
              "décembre"]


def _frequency_label(item: Dict[str, Any]) -> str:
    kind = item.get("recurrence_type")
    days = [d for d in (item.get("weekly_days") or []) if d in _WEEKDAYS_FR]
    if kind == "WEEKLY_DAYS" and days:
        return "chaque " + " et ".join(_WEEKDAYS_FR[d] for d in days)
    return {"DAILY": "chaque jour", "WEEKLY_DAYS": "chaque semaine", "WEEKLY": "chaque semaine",
            "MONTHLY": "chaque mois", "ONE_OFF": "une seule fois"}.get(str(kind), "chaque jour")


def _fmt_date_fr(iso: Any) -> Optional[str]:
    try:
        y, m, d = (int(x) for x in str(iso)[:10].split("-"))
        return f"{d} {_MONTHS_FR[m - 1]}"
    except (ValueError, IndexError):
        return None


def _need_status_label(status: Any) -> str:
    return {"ACTIVE": "🟢 Actif", "PAUSED": "⏸ Suspendu"}.get(str(status), "⛔ Annulé")


def _availability_label(status: Any, requested: Any, matched: Any, unit: Any) -> Optional[str]:
    """Libellé de disponibilité d'une occurrence PROPOSABLE (jamais d'invention : `None` si l'info manque)."""
    if status not in ("OPEN", "MATCHED") or requested is None or matched is None:
        return None
    if float(matched) <= 0:
        return "aucune disponibilité trouvée pour le moment"
    return f"{_fmt_qty(matched)} / {_fmt_qty(requested)} {unit}"


def _indistinguishable_needs(items: List[Dict[str, Any]]) -> set:
    """Ids des besoins que rien ne distingue à l'affichage (même produit, quantité, unité, fréquence) : deux engagements
    LÉGITIMES (ex. « 3 chèvres par semaine » démarré le 5, puis un autre le 10) — jamais fusionnés, mais datés."""
    groups: Dict[tuple, List[str]] = {}
    for item in items:
        key = (str(item.get("product") or "").lower(), item.get("quantity"), item.get("unit"), _frequency_label(item),
               item.get("status"))
        groups.setdefault(key, []).append(str(item.get("recurring_need_id")))
    return {need_id for ids in groups.values() if len(ids) > 1 for need_id in ids}


def _delivery_line(item: Dict[str, Any], *, prefix: str = "") -> str:
    """Ligne « prochaine livraison » d'un besoin ACTIF : date réelle, « aujourd'hui », ou un état de planning EXPLICITE —
    jamais un « à planifier » ambigu (B22)."""
    state = item.get("schedule_state")
    when = _fmt_date_fr(item.get("next_occurrence_date"))
    if state == "OK" and when:
        if item.get("next_occurrence_is_today") or item.get("occurrence_is_today"):
            return f"{prefix}Livraison prévue aujourd'hui"
        return f"{prefix}Prochaine livraison : {when}"
    if state == "ENDED":
        return f"{prefix}Terminé — aucune livraison à venir."
    return f"{prefix}⚠️ Planning incomplet — date de prochaine livraison à préciser."


def _render_need_block(index: int, item: Dict[str, Any], *, show_start: bool = False) -> List[str]:
    started = _fmt_date_fr(item.get("starts_on"))
    qty_line = f"   {_fmt_qty(item.get('quantity'))} {item.get('unit')} {_frequency_label(item)}"
    if show_start and started:
        qty_line += f" · démarré le {started}"
    lines = [f"{index}. {str(item.get('product') or '?').capitalize()}",
             qty_line,
             f"   {_need_status_label(item.get('status'))}"]
    if item.get("status") == "ACTIVE":
        lines.append(_delivery_line(item, prefix="   "))
        occ_status = item.get("next_occurrence_status") or ("OPEN" if item.get("next_occurrence_id") else None)
        if occ_status in ("ACCEPTED", "PARTIALLY_ACCEPTED"):
            lines.append("   ✅ Approvisionnement accepté" if occ_status == "ACCEPTED" else "   ✅ Approvisionnement partiellement accepté")
        else:
            avail = _availability_label(occ_status, item.get("requested_quantity"), item.get("matched_quantity"), item.get("unit"))
            if avail:
                lines.append(f"   Disponibilité actuelle : {avail}")
    lines.append("")
    return lines


def _render_need_line(item: Dict[str, Any], *, show_start: bool = False) -> str:
    freq = {
        "DAILY": "jour",
        "WEEKLY_DAYS": "semaine",
        "WEEKLY": "semaine",
        "MONTHLY": "mois",
        "ONE_OFF": "une fois",
    }.get(item.get("recurrence_type"), "jour")
    status = "actif" if item.get("status") == "ACTIVE" else "en pause" if item.get("status") == "PAUSED" else "annulé"
    label = f"{item.get('product', '?').capitalize()} — {item.get('quantity')} {item.get('unit')}/{freq} — {status}"
    requested, matched = item.get("requested_quantity"), item.get("matched_quantity")
    if requested is not None and matched is not None:
        emoji = "✅" if matched >= requested and requested > 0 else "❌" if matched <= 0 else "⚠️"
        label += f" — {emoji} {_fmt_qty(matched)}/{_fmt_qty(requested)} {item.get('unit')} disponibles demain"
    started = _fmt_date_fr(item.get("starts_on"))
    if show_start and started:  # B27 : deux besoins identiques se distinguent par leur date (« celui commencé le 5 »)
        label += f" — démarré le {started}"
    when = _fmt_date_fr(item.get("next_occurrence_date"))
    if item.get("status") == "ACTIVE" and item.get("schedule_state") == "OK" and when:
        label += f" — prochaine livraison {when}"
    return label


def _fmt_qty(value: Any) -> str:
    if value is None:
        return "?"
    return str(int(value)) if float(value).is_integer() else str(value)


def _join_match_target(recurring_need_id: Any, occurrence_id: Any, version: Any) -> str:
    if occurrence_id is None or version is None:
        return str(recurring_need_id)
    return f"{recurring_need_id}|{occurrence_id}|{int(version)}"


def _split_match_target(target: Any) -> tuple[str, Optional[str], Optional[int]]:
    """Inverse de `_join_match_target`. Ancien format (`<need_id>` seul, menu affiché avant B12) :
    pas d'identité exacte -> `(need_id, None, None)`."""
    parts = str(target or "").split("|")
    if len(parts) == 3 and parts[1] and parts[2].isdigit():
        return parts[0], parts[1], int(parts[2])
    return parts[0], None, None


async def _refresh_matching(
    state: Dict[str, Any], mc_runtime: MarketRuntime, recurring_need_id: str, *,
    occurrence_id: Optional[str] = None, occurrence_version: Optional[int] = None,
) -> Dict[str, Any]:
    """« Rechercher maintenant » : le VRAI moteur de matching (celui du cron), puis l'écran à jour (nouvelle `version` si les
    allocations ont changé). N'envoie aucun digest. B28 : quand la livraison visée est connue (écran affiché, notification d'échec),
    la relance porte son identité EXACTE (occurrence + version, B26) — le DOMAINE décide si elle est encore récupérable
    (`recover_occurrence_sourcing`) ; ce flux ne fait que rendre l'issue. Jamais « la prochaine » à la place de celle-là."""
    gw = RecurringSupplyGateway(mc_runtime)
    logger.info("RECURRING_MANUAL_MATCH_TRIGGERED")
    notice = ""
    exact = occurrence_id is not None and occurrence_version is not None
    try:
        if exact:
            res = await gw.refresh_recurring_need_matching(
                phone=str(state.get("user_phone")), recurring_need_id=recurring_need_id,
                occurrence_id=occurrence_id, expected_occurrence_version=int(occurrence_version),  # type: ignore[arg-type]
            )
        else:
            res = await gw.refresh_recurring_need_matching(phone=str(state.get("user_phone")), recurring_need_id=recurring_need_id)
    except MCPCallError:
        res = {}
        notice = "⚠️ La recherche n'a pas pu aboutir — réessayez dans un instant.\n\n"
    outcome = (res or {}).get("outcome")
    when = _fmt_date_fr((res or {}).get("occurrence_date")) if isinstance(res, dict) else ""
    day = f" du {when}" if when else ""
    if outcome == "NO_AVAILABILITY":
        notice = "🔎 Recherche terminée : aucune disponibilité trouvée pour le moment.\n\n"
    elif outcome == "MATCHED":
        notice = "🔎 Recherche terminée.\n\n"
    elif outcome == "RECOVERY_APPLIED":
        notice = (f"🔎 Recherche relancée pour la livraison{day} (un autre producteur que le précédent).\n\n" if res.get("proposal_available")
                  else f"🔎 Recherche relancée pour la livraison{day} : aucun autre producteur disponible pour le moment.\n\n")
    elif outcome == "ALREADY_RECOVERED":
        notice = f"🔎 La recherche a déjà été relancée pour la livraison{day} — voici où elle en est.\n\n"
    elif outcome == "VERSION_CONFLICT":
        notice = f"🔎 La livraison{day} a changé depuis votre dernier message — voici où elle en est.\n\n"
    elif outcome == "RECOVERY_WINDOW_CLOSED":
        notice = (f"⛔ La livraison{day} ne peut plus être relancée à temps. "
                  "Votre besoin récurrent reste actif pour les prochaines livraisons.\n\n")
    elif outcome == "OCCURRENCE_COMMITTED":
        notice = f"✅ La livraison{day} est déjà confirmée par un producteur : rien à relancer.\n\n"
    elif outcome == "NEED_NOT_ACTIVE":
        notice = "⏸️ Ce besoin récurrent n'est plus actif : aucune recherche n'a été relancée.\n\n"
    elif outcome in ("NOT_RECOVERABLE", "TARGET_NOT_FOUND"):
        notice = f"🔎 La livraison{day} ne peut pas être relancée.\n\n"
    elif outcome == "MATCH_ERROR":
        notice = "⚠️ La recherche n'a pas pu aboutir — réessayez dans un instant.\n\n"
    elif outcome in ("NOT_MATCHABLE", "NO_OCCURRENCE"):
        # B27 : c'est le DOMAINE qui dit si une relance est encore possible (jamais le langage naturel) ; le message le répète.
        notice = "🔎 Cette livraison ne peut plus être relancée pour le moment.\n\n"
    return await _show_need_detail(state, mc_runtime, recurring_need_id, notice=notice)


async def _refresh_by_message(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    """B24 — « cherche pour mes boeufs » : intention sémantique REFRESH_RECURRING_MATCHING -> cible métier exacte
    (écran affiché, ou produit nommé parmi les besoins de l'acheteur courant) -> le même moteur que « Rechercher
    maintenant ». 0 cible : introuvable ; N cibles : choix explicite (jamais une devinette, jamais un index périmé)."""
    intent = "REFRESH_RECURRING_MATCHING"
    gw = RecurringSupplyGateway(mc_runtime)
    try:
        listing = await gw.list_my_recurring_needs(phone=str(state.get("user_phone")))
    except MCPCallError:
        return {"final_response": "Je n'ai pas pu récupérer vos besoins.", "status": "COMPLETED"}
    active = _active_needs(listing.get("items") or [])
    if not active:
        return {"final_response": "Vous n'avez aucun besoin actif pour l'instant.", "status": "COMPLETED"}
    hint = str(entities_said_this_turn(state).get("product") or "")
    kind, picked = _pick_need(active, product_hint=hint, context_target=state.get("context_target"))
    if kind == "not_found":
        _arbitration_log(state, intent=intent, relation="AMBIGUOUS", reason="target_not_found", resolution="not_found")
        return {"final_response": f"Je ne trouve pas de besoin récurrent « {hint} » (vous avez : {', '.join(picked)}).",
                "status": "COMPLETED", "relation_to_context": "AMBIGUOUS"}
    picked = cast(Any, picked)  # `_pick_need` renvoie selon `kind` : liste, dict ou None
    ctx: Dict[str, Any] = state.get("context_target") if isinstance(state.get("context_target"), dict) else {}  # type: ignore[assignment]
    failed_ids = {str(n) for n in ctx.get("ids") or []} if ctx.get("source") == "recovery_notification" else set()
    if kind == "not_unique" and not hint and len(failed_ids) >= 2:
        # B28 : plusieurs livraisons viennent d'échouer — la question ne porte que sur CELLES-LÀ (pas sur tous les besoins).
        failed = [i for i in picked if str(i.get("recurring_need_id")) in failed_ids]
        if len(failed) >= 2:
            picked = failed
    if kind == "not_unique":
        _arbitration_log(state, intent=intent, relation="AMBIGUOUS", reason="target_not_unique", resolution="not_unique")
        label = str(picked[0].get("product") or "") if hint else ""
        question = (f"J'ai trouvé {len(picked)} besoins récurrents de {label}. Lequel voulez-vous relancer ?" if label
                    else ("Plusieurs livraisons viennent d'échouer — pour laquelle voulez-vous chercher un autre producteur ?"
                          if len(failed_ids) >= 2
                          else "Vous avez plusieurs besoins actifs — pour lequel voulez-vous relancer la recherche ?"))
        patch = await _need_choice_menu(state, mc_runtime, picked, question)
        patch["relation_to_context"] = "AMBIGUOUS"
        return patch
    _arbitration_log(state, intent=intent, relation="ANSWER", reason="contextual_command",
                     resolution={"context": "context", "name": "by_name", "single": "single"}[kind])
    target: Dict[str, Any] = state.get("context_target") if isinstance(state.get("context_target"), dict) else {}  # type: ignore[assignment]
    exact = kind == "context" and str(target.get("id") or "") == str(picked["recurring_need_id"]) and target.get("occurrence_id")
    if exact and target.get("occurrence_version") is None:
        # Contexte sans version (notification antérieure à B28) : jamais de relance à l'aveugle (B26), ni d'autre livraison.
        patch = await _show_need_detail(state, mc_runtime, str(picked["recurring_need_id"]),
                                        notice="🔎 L'état de cette livraison a changé depuis la notification — voici où elle en est.\n\n")
        patch["relation_to_context"] = "ANSWER"
        patch["response_strategy"] = "CLARIFICATION"
        if patch.get("status") == "WAITING_INPUT":
            patch["current_goal"] = "GET_MY_NEEDS"
        return patch
    if kind == "context" and not target.get("occurrence_id") and target.get("source") == "recovery_notification":
        dates = [str(_fmt_date_fr(d)) for d in target.get("dates") or []]
        if len(dates) > 1:  # plusieurs livraisons du MÊME besoin en échec : jamais en choisir une à la place de l'acheteur
            _arbitration_log(state, intent=intent, relation="AMBIGUOUS", reason="recovery_target_not_unique", resolution="not_unique")
            return {"final_response": f"Plusieurs livraisons de {picked.get('product')} sont à relancer ({', '.join(dates)}). "
                                      "Pour laquelle ? Précisez la date.", "status": "COMPLETED", "relation_to_context": "AMBIGUOUS"}
    patch = await _refresh_matching(
        state, mc_runtime, str(picked["recurring_need_id"]),
        occurrence_id=str(target["occurrence_id"]) if exact else None,
        occurrence_version=int(target["occurrence_version"]) if exact and target.get("occurrence_version") is not None else None,
    )
    patch["relation_to_context"] = "ANSWER"
    patch["response_strategy"] = "CLARIFICATION"  # l'écran rendu par le flow fait foi (pas « Je passe à… »)
    # B28 : l'écran rendu est un menu de la LISTE (goal GET_MY_NEEDS) — comme `_need_choice_menu`. Sans cela le goal restait
    # REFRESH_RECURRING_MATCHING et « 1 » (Confirmer) relançait la recherche au lieu d'accepter la proposition affichée.
    if patch.get("status") == "WAITING_INPUT":
        patch["current_goal"] = "GET_MY_NEEDS"
    return patch


def _coverage_note(requested: Any, matched: float, unit: str) -> str:
    if requested is not None and matched >= float(requested) > 0:
        return f"✅ Disponibilité complète : {_fmt_qty(matched)} {unit}"
    return f"⚠️ {_fmt_qty(matched)} {unit} disponibles sur {_fmt_qty(requested)} {unit} demandés"


async def _show_need_detail(
    state: Dict[str, Any], mc_runtime: MarketRuntime, recurring_need_id: str, *, ensure: bool = False, notice: str = ""
) -> Dict[str, Any]:
    """Écran d'UN besoin + de sa PROCHAINE occurrence, selon l'état réel (self-service B21, aucun digest requis) :
    absente (matérialisée à la demande) / OPEN sans offre (« Rechercher maintenant ») / proposition (accepter-refuser,
    version et identité EXACTES) / déjà acceptée (commandes) / terminale (demandé vs reçu)."""
    phone = state.get("user_phone")
    gw = RecurringSupplyGateway(mc_runtime)
    if ensure:
        try:
            await gw.ensure_next_recurring_occurrence(phone=str(phone), recurring_need_id=recurring_need_id)
        except MCPCallError:
            logger.warning("recurring_self_service.ensure_failed")  # le détail reste consultable
    try:
        detail = await gw.get_recurring_need_detail(phone=str(phone), recurring_need_id=recurring_need_id)
    except MCPCallError:
        return {"final_response": "Je n'ai pas pu récupérer ce détail.", "status": "COMPLETED"}
    if not isinstance(detail, dict) or detail.get("status") != "success":
        return {"final_response": "Je n'ai pas pu récupérer ce détail.", "status": "COMPLETED"}

    unit = str(detail.get("unit") or "")
    product = str(detail.get("product") or "").capitalize()
    mismatch = _list_detail_mismatch(state, recurring_need_id, detail)
    if mismatch:  # B27 : la liste et le détail ne doivent pas se contredire en silence
        notice += mismatch + "\n\n"
    header = [
        f"{notice}📦 {product}",
        f"Besoin : {_fmt_qty(detail.get('requested_quantity'))} {unit} · {_frequency_label(detail)}",
        f"Statut : {_need_status_label(detail.get('need_status'))}",
    ]
    occ_status = detail.get("occurrence_status")
    back = {"1": "LIST"}

    if detail.get("occurrence_date") is None:
        lines = header + [""]
        last = detail.get("last_occurrence")
        if last:
            lines += [f"Dernière livraison ({_fmt_date_fr(last.get('date')) or '—'}) :",
                      f"Demandé : {_fmt_qty(last.get('requested_quantity'))} {last.get('unit') or unit}"]
            if last.get("status") in ("FULFILLED", "PARTIALLY_FULFILLED", "UNFULFILLED"):
                label = {"FULFILLED": "livré", "PARTIALLY_FULFILLED": "partiellement livré", "UNFULFILLED": "non livré"}[last["status"]]
                lines += [f"Reçu : {_fmt_qty(last.get('quantity_delivered'))} {last.get('unit') or unit}", f"Résultat : {label}"]
            elif last.get("status") in ("REJECTED", "EXPIRED"):
                lines += ["Résultat : " + ("refusé" if last["status"] == "REJECTED" else "expiré")]
            lines.append("")
        need_state = str(detail.get("need_status") or "")
        if need_state == "PAUSED":
            lines.append("Besoin suspendu — aucune livraison planifiée.")
        elif need_state != "ACTIVE":
            lines.append("Besoin annulé — aucune livraison planifiée.")
        else:
            lines.append(_delivery_line({**detail, "next_occurrence_date": detail.get("planned_next_date")}))
        lines += ["", "1. Retour"]
        mapping = back
    elif occ_status in ("ACCEPTED", "PARTIALLY_ACCEPTED"):
        orders = detail.get("orders") or []
        lines = header + ["", "📦 " + _delivery_line({**detail, "schedule_state": "OK", "next_occurrence_date": detail.get("occurrence_date")}),
                          f"Demandé : {_fmt_qty(detail.get('requested_quantity'))} {unit}",
                          "Approvisionnement déjà accepté." if occ_status == "ACCEPTED" else f"Approvisionnement déjà accepté (partiellement : {_fmt_qty(detail.get('quantity_confirmed'))} sur {_fmt_qty(detail.get('requested_quantity'))} {unit}).",
                          f"{len(orders)} commande(s) créée(s) — {_fmt_qty(detail.get('quantity_confirmed'))} {unit} :"]
        lines += [f"- {o['producer_label']} : {_fmt_qty(o['quantity'])} {unit}" for o in orders]
        lines += ["", "1. Voir les commandes", "2. Retour"]
        mapping = {"1": "ORDERS", "2": "LIST"}
    else:
        allocations = [
            AllocationLine(
                producer_label=a["producer_label"], quantity=Decimal(str(a["quantity"])),
                unit_price=Decimal(str(a["unit_price"])), unit=a["unit"],
            )
            for a in detail.get("allocations") or []
        ]
        confirmable = bool(allocations)
        matched = float(detail.get("quantity_matched") or sum(float(a.quantity) for a in allocations))
        logger.info("RECURRING_PROPOSAL_VIEWED")
        if confirmable:
            body = build_detail_text(
                product=detail["product"], requested_quantity=Decimal(str(detail["requested_quantity"])), unit=detail["unit"],
                allocations=allocations, confirmable=True,
            )
            # B12 : la confirmation depuis cet écran vise l'occurrence ET la version AFFICHÉES (identité exacte, même
            # service durci que la réponse au digest) — jamais « la prochaine ouverte ».
            target = _join_match_target(recurring_need_id, detail.get("occurrence_id"), detail.get("occurrence_version"))
            mapping = {"1": f"CONFIRM:{target}", "2": f"REJECT:{target}", "3": "LIST"}
            lines = header + [_delivery_line({**detail, "schedule_state": "OK", "next_occurrence_date": detail.get("occurrence_date")}), _coverage_note(detail.get("requested_quantity"), matched, unit), "",
                              body, "", "_Tapez *actualiser* pour relancer la recherche._"]
        else:
            lines = header + ["", _delivery_line({**detail, "schedule_state": "OK", "next_occurrence_date": detail.get("occurrence_date")}),
                              f"Demandé : {_fmt_qty(detail.get('requested_quantity'))} {unit}",
                              "Disponibilité : aucune offre disponible pour le moment", "",
                              "1. Rechercher maintenant", "2. Retour"]
            mapping = {"1": f"REFRESH:{_join_match_target(recurring_need_id, detail.get('occurrence_id'), detail.get('occurrence_version'))}",
                       "2": "LIST"}
    return {
        "final_response": "\n".join(lines),
        "status": "WAITING_INPUT",  # B13 : voir `_render_needs_list` (garde le goal vivant pour « 1/2/3 »)
        "working_memory": {"recurring_need_menu": {
            "__reset__": True,  # B24 : voir `_render_needs_list`
            "mapping": mapping, "created_at": time.time(), "actions": {k: _menu_action_of(v) for k, v in mapping.items()},
            "title": f"Écran du besoin récurrent « {product} » (livraison {_fmt_date_fr(detail.get('occurrence_date')) or 'à venir'})",
            # B27 : les nombres que CET écran a montrés (une « confirmation » qui en apporte un autre est une correction).
            "shown_numbers": _shown_numbers(detail.get("requested_quantity"), detail.get("quantity_matched"),
                                            *[v for a in detail.get("allocations") or [] for v in (a.get("quantity"), a.get("unit_price"))],
                                            sum(float(a.get("quantity") or 0) * float(a.get("unit_price") or 0)
                                                for a in detail.get("allocations") or []) or None,
                                            _day_of(detail.get("occurrence_date"))),
            # B24 : la CIBLE métier de l'écran (indice de résolution pour « mets-en 3 », « cherche pour mes boeufs » ; revalidée
            # contre la liste de l'acheteur avant toute action — jamais un index de menu, jamais une preuve).
            "target": {"type": "RECURRING_NEED", "id": str(recurring_need_id), "product": product,
                       "quantity": _fmt_qty(detail.get("requested_quantity")), "unit": unit, "frequency": _frequency_label(detail),
                       # B26 : la base de toute mutation faite depuis CET écran (besoin ET livraison affichés).
                       "need_version": detail.get("need_version"), "occurrence_id": detail.get("occurrence_id"),
                       "occurrence_date": str(detail.get("occurrence_date") or "")[:10] or None,
                       "occurrence_status": occ_status, "occurrence_version": detail.get("occurrence_version")},
            "labels": {k: _ACTION_LABELS.get(_menu_action_of(v), v) for k, v in mapping.items()}}},
        **set_pending_interaction(InteractionKind.SELECTION_MENU, goal="GET_MY_NEEDS"),
    }


def _shown_numbers(*values: Any) -> List[float]:
    out: List[float] = []
    for v in values:
        try:
            if v is not None and float(v) == float(v):
                out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _day_of(iso: Any) -> Optional[int]:
    try:
        return int(str(iso)[8:10])
    except (TypeError, ValueError):
        return None


def _list_detail_mismatch(state: Dict[str, Any], need_id: str, detail: Dict[str, Any]) -> str:
    """B27 — LISTE/DÉTAIL : l'écran de détail doit parler de la MÊME livraison que la liste d'où l'acheteur l'a ouvert. Si la liste
    annonçait une date/une occurrence et que le détail en montre une autre, on ne laisse pas deux vérités circuler en silence :
    l'écart est dit, et journalisé (sans PII). Aucun identifiant ni devinette : seules les données de la liste affichée et du
    détail lu sont comparées."""
    menu = (state.get("working_memory") or {}).get("recurring_need_menu")
    listed = ((menu or {}).get("occurrences") or {}).get(need_id) if isinstance(menu, dict) else None
    if not isinstance(listed, dict) or not listed.get("date") or not detail.get("occurrence_date"):
        return ""
    shown_date = str(detail["occurrence_date"])[:10]
    same_id = listed.get("id") is None or detail.get("occurrence_id") is None or str(listed["id"]) == str(detail["occurrence_id"])
    if listed["date"] == shown_date and same_id:
        return ""
    logger.warning("RECURRING_LIST_DETAIL_MISMATCH | same_occurrence_id=%s | list_date_differs=%s", same_id, listed["date"] != shown_date)
    return (f"⚠️ La liste annonçait la livraison du {_fmt_date_fr(listed['date'])} ; ce besoin est maintenant planifié "
            f"pour le {_fmt_date_fr(shown_date)}.")


def _menu_action_of(value: str) -> str:
    """Action FERMÉE d'une entrée de menu récurrent (pour l'arbitrage de contexte : alias textuels déterministes)."""
    head = str(value).split(":", 1)[0]
    return head if head in {"CONFIRM", "REJECT", "REFRESH", "ORDERS", "VIEW", "LIST", "EXEC", "ASK"} else "SELECT"


# (2026-09-26, mandat "mismatch CONFIRM vs ACCEPT" ; élargi 2026-09-26 au 2e call-site) : les DEUX
# appelants de `RecurringSupplyGateway.accept_match_proposal` — `_respond_to_match` (menu détail
# `GET_MY_NEEDS`, ci-dessous) et `_respond_to_digest_flow` (réponse au digest, plus bas) — reçoivent
# leur `action` au vocabulaire CONVERSATIONNEL ("CONFIRM"/"REJECT") mais doivent la transmettre au
# service dans SON vocabulaire, `services/database/recurring_supply.py::MATCH_RESPONSE_ACTIONS =
# ("ACCEPT", "REJECT")`. "CONFIRM" n'y a jamais figuré : chaque appel réel à `accept_match_proposal`
# (production ET la suite de tests dédiée contre PostgreSQL réel,
# `tests/schema/test_recurring_need_confirmation_service.py`) utilise "ACCEPT" — jamais "CONFIRM".
# Le mismatch était invisible en test parce que le double de gateway du harnais (`tests/harness/
# conversation.py`) enregistre les kwargs sans les valider, contrairement au VRAI service
# (`RecurringSupplyMixin.accept_match_proposal`, qui lève `BusinessRuleException("Action inconnue :
# CONFIRM")`) — capturée par le `except MCPCallError` de chaque appelant et silencieusement comptée
# comme un échec, jamais une vraie confirmation. "REJECT" n'a jamais eu ce problème (même mot des
# deux côtés) — seule la moitié CONFIRM->ACCEPT de la table est non triviale.
#
# UNE SEULE table, partagée par les deux appelants (mandat : "une seule source de vérité", jamais
# une 2e table dupliquée) — validée à l'import contre le contrat canonique importé, jamais un enum
# recréé. `_to_match_service_action` échoue fort (AssertionError, même idiome que `services/
# database/recurring_supply.py::update_recurring_need`'s `raise AssertionError(action) # pragma: no
# cover` pour un branchement déjà filtré en amont) plutôt que de transmettre silencieusement une
# valeur hors contrat — les deux appelants ne peuvent structurellement passer que "CONFIRM"/
# "REJECT" (fermé par leurs propres appelants), donc cette branche ne devrait jamais s'exécuter ;
# si elle le fait un jour, c'est un bug à un autre endroit qui doit remonter fort, pas un 3e mot
# inventé qui atteindrait le service.
_MATCH_RESPONSE_TO_SERVICE_ACTION = {"CONFIRM": "ACCEPT", "REJECT": "REJECT"}
assert set(_MATCH_RESPONSE_TO_SERVICE_ACTION.values()) <= set(MATCH_RESPONSE_ACTIONS), (
    "_MATCH_RESPONSE_TO_SERVICE_ACTION doit rester un sous-ensemble du contrat canonique "
    "MATCH_RESPONSE_ACTIONS — toute divergence future doit casser l'import, pas silencieusement "
    "envoyer une valeur inconnue au service."
)


def _to_match_service_action(resolved_action: str) -> str:
    """Frontière conversation -> service (mandat §5) : ne transmet JAMAIS `resolved_action` tel
    quel à `accept_match_proposal` — toujours en le faisant d'abord passer par la table canonique
    ci-dessus. Lève si `resolved_action` n'y figure pas, plutôt qu'un fallback deviné."""
    service_action = _MATCH_RESPONSE_TO_SERVICE_ACTION.get(resolved_action)
    if service_action is None:
        logger.error(
            "recurring_supply.match_response_action_unmapped | resolved_action=%s", resolved_action
        )
        raise AssertionError(
            f"Action de réponse non mappée vers le contrat service : {resolved_action!r}"
        )
    return service_action


async def _respond_to_match(
    state: Dict[str, Any],
    mc_runtime: MarketRuntime,
    *,
    recurring_need_id: str,
    action: str,
    occurrence_id: Optional[str] = None,
    expected_version: Optional[int] = None,
) -> Dict[str, Any]:
    """Confirme ("CONFIRM") ou refuse ("REJECT") la proposition affichée par `_show_need_detail` —
    VS4 pilote. `RecurringSupplyGateway.accept_match_proposal` réutilise le moteur de commande
    existant (voir `services/database/recurring_supply.py::accept_match_proposal`) ; ce nœud ne
    fait que traduire son résultat en message WhatsApp, jamais de logique métier ici."""
    phone = state.get("user_phone")
    service_action = _to_match_service_action(action)
    gw = RecurringSupplyGateway(mc_runtime)
    try:
        result = await gw.accept_match_proposal(
            phone=str(phone),
            recurring_need_id=recurring_need_id,
            action=service_action,
            occurrence_id=occurrence_id,
            expected_version=expected_version,
        )
    except MCPCallError as exc:
        logger.warning(
            "recurring_supply.match_response_mapped | need=%s | resolved_action=%s | "
            "service_action=%s | result=failed | error_class=%s",
            recurring_need_id, action, service_action, type(exc).__name__,
        )
        return {
            "final_response": "Je n'ai pas pu enregistrer votre réponse — réessayez dans un instant.",
            "status": "COMPLETED",
        }

    outcome = (result or {}).get("outcome")
    if outcome:
        # Refus MÉTIER structuré (modifiée / expirée / déjà traitée / stock...) : aucune mutation.
        label = _PROPOSAL_REFUSAL_LABELS.get(str(outcome), str(outcome).lower())
        logger.info(
            "recurring_supply.match_response_mapped | need=%s | resolved_action=%s | "
            "service_action=%s | result=refused | outcome=%s",
            recurring_need_id, action, service_action, outcome,
        )
        return {
            "final_response": f"Je n'ai rien enregistré : {label}. Consultez à nouveau votre besoin pour voir la proposition à jour.",
            "status": "COMPLETED",
            "result": result,
        }
    # Log structuré (mandat §8) : même convention que `_respond_to_digest_flow` — le mapping
    # conversation -> service reste visible même quand les deux mots coïncident (REJECT->REJECT).
    logger.info(
        "recurring_supply.match_response_mapped | need=%s | resolved_action=%s | "
        "service_action=%s | result=success",
        recurring_need_id, action, service_action,
    )
    if action == "REJECT":
        message = "D'accord, pas de livraison cette fois — votre besoin habituel reste actif."
    else:
        message = "✅ C'est confirmé, votre commande est en cours de préparation."
    return {"final_response": message, "status": "COMPLETED", "result": result}


# =====================================================================
# Confirmation directe depuis le DIGEST (mandat digest, VS4 pilote), atteinte
# via `UPDATE_RECURRING_NEED` + `action` = "CONFIRM_MATCH"/"REJECT_MATCH"
# (voir `_update_flow` ci-dessus — aucun intent dédié, budget LLM saturé).
# Le digest agrège TOUS les besoins d'un acheteur en UN message
# ("CONFIRMER TOUT") ; contrairement à `_respond_to_match` (ciblé sur UN
# besoin, depuis l'écran détail), ici l'action s'applique à TOUTES les
# occurrences actionnables du moment — exactement ce que le digest a
# montré, jamais plus, jamais moins.
# =====================================================================


_PROPOSAL_REFUSAL_LABELS = {
    "ALREADY_PROCESSED": "déjà traité",
    "EXPIRED": "proposition expirée",
    "NEED_INACTIVE": "besoin désactivé",
    "PROPOSAL_CHANGED": "proposition modifiée depuis l'envoi — nouvelle validation nécessaire",
    "PRODUCER_UNAVAILABLE": "producteur ou produit indisponible",
    "NO_PROPOSAL": "plus de proposition en attente",
    "STOCK_CHANGED": "stock modifié",
    "NOT_FOUND": "introuvable",
}


def _digest_target_set(items: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """L'OBJET MÉTIER qu'une réponse courte au digest désigne — jamais « tout ce qui est matché ».

    Le digest est UN message (outbox) : aucun tour de conversation, donc aucun `PendingInteraction`
    possible. Son contenu EXACT est le payload du dernier digest (B12) : `[(occurrence_id, version)]`.
    `list_my_recurring_needs` expose, par besoin, `in_latest_digest` + `digest_occurrence_version` ;
    les cibles sont EXACTEMENT ces occurrences, chacune avec la version que le message décrivait
    (`expected_version`). Un besoin hors digest, une occurrence d'un ancien digest ne sont jamais
    confirmés par un « oui ».

    REPLI TRANSITOIRE (digest envoyé avant B12, payload sans versions) : occurrences NOTIFIÉES d'un
    besoin ACTIF du digest le plus récent, SANS version attendue (le service retombe alors sur
    l'heuristique temporelle). À retirer quand ces digests seront écoulés.
    Retourne `(cibles, écartées)`."""
    with_identity = [i for i in items if i.get("next_occurrence_id")]
    in_digest = [i for i in with_identity if i.get("in_latest_digest")]
    if in_digest:
        targets = []
        for i in in_digest:
            # Une proposition sans disponibilité au moment du digest ET inchangée depuis n'a jamais
            # été confirmable : on ne la compte pas comme « refus » (bruit) ; si elle a changé, le
            # service la refusera (PROPOSAL_CHANGED) et on le dira.
            unchanged = i.get("next_occurrence_version") == i.get("digest_occurrence_version")
            if float(i.get("matched_quantity") or 0) > 0 or not unchanged:
                targets.append({**i, "expected_version": i.get("digest_occurrence_version")})
        skipped = [i for i in with_identity if i not in in_digest]
        return targets, skipped

    candidates = [
        i
        for i in with_identity
        if float(i.get("matched_quantity") or 0) > 0 and str(i.get("status") or "ACTIVE").upper() == "ACTIVE"
    ]
    notified = [i for i in candidates if i.get("next_occurrence_notified")]
    if not notified:
        return [], candidates
    latest = max(str(i.get("next_occurrence_date") or "") for i in notified)
    legacy = [i for i in notified if str(i.get("next_occurrence_date") or "") == latest]
    logger.info("RECURRING_PROPOSAL_REPLY_ROUTED | legacy_digest=True | occurrence_ids=%s",
                [i.get("next_occurrence_id") for i in legacy])
    return [{**i, "expected_version": None} for i in legacy], [i for i in candidates if i not in legacy]


async def _respond_to_digest_flow(
    state: Dict[str, Any], mc_runtime: MarketRuntime, *, action: str
) -> Dict[str, Any]:
    phone = state.get("user_phone")
    gw = RecurringSupplyGateway(mc_runtime)
    try:
        listing = await gw.list_my_recurring_needs(phone=str(phone))
    except MCPCallError:
        return {"final_response": "Je n'ai pas pu récupérer vos approvisionnements.", "status": "COMPLETED"}

    items = listing.get("items") or []
    actionable, skipped = _digest_target_set(items)
    logger.info(
        "RECURRING_PROPOSAL_REPLY_ROUTED | action=%s | occurrence_ids=%s | skipped_occurrence_ids=%s",
        action,
        [i.get("next_occurrence_id") for i in actionable],
        [i.get("next_occurrence_id") for i in skipped],
    )
    if not actionable:
        return {
            "final_response": "Il n'y a rien à confirmer pour le moment — votre prochaine proposition arrivera bientôt.",
            "status": "COMPLETED",
        }

    service_action = _to_match_service_action(action)
    confirmed_products: List[str] = []
    failed_products: List[str] = []
    refusal_outcomes: List[str] = []
    for item in actionable:
        try:
            result = await gw.accept_match_proposal(
                phone=str(phone),
                recurring_need_id=item["recurring_need_id"],
                action=service_action,
                occurrence_id=str(item["next_occurrence_id"]),
                expected_version=item.get("expected_version"),
            )
        except MCPCallError as exc:
            # Idempotence (mandat §6) : un 2e ACCEPT/REJECT sur une occurrence déjà traitée échoue
            # proprement côté service — jamais une 2e commande. Un échec ici est donc attendu en cas
            # de double-tap, pas forcément une vraie erreur.
            logger.info(
                "recurring_supply_digest.confirm_action_mapped | need=%s | resolved_action=%s | "
                "service_action=%s | result=failed | error_class=%s",
                item["recurring_need_id"], action, service_action, type(exc).__name__,
            )
            failed_products.append(item["product"])
            continue
        outcome = (result or {}).get("outcome")
        if outcome:
            # Refus MÉTIER structuré (proposition expirée / modifiée / déjà traitée / stock...) :
            # AUCUNE mutation côté service, jamais comptée comme confirmée.
            label = _PROPOSAL_REFUSAL_LABELS.get(str(outcome), str(outcome).lower())
            refusal_outcomes.append(str(outcome))
            failed_products.append(f"{item['product']} ({label})")
            logger.info(
                "recurring_supply_digest.confirm_action_mapped | need=%s | resolved_action=%s | "
                "service_action=%s | result=refused | outcome=%s",
                item["recurring_need_id"], action, service_action, outcome,
            )
            continue
        # Log structuré (mandat §8) : le mapping conversation -> service reste visible même
        # quand les deux mots coïncident (REJECT->REJECT) — jamais implicite en observabilité.
        logger.info(
            "recurring_supply_digest.confirm_action_mapped | need=%s | resolved_action=%s | "
            "service_action=%s | result=success",
            item["recurring_need_id"], action, service_action,
        )
        confirmed_products.append(item["product"])

    if action == "REJECT" and confirmed_products:
        message = "D'accord, rien ne sera livré demain — vos besoins habituels restent actifs."
    elif confirmed_products and failed_products:
        # (B9) succès PARTIEL : jamais « vos commandes sont en cours de préparation » si une partie
        # seulement a été confirmée (un échec peut aussi être un double-tap déjà traité).
        message = (
            f"✅ Confirmé : {', '.join(confirmed_products)}.\n"
            f"⚠️ Non confirmé : {', '.join(failed_products)}."
        )
    elif confirmed_products:
        message = "✅ C'est confirmé, vos commandes sont en cours de préparation."
    elif refusal_outcomes and len(set(refusal_outcomes)) == 1 and len(refusal_outcomes) == len(failed_products):
        label = _PROPOSAL_REFUSAL_LABELS.get(refusal_outcomes[0], refusal_outcomes[0].lower())
        message = f"Je n'ai rien confirmé : {label}. Votre prochaine proposition arrivera bientôt."
    elif refusal_outcomes:
        message = f"Je n'ai rien confirmé : {', '.join(failed_products)}."
    else:
        message = "Je n'ai pas pu confirmer votre approvisionnement — réessayez dans un instant."
    return {
        "final_response": message,
        "status": "COMPLETED",
        "result": {"confirmed": confirmed_products, "skipped": failed_products},
    }


# =====================================================================
# "modifier" en réponse au digest — mini-flow guidé (mandat digest §5/§6/§12, 2026-09-26)
#
# État explicite, porté par `PendingInteraction(RECURRING_SUPPLY_DIGEST_ACTION).target`
# (mandat §12 : "pas de chaînes implicites dispersées") :
#   DIGEST_AWAIT_ACTION     — pas de `PendingInteraction` persisté : le premier "modifier" bare
#                             (voir `interpreter/routing.py`) est traité SANS état préalable.
#   DIGEST_AWAIT_SELECTION  — menu numéroté affiché, attend un index (2+ besoins actionnables).
#   DIGEST_AWAIT_QUANTITY   — besoin choisi (ou seul besoin actionnable), attend une quantité.
#   DIGEST_CONFIRM_OVERRIDE — transition interne (même tour que la quantité reçue, voir mandat
#                             §16 : l'override s'applique DIRECTEMENT, sans confirmation
#                             supplémentaire) — jamais persistée, nommée ici pour les logs (§14).
#   DIGEST_DONE             — l'override est appliqué, `pending_interaction` est résolu (NONE).
#
# Réutilise `update_recurring_need(action="OCCURRENCE_OVERRIDE", ...)`, DÉJÀ implémenté et
# testé côté service (`services/database/recurring_supply.py`) — CAS sur `occurrence.version`,
# refuse une occurrence hors `OPEN/MATCHED` — jamais un second moteur de mutation ; ce mini-flow
# ne fait qu'orchestrer les tours conversationnels qui y mènent.
# =====================================================================

_DIGEST_AWAIT_SELECTION = "DIGEST_AWAIT_SELECTION"
_DIGEST_AWAIT_QUANTITY = "DIGEST_AWAIT_QUANTITY"


async def _digest_actionable_items(gw: RecurringSupplyGateway, phone: Any) -> List[Dict[str, Any]]:
    """Besoins ayant une occurrence de DEMAIN encore ouverte — exactement le périmètre montré par
    le digest (`RecurringSupplyDigestService`/`domain/recurring_supply/digest.py`), jamais la
    liste complète (`GET_MY_NEEDS`, qui inclut aussi les besoins sans occurrence active). Un
    besoin à 0 KG disponible (ex: "Oignon : 0/75") reste modifiable ICI — contrairement à
    `_respond_to_digest_flow` (CONFIRM/REJECT), qui ne porte que sur ce qui a été RÉELLEMENT
    matché (`matched_quantity > 0`)."""
    result = await gw.list_my_recurring_needs(phone=str(phone))
    items = result.get("items") or []
    return [i for i in items if i.get("next_occurrence_id")]


def _digest_occurrence_version(item: Dict[str, Any]) -> Optional[int]:
    """Version de l'OCCURRENCE que l'acheteur a sous les yeux (B26) : celle du digest qu'il a reçu (`digest_occurrence_version`,
    figée à l'envoi) ; sans snapshot de digest, celle lue ce tour (`next_occurrence_version`). `None` : aucune version connue —
    la passerelle refuse alors toute mutation (jamais « la version courante » par omission)."""
    version = item.get("digest_occurrence_version") if item.get("in_latest_digest") else None
    if version is None:
        version = item.get("next_occurrence_version")
    return int(version) if version is not None else None


def _digest_menu_entities(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "index": i,
            "recurring_need_id": item["recurring_need_id"],
            "occurrence_id": item["next_occurrence_id"],
            "occurrence_version": _digest_occurrence_version(item),  # B26 : base de l'override (jamais relue au moment d'écrire)
            "occurrence_date": item["next_occurrence_date"],
            "product": item["product"],
            "unit": item.get("unit"),
            "requested_quantity": item.get("requested_quantity"),
            "matched_quantity": item.get("matched_quantity"),
        }
        for i, item in enumerate(items, start=1)
    ]


def _digest_menu_text(items: List[Dict[str, Any]], *, notice: str = "") -> str:
    lines = [f"{notice}Quel besoin veux-tu modifier pour demain ?", ""]
    for entry in items:
        matched, requested = entry.get("matched_quantity"), entry.get("requested_quantity")
        avail = (
            f" ({_fmt_qty(matched)}/{_fmt_qty(requested)} {entry['unit']} disponibles)"
            if matched is not None and requested is not None
            else ""
        )
        lines.append(f"{entry['index']}. {str(entry['product']).capitalize()}{avail}")
    lines += ["", "Réponds avec le numéro."]
    return "\n".join(lines)


def _digest_quantity_prompt(product: str) -> str:
    return f"Quelle quantité veux-tu pour *{product}* demain ?"


async def _digest_modify_flow_start(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    phone = state.get("user_phone")
    gw = RecurringSupplyGateway(mc_runtime)
    try:
        items = await _digest_actionable_items(gw, phone)
    except MCPCallError as exc:
        logger.warning("recurring_supply_digest.modify_start_failed | error_class=%s", type(exc).__name__)
        return {"final_response": "Je n'ai pas pu récupérer vos approvisionnements.", "status": "COMPLETED"}

    if not items:
        return {
            "final_response": "Il n'y a rien à modifier pour le moment — votre prochain digest arrivera bientôt.",
            "status": "COMPLETED",
        }

    entries = _digest_menu_entities(items)
    logger.info(
        "recurring_supply_digest.modify_started | pending_state=%s | occurrence_ids=%s | needs_count=%s",
        _DIGEST_AWAIT_SELECTION if len(entries) > 1 else _DIGEST_AWAIT_QUANTITY,
        [e["occurrence_id"] for e in entries],
        len(entries),
    )

    if len(entries) == 1:
        # Un seul besoin actionnable : mandat §5 n'exige un menu QUE s'il faut choisir — même
        # esprit que `_pick_need`, qui saute la question quand un seul besoin existe.
        chosen = entries[0]
        return {
            "final_response": _digest_quantity_prompt(chosen["product"]),
            "status": "WAITING_INPUT",
            **set_pending_interaction(
                InteractionKind.RECURRING_SUPPLY_DIGEST_ACTION,
                goal="UPDATE_RECURRING_NEED",
                target={"sub_state": _DIGEST_AWAIT_QUANTITY, "selected": chosen},
            ),
        }

    return {
        "final_response": _digest_menu_text(entries),
        "status": "WAITING_INPUT",
        **set_pending_interaction(
            InteractionKind.RECURRING_SUPPLY_DIGEST_ACTION,
            goal="UPDATE_RECURRING_NEED",
            target={"sub_state": _DIGEST_AWAIT_SELECTION, "items": entries},
        ),
    }


async def _digest_modify_flow_continue(
    state: Dict[str, Any], mc_runtime: MarketRuntime, pending: Any
) -> Dict[str, Any]:
    target = dict(pending.target or {})
    sub_state = target.get("sub_state")
    payload = state.get("transaction_payload") or {}

    if sub_state == _DIGEST_AWAIT_SELECTION:
        items: List[Dict[str, Any]] = target.get("items") or []
        index = payload.get("selection_index")
        chosen = next((it for it in items if it.get("index") == index), None) if index is not None else None
        if chosen is None:
            logger.info("recurring_supply_digest.modify_selection_invalid | pending_state=%s", sub_state)
            return {
                "final_response": _digest_menu_text(items, notice="Je n'ai pas compris ce choix.\n\n"),
                "status": "WAITING_INPUT",
                **set_pending_interaction(
                    InteractionKind.RECURRING_SUPPLY_DIGEST_ACTION,
                    goal="UPDATE_RECURRING_NEED",
                    target={"sub_state": _DIGEST_AWAIT_SELECTION, "items": items},
                ),
            }
        logger.info(
            "recurring_supply_digest.modify_selected | pending_state=%s | occurrence_id=%s",
            _DIGEST_AWAIT_QUANTITY,
            chosen.get("occurrence_id"),
        )
        return {
            "final_response": _digest_quantity_prompt(chosen["product"]),
            "status": "WAITING_INPUT",
            **set_pending_interaction(
                InteractionKind.RECURRING_SUPPLY_DIGEST_ACTION,
                goal="UPDATE_RECURRING_NEED",
                target={"sub_state": _DIGEST_AWAIT_QUANTITY, "selected": chosen},
            ),
        }

    if sub_state == _DIGEST_AWAIT_QUANTITY:
        selected: Dict[str, Any] = target.get("selected") or {}
        quantity = payload.get("quantity")
        if not slot_has_value(quantity):
            logger.info("recurring_supply_digest.modify_quantity_missing | pending_state=%s", sub_state)
            return {
                "final_response": (
                    f"Je n'ai pas compris la quantité. {_digest_quantity_prompt(selected.get('product', ''))}"
                ),
                "status": "WAITING_INPUT",
                **set_pending_interaction(
                    InteractionKind.RECURRING_SUPPLY_DIGEST_ACTION,
                    goal="UPDATE_RECURRING_NEED",
                    target={"sub_state": _DIGEST_AWAIT_QUANTITY, "selected": selected},
                ),
            }

        # `slot_has_value(quantity)` (ci-dessus) garantit déjà une valeur exploitable à
        # l'exécution — ce narrowing explicite n'est là que pour mypy (`quantity` reste
        # typé `Any | None` via `transaction_payload`, jamais affiné par un simple contrôle
        # de vérité).
        quantity_value = float(quantity) if isinstance(quantity, (int, float, str)) else 0.0

        phone = state.get("user_phone")
        gw = RecurringSupplyGateway(mc_runtime)
        try:
            # DIGEST_CONFIRM_OVERRIDE (mandat §12) — transition interne, MÊME tour : le mandat
            # §16 attend l'override appliqué dès cette quantité reçue, sans confirmation
            # supplémentaire. Occurrence UNIQUEMENT (mandat §5 : "ne pas modifier automatiquement
            # le recurring need permanent") — `RecurringNeed.quantity` n'est jamais touché ici.
            applied = await gw.update_recurring_need(
                phone=str(phone),
                recurring_need_id=selected["recurring_need_id"],
                action="OCCURRENCE_OVERRIDE",
                occurrence_date=selected["occurrence_date"],
                quantity=quantity_value,
                expected_occurrence_version=selected.get("occurrence_version"),
            )
            if isinstance(applied, dict) and applied.get("status") == "conflict":
                logger.info("RECURRING_VERSION_CONFLICT | caller_path=digest_override | action=OCCURRENCE_OVERRIDE | scope=OCCURRENCE")
                return {
                    "final_response": (
                        f"La livraison de {selected.get('product', '')} a été modifiée depuis ce message. Rien n'a été changé : "
                        "répondez « modifier » pour repartir de l'état à jour."
                    ),
                    "status": "COMPLETED",
                    **resolve_pending_interaction(),
                }
        except MCPCallError as exc:
            logger.warning(
                "recurring_supply_digest.modify_override_failed | occurrence_id=%s | error_class=%s",
                selected.get("occurrence_id"),
                type(exc).__name__,
            )
            return {
                "final_response": "Je n'ai pas pu appliquer ce changement — réessayez dans un instant.",
                "status": "COMPLETED",
                **resolve_pending_interaction(),
            }

        logger.info(
            "recurring_supply_digest.modify_override_applied | pending_state=DIGEST_DONE | "
            "occurrence_id=%s | quantity=%s",
            selected.get("occurrence_id"),
            quantity_value,
        )
        unit = payload.get("unit") or selected.get("unit") or ""
        product_label = selected.get("product", "")
        return {
            "final_response": (
                f"✅ C'est noté : *{_fmt_qty(quantity_value)} {unit}* de {product_label} pour demain "
                "uniquement — votre besoin habituel n'a pas changé."
            ),
            "status": "COMPLETED",
            **resolve_pending_interaction(),
        }

    # sub_state inconnu (corruption défensive, ex: TTL/migration future) : abandon propre plutôt
    # qu'une boucle silencieuse sur un état qu'on ne sait plus interpréter.
    logger.warning("recurring_supply_digest.modify_unknown_sub_state | pending_state=%s", sub_state)
    return {
        "final_response": "Je n'ai pas pu poursuivre cette modification — dites-moi ce que vous voulez changer.",
        "status": "COMPLETED",
        **resolve_pending_interaction(),
    }


async def _digest_skip_named_product(
    state: Dict[str, Any], mc_runtime: MarketRuntime, product_hint: str
) -> Dict[str, Any]:
    """"pas demain pour l'oignon" (mandat digest §9) — skip l'occurrence de DEMAIN d'UN SEUL
    besoin nommé, jamais tous les besoins actionnables (voir `_respond_to_digest_flow`, chemin
    "pas demain" bare). Résolution par nom BORNÉE : abstention (question) si zéro ou plusieurs
    besoins actionnables correspondent — jamais un choix deviné."""
    phone = state.get("user_phone")
    gw = RecurringSupplyGateway(mc_runtime)
    try:
        items = await _digest_actionable_items(gw, phone)
    except MCPCallError as exc:
        logger.warning("recurring_supply_digest.skip_product_failed | error_class=%s", type(exc).__name__)
        return {"final_response": "Je n'ai pas pu récupérer vos approvisionnements.", "status": "COMPLETED"}

    hint = product_hint.strip().lower()
    matches = [i for i in items if hint and hint in str(i.get("product") or "").lower()]
    if len(matches) != 1:
        names = ", ".join(str(i["product"]) for i in items) if items else "aucun"
        return {
            "final_response": f"Je n'ai pas trouvé de besoin correspondant. Vos besoins de demain : {names}.",
            "status": "COMPLETED",
        }

    target = matches[0]
    try:
        applied = await gw.update_recurring_need(
            phone=str(phone),
            recurring_need_id=target["recurring_need_id"],
            action="OCCURRENCE_SKIP",
            occurrence_date=target["next_occurrence_date"],
            expected_occurrence_version=_digest_occurrence_version(target),
        )
        if isinstance(applied, dict) and applied.get("status") == "conflict":
            logger.info("RECURRING_VERSION_CONFLICT | caller_path=digest_skip | action=OCCURRENCE_SKIP | scope=OCCURRENCE")
            return {
                "final_response": (
                    f"La livraison de {target['product']} a été modifiée depuis ce message. Rien n'a été changé : "
                    "redites « pas demain » pour repartir de l'état à jour."
                ),
                "status": "COMPLETED",
            }
    except MCPCallError as exc:
        logger.warning(
            "recurring_supply_digest.skip_product_apply_failed | occurrence_id=%s | error_class=%s",
            target.get("next_occurrence_id"),
            type(exc).__name__,
        )
        return {"final_response": "Je n'ai pas pu appliquer ce changement — réessayez dans un instant.", "status": "COMPLETED"}

    logger.info(
        "recurring_supply_digest.skip_product_applied | occurrence_id=%s",
        target.get("next_occurrence_id"),
    )
    return {
        "final_response": (
            f"D'accord, pas de {target['product']} demain — le reste de vos besoins habituels reste actif."
        ),
        "status": "COMPLETED",
    }


def _same_product(hint: str, label: str) -> bool:
    """Nom cité vs nom du besoin, insensible à la casse/accents/pluriel simple (« chèvres » ~ « chèvre »)."""
    a, b = _fold_text(hint), _fold_text(label)
    return bool(a and b and (a == b or a in b or b in a or a.rstrip("s") == b.rstrip("s")))


__all__ = ["recurring_need_flow"]
