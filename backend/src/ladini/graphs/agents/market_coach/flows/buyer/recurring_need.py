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

import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

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
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.core.state import entities_said_this_turn
from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
    CancelRecurringNeedDraft,
    ConfirmRecurringNeedDraft,
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
from ladini.graphs.agents.market_coach.services.mcp.gateway import (
    MCPCallError,
    RecurringSupplyGateway,
)
from ladini.graphs.agents.market_coach.utils import MarketRuntime, slot_has_value
from ladini.services.database import recurring_need_draft_store
from ladini.services.database.draft_store_support import cas_finalize

logger = logging.getLogger("Ladini.Market.RecurringNeed")

# Menu "mes besoins" / détail (mandat Phase 4 §6) — même durée que la confirmation générique
# (`nodes/confirmation_gate.py::_CONFIRMATION_TTL_SECONDS`) : passé ce délai, une réponse numérique
# ne cible plus rien de fiable, on réaffiche la liste plutôt que d'appliquer un choix périmé.
_MENU_TTL_SECONDS = 600.0
_BACK_WORDS = ("retour", "besoin", "mes besoins")

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
            resolved_unit = str(unit).strip() if slot_has_value(unit) else default_unit_for_product(product)
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
}


async def recurring_need_flow(state: Dict[str, Any], mc_runtime: MarketRuntime) -> Dict[str, Any]:
    goal = str(state.get("current_goal") or "").upper()
    if goal == "CREATE_RECURRING_NEED":
        return await _create_flow(state, mc_runtime)
    if goal == "UPDATE_RECURRING_NEED":
        return await _update_flow(state, mc_runtime)
    if goal == "GET_MY_NEEDS":
        return await _get_my_needs_flow(state, mc_runtime)
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
        if _is_correction(interpreted_event, said):
            return await _correct(draft, said, state, conversation_id)

    if interpreted_event in ("NEW_TASK", "INTERRUPTION"):
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
    await _persist(draft, outcome.draft, conversation_id)
    if isinstance(action, UpdateRecurringNeedDraft):
        ambiguous_response = _ambiguous_quantity_clarification(payload, outcome.draft)
        if ambiguous_response is not None:
            return ambiguous_response
    return _apply_response_plan(build_response_plan(outcome))


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


def _is_correction(interpreted_event: str, said: Dict[str, Any]) -> bool:
    """Politique Phase 2 (C) — décision déterministe sur la STRUCTURE du message :
      - REJECT porteur de valeurs (« non, plutôt 23 bœufs ») -> correction ;
      - même intention reformulée SANS sa propre fréquence -> correction du draft en cours
        (une nouvelle demande autonome redit sa fréquence : « je veux 30 poulets chaque
        semaine » reste une nouvelle demande, voir test F)."""
    if not said:
        return False
    if interpreted_event == "REJECT":
        return True
    return interpreted_event in ("NEW_TASK", "INTERRUPTION") and not said.get("recurrence_type")


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
    return await _settle(executing, RecurringNeedExecutionResult(success=True, external_id=_created_ids(mcp_result)))


async def _settle(executing: RecurringNeedDraft, result: RecurringNeedExecutionResult) -> Dict[str, Any]:
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
    phone = state.get("user_phone")
    payload: Dict[str, Any] = state.get("transaction_payload") or {}

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

    gw = RecurringSupplyGateway(mc_runtime)
    resolved = await _resolve_target_need(gw, phone, payload)
    if resolved is None:
        return {"final_response": "Vous n'avez aucun besoin actif pour l'instant.", "status": "COMPLETED"}
    if isinstance(resolved, str):
        return {"final_response": resolved, "status": "WAITING_INPUT"}
    need_id, product_label = resolved

    action, kwargs = _resolve_update_action(payload)
    if action is None:
        return {
            "final_response": "Je n'ai pas compris ce que vous voulez changer sur ce besoin.",
            "status": "WAITING_INPUT",
        }

    try:
        result = await gw.update_recurring_need(phone=str(phone), recurring_need_id=need_id, action=action, **kwargs)
    except MCPCallError as exc:
        logger.warning("recurring_need.update_failed | need=%s | action=%s | %s", need_id, action, exc)
        return {"final_response": "Je n'ai pas pu appliquer ce changement.", "status": "COMPLETED"}

    return {"final_response": _render_update_confirmation(action, product_label), "status": "COMPLETED", "result": result}


