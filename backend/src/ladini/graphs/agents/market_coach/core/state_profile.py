"""StateProfile — Classification des champs de MarketAgentState.

Chaque champ est classé dans une catégorie de cycle de vie :

  DURABLE   — Survit entre les tours. Identité utilisateur, contexte de session,
              objectif courant, panier, payload transactionnel, etc.
  EPHEMERAL — Significatif uniquement pendant le tour courant. Réinitialisé par
              le state_cleaner / post_response_cleanup avant le checkpoint final.
  DERIVED   — Recalculé à chaque tour par un nœud déterministe. Ne nécessite
              pas de persistance — économise de l'espace checkpoint.

Le StateProfile sert de source unique pour :
  1. Le state_cleaner (quels champs réinitialiser)
  2. Le post_response_cleanup (quels champs vider après la réponse)
  3. La documentation (cycle de vie de chaque champ)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, FrozenSet, Optional, Tuple


class FieldLifecycle(Enum):
    DURABLE = "DURABLE"
    EPHEMERAL = "EPHEMERAL"
    DERIVED = "DERIVED"


@dataclass(frozen=True)
class FieldSpec:
    name: str
    lifecycle: FieldLifecycle
    reset_value: Any = None


# =====================================================================
# FIELD REGISTRY — one entry per MarketAgentState + BuyerContext +
# ProducerContext field, grouped by section.
# =====================================================================

_FIELDS: Tuple[FieldSpec, ...] = (
    # ── 1. RAW INPUT LAYER ────────────────────────────────────────
    FieldSpec("user_query", FieldLifecycle.DURABLE),
    FieldSpec("normalized_text", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("input_truncated", FieldLifecycle.EPHEMERAL, reset_value=False),
    FieldSpec("detected_language", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("translated_text", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("audio_file_path", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("transcribed_audio", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("timestamp", FieldLifecycle.DURABLE),
    # ── 2. USER / SESSION CONTEXT ─────────────────────────────────
    FieldSpec("user_phone", FieldLifecycle.DURABLE),
    FieldSpec("session_id", FieldLifecycle.DURABLE),
    FieldSpec("user_role", FieldLifecycle.DURABLE),
    FieldSpec("user_name", FieldLifecycle.DURABLE),
    FieldSpec("zone_name", FieldLifecycle.DURABLE),
    FieldSpec("zone_id", FieldLifecycle.DURABLE),
    FieldSpec("user_context_loaded", FieldLifecycle.DURABLE),
    FieldSpec("user_id", FieldLifecycle.DURABLE),
    FieldSpec("is_onboarding", FieldLifecycle.DURABLE),
    FieldSpec("onboarding_step", FieldLifecycle.DURABLE),
    FieldSpec("onboarding_internal_step", FieldLifecycle.DURABLE),
    FieldSpec("onboarding_mode", FieldLifecycle.DURABLE),
    FieldSpec("onboarding_profile", FieldLifecycle.DURABLE),
    FieldSpec("turn_count", FieldLifecycle.DURABLE),
    FieldSpec("proactive_hint", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("conversation_progress", FieldLifecycle.EPHEMERAL, reset_value=None),
    # ── 3. SECURITY / TRUST ───────────────────────────────────────
    FieldSpec("security_status", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("security_reason", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("security_decision", FieldLifecycle.EPHEMERAL, reset_value=None),
    # (2026-09-08, clôture Bloc 1, mandat §6) — voir core/state.py.
    FieldSpec("security_degraded", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("security_degraded_reason", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("trust_score", FieldLifecycle.DURABLE),
    FieldSpec("requires_human", FieldLifecycle.EPHEMERAL, reset_value=False),
    # (2026-09-08, P1-4 audit architectural) — voir core/state.py.
    FieldSpec("blocked_user_query", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("error_message", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("technical_details", FieldLifecycle.EPHEMERAL, reset_value=None),
    # ── 4. INTERPRETER OUTPUT ─────────────────────────────────────
    FieldSpec("interpreted_event", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("unknown_reason", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("disambiguation_candidate", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("detected_intent", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("interpreter_confidence", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("validation_status", FieldLifecycle.EPHEMERAL, reset_value=None),
    # (2026-09-08, P1-3 audit architectural) — voir core/state.py.
    FieldSpec(
        "slot_enrichment_force_clarification",
        FieldLifecycle.EPHEMERAL,
        reset_value=None,
    ),
    FieldSpec("clarification_reasons", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec(
        "extracted_entities", FieldLifecycle.EPHEMERAL, reset_value={"__reset__": True}
    ),
    FieldSpec(
        "raw_analysis", FieldLifecycle.EPHEMERAL, reset_value={"__reset__": True}
    ),
    FieldSpec("intent_competition", FieldLifecycle.EPHEMERAL, reset_value=[]),
    FieldSpec(
        "cognitive_decision", FieldLifecycle.EPHEMERAL, reset_value={"__reset__": True}
    ),
    # ── 5. GOAL MANAGEMENT ────────────────────────────────────────
    FieldSpec("current_goal", FieldLifecycle.DURABLE),
    FieldSpec("last_terminated_goal", FieldLifecycle.DURABLE),
    FieldSpec("pending_goal", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("goal_stack", FieldLifecycle.DURABLE),
    FieldSpec("goal_status", FieldLifecycle.DURABLE),
    FieldSpec("current_plan_id", FieldLifecycle.DURABLE),
    FieldSpec("goal_metadata", FieldLifecycle.DURABLE),
    # ── 6. EXPECTATION ENGINE ─────────────────────────────────────
    # `expected_input` (ancien Literal[...]) retiré du registre (2026-09-02,
    # "no legacy shim") — plus dans MarketAgentState, donc plus DURABLE :
    # une purge Tier-3 (voir workspace/checkpointer.py) peut désormais le
    # dropper d'un vieux checkpoint sans risque (rien ne le relit, hors le
    # pont de transition isolé de get_pending_interaction()).
    # (2026-09-02) Source canonique — voir core/pending_interaction.py. DURABLE
    # pour la même raison que expected_input : doit survivre au tour suivant
    # tant que l'interaction (confirmation, champ, localisation...) n'est pas
    # résolue. `resolve_pending_interaction()`/`clear_pending_interaction()`
    # le remettent explicitement à None quand ce n'est plus le cas — jamais
    # laissé au hasard d'un reset générique de fin de tour.
    FieldSpec("pending_interaction", FieldLifecycle.DURABLE),
    # (2026-09-03) domain/procurement_draft.py::ProcurementDraft sérialisé —
    # DURABLE pour la même raison que pending_interaction : le draft d'un
    # appel d'offres en cours de construction/confirmation doit survivre au
    # tour suivant, minuscule (quelques scalaires), jamais candidat au
    # nettoyage même en dernier recours (Tier-3, workspace/checkpointer.py).
    FieldSpec("procurement_draft", FieldLifecycle.DURABLE),
    # (2026-09-03, migration PREORDER) — même raison que procurement_draft.
    FieldSpec("preorder_draft", FieldLifecycle.DURABLE),
    # (2026-09-08, P0-1 audit architectural) — même raison que
    # procurement_draft/preorder_draft. Voir core/state.py pour l'incident.
    FieldSpec("sales_publish_draft", FieldLifecycle.DURABLE),
    # (Phase 2 hardening, P1) : `recurring_need_draft` — voir `core/draft_registry.py`.
    # Manquait ici pendant plusieurs mois (audit 2026-09-24) : sans cette déclaration, le
    # shrink Tier-3 du checkpointer pouvait le supprimer silencieusement sous pression de
    # taille, perdant une transaction en cours sans message d'erreur. `test_draft_registry_
    # completeness.py` interdit désormais qu'un draft déclaré dans le registre manque ici.
    FieldSpec("recurring_need_draft", FieldLifecycle.DURABLE),
    # (2026-09-08, P1-4 audit architectural) : DURABLE — doit survivre au
    # tour SUIVANT pour empêcher une seconde tentative de création tant que
    # le même goal farm-critique n'est pas résolu. Nettoyé explicitement au
    # goal_completed (voir nodes/cleaner.py — même bloc que
    # transaction_payload/stable_entities), pas par un TTL générique.
    FieldSpec("farm_creation_attempted", FieldLifecycle.DURABLE),
    # `auto_farm_notice`/`error_creating_farm` : consommés UNE FOIS par
    # `rendering/success.py` dans le MÊME tour où `ensure_farm_node` les
    # produit — aucune raison de survivre au-delà, EPHEMERAL (comme
    # `execution_result`, reset par post_response_cleanup après rendu).
    FieldSpec("auto_farm_notice", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("error_creating_farm", FieldLifecycle.EPHEMERAL, reset_value=False),
    FieldSpec("last_agent_question", FieldLifecycle.DURABLE),
    FieldSpec("expected_candidates", FieldLifecycle.DURABLE),
    FieldSpec("last_missing_field", FieldLifecycle.DURABLE),
    # ── 7. WORKING MEMORY ─────────────────────────────────────────
    FieldSpec("working_memory", FieldLifecycle.DURABLE),
    FieldSpec("transaction_payload", FieldLifecycle.DURABLE),
    FieldSpec("draft_payload", FieldLifecycle.DURABLE),
    FieldSpec("stable_entities", FieldLifecycle.DURABLE),
    FieldSpec(
        "volatile_entities", FieldLifecycle.EPHEMERAL, reset_value={"__reset__": True}
    ),
    FieldSpec("available_mapping", FieldLifecycle.DURABLE),
    # (2026-09-09, audit ui_engine) — voir core/state.py.
    FieldSpec("menu_snapshot_id", FieldLifecycle.DURABLE),
    FieldSpec("pending_cleanup", FieldLifecycle.EPHEMERAL, reset_value=None),
    # ── 8. SLOT TRACKING ──────────────────────────────────────────
    FieldSpec("required_fields", FieldLifecycle.DERIVED, reset_value=[]),
    FieldSpec("missing_fields", FieldLifecycle.DERIVED, reset_value=[]),
    FieldSpec("completed_fields", FieldLifecycle.DERIVED, reset_value=[]),
    FieldSpec("validation_errors", FieldLifecycle.DERIVED, reset_value=[]),
    FieldSpec("warnings", FieldLifecycle.DERIVED, reset_value=[]),
    # ── 9. INTERRUPTIONS / MULTI-TASK ─────────────────────────────
    FieldSpec("interruption_detected", FieldLifecycle.EPHEMERAL, reset_value=False),
    FieldSpec("interruption_unresolved", FieldLifecycle.EPHEMERAL, reset_value=False),
    FieldSpec("interruption_type", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec(
        "interruption_payload",
        FieldLifecycle.EPHEMERAL,
        reset_value={"__reset__": True},
    ),
    FieldSpec("suspended_goal", FieldLifecycle.DURABLE),
    FieldSpec("suspended_payload", FieldLifecycle.DURABLE),
    # ── 10. CONFIRMATION / EXECUTION ──────────────────────────────
    # `waiting_for_confirmation` (ancien bool) retiré du registre — voir
    # `pending_interaction` ci-dessus.
    FieldSpec("is_certified", FieldLifecycle.EPHEMERAL, reset_value=False),
    FieldSpec("confirmation_summary", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("confirmation_summary_goal", FieldLifecycle.EPHEMERAL, reset_value=None),
    # `replace_value` (pas `merge_dict`) : contrairement aux champs
    # `_MERGE_DICT_RESET`, un simple `None` suffit à effacer ce champ (voir
    # agents/reducers.py::replace_value — seul le sentinel _KEEP préserve
    # l'ancienne valeur, `None` écrase réellement).
    FieldSpec("confirmation_summary_payload", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("execution_authorized", FieldLifecycle.EPHEMERAL, reset_value=False),
    FieldSpec(
        "execution_result", FieldLifecycle.EPHEMERAL, reset_value={"__reset__": True}
    ),
    # ── 11. MCP / TOOL EXECUTION ──────────────────────────────────
    FieldSpec("selected_tool", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec(
        "selected_tool_args", FieldLifecycle.EPHEMERAL, reset_value={"__reset__": True}
    ),
    FieldSpec("tool_execution_history", FieldLifecycle.DURABLE),
    FieldSpec("retry_count", FieldLifecycle.EPHEMERAL, reset_value=0),
    # ── 12. RESPONSE GENERATION ───────────────────────────────────
    # NOTE: These are consumed by final_response which runs AFTER
    # state_cleaner. They are reset by post_response_cleanup.
    FieldSpec("final_response", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("ag_ui_component", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("reply_audio_url", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("response_strategy", FieldLifecycle.EPHEMERAL, reset_value=None),
    FieldSpec("onboarding_prompt", FieldLifecycle.EPHEMERAL, reset_value=None),
    # ── 13. SYSTEM FLAGS ──────────────────────────────────────────
    FieldSpec("status", FieldLifecycle.DURABLE),
    FieldSpec("is_locked", FieldLifecycle.EPHEMERAL, reset_value=False),
    # (2026-09-08, P1-1) `should_replan` supprimé — voir core/state.py.
    FieldSpec("should_interrupt", FieldLifecycle.EPHEMERAL, reset_value=False),
    # ── 14. SLOT-FILLING (DRY form engine) ────────────────────────
    FieldSpec("active_form", FieldLifecycle.DURABLE),
    FieldSpec("form_data", FieldLifecycle.DURABLE),
    FieldSpec("form_step", FieldLifecycle.DURABLE),
    # ── 15. UI ENGINE ─────────────────────────────────────────────
    FieldSpec("pending_menu", FieldLifecycle.EPHEMERAL, reset_value=None),
    # ── BUYER CONTEXT ─────────────────────────────────────────────
    FieldSpec("active_cart", FieldLifecycle.DURABLE),
    FieldSpec("cart_meta", FieldLifecycle.DURABLE),
    FieldSpec("negotiation_context", FieldLifecycle.DURABLE),
    FieldSpec("preorder_workflow", FieldLifecycle.DURABLE),
    FieldSpec("fallback_recommendations", FieldLifecycle.EPHEMERAL, reset_value=[]),
    FieldSpec("last_order_summary", FieldLifecycle.DURABLE),
    FieldSpec("order_tracking_context", FieldLifecycle.DURABLE),
    FieldSpec("vendor_selection_context", FieldLifecycle.DURABLE),
    # (2026-08-30) Sans cette entrée, `tier_selection_context` — bien
    # déclaré comme channel LangGraph dans flows/buyer/state.py — était
    # quand même traité comme éphémère par LE CHECKPOINTER (registre
    # SÉPARÉ, voir workspace/checkpointer.py) et ne survivait PAS d'un tour
    # à l'autre : le menu de paliers se réaffichait indéfiniment, "2" ne
    # trouvant jamais aucun contexte actif au tour suivant. Même bug de
    # fond que la note plus haut sur `vendor_selection_context` — trois
    # registres distincts (Annotated reducer, BuyerContext, ce profil de
    # durabilité) doivent TOUS connaître un champ multi-tour pour qu'il
    # survive réellement en production.
    FieldSpec("tier_selection_context", FieldLifecycle.DURABLE),
    # ── PRODUCER CONTEXT ──────────────────────────────────────────
    FieldSpec("user_farms_cache", FieldLifecycle.DURABLE),
    FieldSpec("original_entity", FieldLifecycle.DURABLE),
    FieldSpec("current_entity", FieldLifecycle.DURABLE),
)


# =====================================================================
# DERIVED LOOKUP TABLES — built once at import time
# =====================================================================

_BY_NAME: Dict[str, FieldSpec] = {f.name: f for f in _FIELDS}

_EPHEMERAL_FIELDS: FrozenSet[str] = frozenset(
    f.name for f in _FIELDS if f.lifecycle == FieldLifecycle.EPHEMERAL
)
_DERIVED_FIELDS: FrozenSet[str] = frozenset(
    f.name for f in _FIELDS if f.lifecycle == FieldLifecycle.DERIVED
)
_DURABLE_FIELDS: FrozenSet[str] = frozenset(
    f.name for f in _FIELDS if f.lifecycle == FieldLifecycle.DURABLE
)

# Fields safe to reset BEFORE final_response (state_cleaner phase).
# Excludes response-generation fields that final_response still needs.
_RESPONSE_FIELDS: FrozenSet[str] = frozenset(
    {
        "final_response",
        "ag_ui_component",
        "reply_audio_url",
        "response_strategy",
        "onboarding_prompt",
        "status",
    }
)

CLEANABLE_BEFORE_RESPONSE: FrozenSet[str] = (
    _EPHEMERAL_FIELDS | _DERIVED_FIELDS
) - _RESPONSE_FIELDS

CLEANABLE_AFTER_RESPONSE: FrozenSet[str] = _RESPONSE_FIELDS & _EPHEMERAL_FIELDS


# =====================================================================
# PUBLIC API
# =====================================================================


def get_field_spec(name: str) -> Optional[FieldSpec]:
    return _BY_NAME.get(name)


def is_ephemeral(name: str) -> bool:
    return name in _EPHEMERAL_FIELDS


def is_durable(name: str) -> bool:
    return name in _DURABLE_FIELDS


def build_reset_patch(
    field_names: FrozenSet[str],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Build a state patch that resets the given fields to their default values.

    Only includes fields that are actually present in state and differ from
    their reset_value, to avoid unnecessary checkpoint churn.
    """
    patch: Dict[str, Any] = {}
    for name in field_names:
        spec = _BY_NAME.get(name)
        if spec is None:
            continue
        current = state.get(name)
        if current is None and spec.reset_value is None:
            continue
        if current == spec.reset_value:
            continue
        if spec.reset_value is not None:
            patch[name] = spec.reset_value
        else:
            patch[name] = None
    return patch


__all__ = [
    "FieldLifecycle",
    "FieldSpec",
    "get_field_spec",
    "is_ephemeral",
    "is_durable",
    "build_reset_patch",
    "CLEANABLE_BEFORE_RESPONSE",
    "CLEANABLE_AFTER_RESPONSE",
    "_EPHEMERAL_FIELDS",
    "_DERIVED_FIELDS",
    "_DURABLE_FIELDS",
]