def _resolve_update_action(payload: Dict[str, Any]):
    """Mandat §6 : UNE action structurée, jamais un intent séparé par verbe."""
    action_hint = str(payload.get("action") or "").upper().strip()
    occurrence_date = payload.get("occurrence_date")
    quantity = payload.get("quantity")

    if action_hint == "PAUSE" or payload.get("pause"):
        return "PAUSE", {"paused_until": payload.get("paused_until")}
    if action_hint == "RESUME" or payload.get("resume"):
        return "RESUME", {}
    if action_hint == "CANCEL" or payload.get("cancel"):
        return "CANCEL", {}
    if action_hint == "OCCURRENCE_SKIP" or (occurrence_date and payload.get("skip")):
        return "OCCURRENCE_SKIP", {"occurrence_date": occurrence_date}
    if action_hint == "OCCURRENCE_OVERRIDE" or (occurrence_date and slot_has_value(quantity)):
        return "OCCURRENCE_OVERRIDE", {"occurrence_date": occurrence_date, "quantity": quantity}
    if action_hint == "PERMANENT_FREQUENCY" or payload.get("recurrence_type"):
        return "PERMANENT_FREQUENCY", {
            "recurrence_type": payload.get("recurrence_type"),
            "weekly_days": payload.get("weekly_days"),
            "excluded_weekdays": payload.get("excluded_weekdays"),
        }
    if action_hint == "PERMANENT_QUANTITY" or slot_has_value(quantity):
        return "PERMANENT_QUANTITY", {"quantity": quantity}
    return None, {}


def _render_update_confirmation(action: str, product_label: str) -> str:
    labels = {
        "PERMANENT_QUANTITY": f"C'est noté, la quantité de {product_label} est mise à jour.",
        "PERMANENT_FREQUENCY": f"C'est noté, la fréquence de {product_label} est mise à jour.",
        "PAUSE": f"D'accord, {product_label} est suspendu.",
        "RESUME": f"D'accord, {product_label} a repris.",
        "CANCEL": f"D'accord, {product_label} est annulé.",
        "OCCURRENCE_OVERRIDE": "C'est noté pour cette date.",
        "OCCURRENCE_SKIP": "D'accord, pas de livraison à cette date.",
    }
    return labels.get(action, "C'est fait.")


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
    resolved = _resolve_menu_reply(state)
    if resolved is not None:
        kind, target = resolved
        if kind == "DETAIL":
            return await _show_need_detail(state, mc_runtime, target)
        if kind in ("CONFIRM", "REJECT"):
            return await _respond_to_match(state, mc_runtime, recurring_need_id=target, action=kind)
        if kind == "INVALID":
            return await _render_needs_list(state, mc_runtime, notice="Je n'ai pas compris ce choix.\n\n")
        # kind == "LIST" (retour / mes besoins) — retombe sur la liste fraîche ci-dessous.
    return await _render_needs_list(state, mc_runtime)


def _resolve_menu_reply(state: Dict[str, Any]):
    """`None` : aucun menu actif pour CE goal (première visite, ou menu d'un autre tunnel — jamais
    touché). Sinon `("LIST"|"DETAIL"|"CONFIRM"|"REJECT"|"INVALID", target)`. `target` est un
    `recurring_need_id` pour `DETAIL`/`CONFIRM`/`REJECT`, `None` pour `LIST`/`INVALID`."""
    pending = state.get("pending_interaction") or {}
    if not isinstance(pending, dict) or pending.get("kind") != "SELECTION_MENU" or pending.get("goal") != "GET_MY_NEEDS":
        return None
    menu = (state.get("working_memory") or {}).get("recurring_need_menu")
    if not isinstance(menu, dict):
        return None
    if time.time() - float(pending.get("created_at") or 0) > _MENU_TTL_SECONDS:
        return None  # périmé — traité comme une première visite, silencieusement (même convention que confirmation_gate)

    payload = state.get("transaction_payload") or {}
    index = str(payload.get("selection_index") or "").strip()
    text_lower = str(state.get("normalized_text") or state.get("user_query") or "").strip().lower()

    mapping: Dict[str, str] = menu.get("mapping") or {}
    chosen = mapping.get(index)
    if chosen is None and any(word in text_lower for word in _BACK_WORDS):
        chosen = "LIST"
    if chosen is None:
        return ("INVALID", None)
    if chosen == "LIST":
        return ("LIST", None)
    # "CONFIRM:<recurring_need_id>" / "REJECT:<recurring_need_id>" (VS4) — préfixe fermé, jamais
    # deviné : une valeur de mapping mal formée retombe sur DETAIL, jamais sur une action muette.
    for prefix, kind in (("CONFIRM:", "CONFIRM"), ("REJECT:", "REJECT")):
        if chosen.startswith(prefix):
            return (kind, chosen[len(prefix):])
    return ("DETAIL", chosen)


async def _render_needs_list(state: Dict[str, Any], mc_runtime: MarketRuntime, *, notice: str = "") -> Dict[str, Any]:
    phone = state.get("user_phone")
    gw = RecurringSupplyGateway(mc_runtime)
    try:
        result = await gw.list_my_recurring_needs(phone=str(phone))
    except MCPCallError:
        return {"final_response": "Je n'ai pas pu récupérer vos besoins.", "status": "COMPLETED"}

    items: List[Dict[str, Any]] = result.get("items") or []
    if not items:
        return {"final_response": f"{notice}Vous n'avez pas encore de besoin récurrent enregistré.", "status": "COMPLETED"}

    lines = [f"{notice}Vos approvisionnements :", ""]
    mapping: Dict[str, str] = {}
    for i, item in enumerate(items, start=1):
        lines.append(f"{i}. {_render_need_line(item)}")
        mapping[str(i)] = str(item["recurring_need_id"])
    lines += ["", "Répondez avec le numéro d'un besoin pour voir sa disponibilité."]

    return {
        "final_response": "\n".join(lines),
        "status": "COMPLETED",
        "working_memory": {"recurring_need_menu": {"mapping": mapping, "created_at": time.time()}},
        **set_pending_interaction(InteractionKind.SELECTION_MENU, goal="GET_MY_NEEDS"),
    }


def _render_need_line(item: Dict[str, Any]) -> str:
    freq = {
        "DAILY": "jour",
        "WEEKLY": "semaine",
        "ONE_OFF": "une fois",
    }.get(item.get("recurrence_type"), "jour")
    status = "actif" if item.get("status") == "ACTIVE" else "en pause" if item.get("status") == "PAUSED" else "annulé"
    label = f"{item.get('product', '?').capitalize()} — {item.get('quantity')} {item.get('unit')}/{freq} — {status}"
    requested, matched = item.get("requested_quantity"), item.get("matched_quantity")
    if requested is not None and matched is not None:
        emoji = "✅" if matched >= requested and requested > 0 else "❌" if matched <= 0 else "⚠️"
        label += f" — {emoji} {_fmt_qty(matched)}/{_fmt_qty(requested)} {item.get('unit')} disponibles demain"
    return label


def _fmt_qty(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


async def _show_need_detail(state: Dict[str, Any], mc_runtime: MarketRuntime, recurring_need_id: str) -> Dict[str, Any]:
    phone = state.get("user_phone")
    gw = RecurringSupplyGateway(mc_runtime)
    try:
        detail = await gw.get_recurring_need_detail(phone=str(phone), recurring_need_id=recurring_need_id)
    except MCPCallError:
        return {"final_response": "Je n'ai pas pu récupérer ce détail.", "status": "COMPLETED"}
    if not isinstance(detail, dict) or detail.get("status") != "success":
        return {"final_response": "Je n'ai pas pu récupérer ce détail.", "status": "COMPLETED"}

    allocations = [
        AllocationLine(
            producer_label=a["producer_label"],
            quantity=Decimal(str(a["quantity"])),
            unit_price=Decimal(str(a["unit_price"])),
            unit=a["unit"],
        )
        for a in detail.get("allocations") or []
    ]
    confirmable = bool(allocations)
    text = build_detail_text(
        product=detail["product"],
        requested_quantity=Decimal(str(detail["requested_quantity"])),
        unit=detail["unit"],
        allocations=allocations,
        confirmable=confirmable,
    )
    mapping = (
        {"1": f"CONFIRM:{recurring_need_id}", "2": f"REJECT:{recurring_need_id}", "3": "LIST"}
        if confirmable
        else {"1": "LIST", "2": "LIST"}
    )
    return {
        "final_response": text,
        "status": "COMPLETED",
        "working_memory": {"recurring_need_menu": {"mapping": mapping, "created_at": time.time()}},
        **set_pending_interaction(InteractionKind.SELECTION_MENU, goal="GET_MY_NEEDS"),
    }


async def _respond_to_match(
    state: Dict[str, Any], mc_runtime: MarketRuntime, *, recurring_need_id: str, action: str
) -> Dict[str, Any]:
    """Confirme ("CONFIRM") ou refuse ("REJECT") la proposition affichée par `_show_need_detail` —
    VS4 pilote. `RecurringSupplyGateway.accept_match_proposal` réutilise le moteur de commande
    existant (voir `services/database/recurring_supply.py::accept_match_proposal`) ; ce nœud ne
    fait que traduire son résultat en message WhatsApp, jamais de logique métier ici."""
    phone = state.get("user_phone")
    gw = RecurringSupplyGateway(mc_runtime)
    try:
        result = await gw.accept_match_proposal(
            phone=str(phone), recurring_need_id=recurring_need_id, action=action
        )
    except MCPCallError as exc:
        logger.warning(
            "recurring_need.match_response_failed | need=%s | action=%s | %s",
            recurring_need_id, action, exc,
        )
        return {
            "final_response": "Je n'ai pas pu enregistrer votre réponse — réessayez dans un instant.",
            "status": "COMPLETED",
        }

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
    # Seuls les besoins avec une disponibilité RÉELLEMENT trouvée (mandat CAS 5 : une réponse
    # tardive sur une proposition déjà traitée/expirée ne doit jamais créer de commande) —
    # `accept_match_proposal` referait de toute façon ce contrôle par besoin, mais le filtrer ici
    # évite un message générique "rien à confirmer" répété autant de fois qu'il y a de besoins.
    actionable = [i for i in items if float(i.get("matched_quantity") or 0) > 0]
    if not actionable:
        return {
            "final_response": "Il n'y a rien à confirmer pour le moment — votre prochaine proposition arrivera bientôt.",
            "status": "COMPLETED",
        }

    confirmed_products: List[str] = []
    failed_products: List[str] = []
    for item in actionable:
        try:
            await gw.accept_match_proposal(
                phone=str(phone), recurring_need_id=item["recurring_need_id"], action=action
            )
            confirmed_products.append(item["product"])
        except MCPCallError as exc:
            # Idempotence (mandat §6) : un 2e ACCEPT/REJECT sur une occurrence déjà traitée échoue
            # proprement côté service (occurrence hors OPEN/MATCHED) — jamais une 2e commande. Un
            # échec ici est donc attendu en cas de double-tap, pas forcément une vraie erreur.
            logger.info(
                "recurring_need.digest_response_skipped | need=%s | action=%s | %s",
                item["recurring_need_id"], action, exc,
            )
            failed_products.append(item["product"])

    if action == "REJECT":
        message = "D'accord, rien ne sera livré demain — vos besoins habituels restent actifs."
    elif confirmed_products:
        message = "✅ C'est confirmé, vos commandes sont en cours de préparation."
    else:
        message = "Je n'ai pas pu confirmer votre approvisionnement — réessayez dans un instant."
    return {
        "final_response": message,
        "status": "COMPLETED",
        "result": {"confirmed": confirmed_products, "skipped": failed_products},
    }


async def _resolve_target_need(gw: RecurringSupplyGateway, phone: Any, payload: Dict[str, Any]):
    """Résout le besoin visé par nom de produit — jamais un UUID brut (même principe que
    `PROCUREMENT_UPDATE_REQUEST`). Retourne `(need_id, label)`, `None` (aucun besoin), ou une chaîne
    (question de clarification, ambiguïté)."""
    try:
        result = await gw.list_my_recurring_needs(phone=str(phone))
    except MCPCallError:
        return "Je n'ai pas pu récupérer vos besoins."
    items: List[Dict[str, Any]] = result.get("items") or []
    active = [i for i in items if i.get("status") in ("ACTIVE", "PAUSED")]
    if not active:
        return None
    if len(active) == 1:
        return active[0]["recurring_need_id"], active[0]["product"]

    product_hint = str(payload.get("product") or "").strip().lower()
    if product_hint:
        matches = [i for i in active if product_hint in str(i.get("product") or "").lower()]
        if len(matches) == 1:
            return matches[0]["recurring_need_id"], matches[0]["product"]

    names = ", ".join(i["product"] for i in active)
    return f"Vous avez plusieurs besoins actifs ({names}) — lequel voulez-vous modifier ?"


__all__ = ["recurring_need_flow"]
