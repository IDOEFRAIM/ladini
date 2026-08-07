"""Market — DRY Form Node.

Single LangGraph node that handles ALL conversational form collection
for the MarketCoach agent (product creation, auction creation, etc.).

It detects `active_form` in state, delegates to the shared form engine,
and returns a state patch. When the form completes, it maps the collected
data back into `transaction_payload` and sets `current_goal` so the
existing confirmation_gate → mcp_tool_executor pipeline can proceed.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from agriconnect.agents.forms import (
    FormSpec,
    SlotSpec,
    FORM_REGISTRY,
    run_form_step,
    _QUANTITY_SLOT_NAMES,
)

logger = logging.getLogger("AgriConnect.Market.FormNode")

# Maps form_id → current_goal to set on completion
_FORM_TO_GOAL: Dict[str, str] = {
    "PRODUCT_CREATE": "SALES_PUBLISH_PRODUCT",
    "AUCTION_CREATE": "PROCUREMENT_CREATE_REQUEST",
}

# Reverse: goal → form_id (for auto-activation from goal_planner)
_GOAL_TO_FORM: Dict[str, str] = {v: k for k, v in _FORM_TO_GOAL.items()}


# Indices lexicaux par champ (l'utilisateur ne prononce presque jamais le nom
# technique du slot — "ça coûte 49750" ne contient ni "price" ni le label
# exact "Prix de départ minimum"). On mappe des mots de SENS vers le slot visé.
# Ces indices permettent AUSSI de reconnaître PLUSIEURS champs dans un seul
# message ("prix 49750 date 30 juillet") pour appliquer les deux corrections.
_CUES_BY_SLOT: Dict[str, tuple[str, ...]] = {
    "price": ("coute", "coûte", "cout", "coût", "vaut", "prix", "fcfa", "franc", "cfa"),
    "quantity": ("kg", "kilo", "kilos", "tonne", "tonnes", "sac", "sacs",
                 "tete", "tête", "tetes", "têtes", "unite", "unité", "panier",
                 "paniers", "quantite", "quantité", "combien"),
    "deadline": ("date", "delai", "délai", "echeance", "échéance", "limite",
                 "jusqu", "jour", "jours", "semaine", "semaines", "mois"),
    "product": ("produit", "article", "culture", "denree", "denrée"),
}


def _infer_slots_from_text(user_text: str, spec: FormSpec) -> list[SlotSpec]:
    """Tous les slots qu'un message de correction vise, pas juste le premier.

    Une correction peut nommer PLUSIEURS champs à la fois ("prix 49750 date 30
    juillet") : renvoyer un seul slot faisait perdre la 2ᵉ correction. On
    reconnaît d'abord les noms/labels/aliases littéraux, puis on complète avec
    les indices de sens par slot (`_CUES_BY_SLOT`).
    """
    if not user_text:
        return []
    lowered = user_text.lower()
    found: list[SlotSpec] = []
    seen: set[str] = set()

    def _add(slot: Optional[SlotSpec]) -> None:
        if slot is not None and slot.name not in seen:
            seen.add(slot.name)
            found.append(slot)

    # 1. Nom / label / alias littéral.
    for slot in spec.slots:
        candidates = [slot.name.replace("_", " "), slot.label or "", *(slot.aliases or [])]
        for candidate in candidates:
            candidate_norm = str(candidate or "").lower().strip()
            if candidate_norm and candidate_norm in lowered:
                _add(slot)
                break

    # 2. Indices de sens par slot.
    for slot in spec.slots:
        cues = _CUES_BY_SLOT.get(slot.name)
        if cues and any(cue in lowered for cue in cues):
            _add(slot)

    return found


async def form_node(state: Dict[str, Any], mc_runtime: Any) -> Dict[str, Any]:
    """LangGraph node: drives the DRY form engine for MarketCoach."""
    form_id = state.get("active_form")

    # Guard: if the form already reached COMPLETE we must not re-run it.
    if state.get("form_step") == "COMPLETE" and form_id is None:
        logger.debug("[FormNode] Form already complete, skipping re-entry.")
        return {}

    # Auto-activate form from current_goal if not yet set
    if not form_id:
        goal = state.get("current_goal")
        form_id = _GOAL_TO_FORM.get(goal) if goal else None
        if form_id:
            logger.info("[FormNode] Auto-activating form=%s from goal=%s", form_id, goal)
        else:
            logger.debug("[FormNode] No active form — pass-through.")
            return {}

    spec = FORM_REGISTRY.get(form_id)
    if spec is None:
        logger.warning("[FormNode] Unknown form_id=%s — clearing.", form_id)
        return {"active_form": None, "form_step": None}

    # Merge extracted_entities as the new user input for this turn
    extracted = dict(state.get("extracted_entities") or {})
    # Also check normalized_text for direct value injection
    norm = (state.get("normalized_text") or state.get("user_query") or "").strip()
    current_step = state.get("form_step")

    # On first activation, pre-fill from any payload carried into this tunnel
    # (e.g. entities stashed by semantic_disambiguation or a resumed goal) so the
    # form skips slots the user already provided instead of re-asking them.
    if not current_step:
        carried = state.get("transaction_payload") or {}
        if isinstance(carried, dict):
            for key, value in carried.items():
                if value in (None, "", [], {}):
                    continue
                extracted.setdefault(key, value)

    # If user is confirming (the form asked for confirmation last turn)
    event = str(state.get("interpreted_event") or "").upper()

    # ── STALE-FORM GUARD ────────────────────────────────────────────
    # A fresh task or interruption while a form is mid-collection or awaiting
    # confirmation means the user is NOT answering the form — they are
    # (re)starting something. This is THE origin of the auction-creation
    # cascade bug: a `CONFIRMING` form persisted (via the checkpointer) from a
    # previous WhatsApp session, so a brand-new "je veux créer une enchère"
    # landed in the correction branch below ("Pour corriger un champ...")
    # instead of starting a clean auction. Never let a stale form hijack a
    # fresh intent: reset it here.
    if current_step and current_step != "COMPLETE" and event in {"NEW_TASK", "INTERRUPTION"}:
        goal_now = str(state.get("current_goal") or "").upper()
        target_form = _GOAL_TO_FORM.get(goal_now)
        if target_form:
            # New intent is itself a form goal → restart that form from a clean
            # slate, seeded only with THIS turn's freshly extracted entities.
            logger.info(
                "[FormNode] %s during form_step=%s — resetting stale form, "
                "restarting '%s' fresh.", event, current_step, target_form,
            )
            fresh_spec = FORM_REGISTRY.get(target_form) or spec
            fresh_state = dict(state)
            fresh_state["form_step"] = None
            fresh_state["form_data"] = {}
            fresh_extracted = dict(state.get("extracted_entities") or {})
            result = run_form_step(fresh_spec, fresh_state, fresh_extracted)
            patch = dict(result.patch or {})
            # merge_dict-safe reset: a plain form_data patch would MERGE onto
            # the stale one (reducers.py) and resurrect old slots — use the
            # reset+populate sentinel so the old form_data is truly cleared.
            fresh_fd = patch.get("form_data") or {}
            patch["form_data"] = {"__reset__": True, **fresh_fd}
            patch.setdefault("transaction_payload", {"__reset__": True})
            patch.setdefault("active_form", target_form)
            patch.setdefault("current_goal", _FORM_TO_GOAL.get(target_form))
            patch.setdefault("goal_status", "ACTIVE")
            patch.setdefault("ag_ui_component", None)
            return patch
        # New intent is NOT a form goal → abandon the stale form entirely and
        # let the rest of the pipeline route the new goal normally.
        logger.info(
            "[FormNode] %s during form_step=%s — abandoning stale form "
            "(new goal '%s' is not a form).", event, current_step, goal_now,
        )
        return {
            "active_form": None,
            "form_step": None,
            "form_data": {"__reset__": True},
        }

    if current_step == "CONFIRMING":
        if event == "CONFIRM":
            # User confirmed → complete the form
            form_data = dict(state.get("form_data") or {})
            goal = _FORM_TO_GOAL.get(form_id)
            logger.info("[FormNode] Form %s confirmed → goal=%s", form_id, goal)
            return {
                "active_form": None,
                "form_step": "COMPLETE",
                "form_data": form_data,
                "transaction_payload": form_data,
                "current_goal": goal,
                "goal_status": "ACTIVE",
                "status": "WAITING_CONFIRMATION",
                "waiting_for_confirmation": True,
                "execution_authorized": False,
                "missing_fields": [],
            }
        elif event == "REJECT" and not _infer_slots_from_text(norm, spec):
            # "non" / "annule" SEUL (aucun champ identifiable dans le texte) :
            # une vraie annulation, comportement inchangé. Si le refus porte
            # une correction explicite ("non c'est 200 tonnes", "non je vends
            # X au prix de Y"), on tombe dans la branche de correction
            # ci-dessous au lieu d'annuler tout le brouillon — même principe
            # que le fix appliqué à `nodes/memory.py` pour le chemin non-form
            # (un REJECT accompagné d'une valeur réelle n'est pas un "non"
            # sans contenu, c'est une correction).
            logger.info("[FormNode] Form %s cancelled by user.", form_id)
            return {
                "active_form": None,
                "form_step": None,
                "form_data": {"__reset__": True},
                "current_goal": None,
                "goal_status": "COMPLETED",
                "status": "COMPLETED",
                "response_strategy": "CLARIFICATION",
                "final_response": "Formulaire annulé. Que souhaitez-vous faire ?",
                "ag_ui_component": None,
            }
        else:
            # User wants to correct one or more fields instead of confirming
            # (y compris un REJECT porteur d'une correction — voir ci-dessus).
            correction_payload = dict(extracted)
            inferred_slots = _infer_slots_from_text(norm, spec)

            if not inferred_slots:
                # Aucun champ identifiable (ni nom, ni indice de sens). On ne
                # devine pas à l'aveugle — on guide l'utilisateur.
                return {
                    "status": "WAITING_INPUT",
                    "response_strategy": "ASK_MISSING_FIELD",
                    "final_response": (
                        "Pour corriger un champ, tapez par exemple « prix 480000 » ou "
                        "« date 30 juillet ». Sinon répondez « oui » pour confirmer ou « annuler » pour quitter."
                    ),
                    "ag_ui_component": None,
                }

            correction_state = dict(state)
            correction_state["form_step"] = None
            correction_state["status"] = "WAITING_INPUT"
            correction_state["waiting_for_confirmation"] = False
            correction_state["ag_ui_component"] = None
            correction_state.pop("confirmation_summary", None)
            correction_state.pop("last_agent_question", None)
            correction_state.pop("final_response", None)

            correction_data = dict(correction_state.get("form_data") or {})
            correction_state["form_data"] = correction_data

            # Narrowing STRICT aux seuls champs nommés/visés ce tour-ci.
            # Sans ça, `run_form_step` absorbe TOUT ce que l'interpréteur a
            # extrait (forms.py ne filtre par étape que si `form_step` est
            # non-nul, or on le force à None ici) : une correction de prix
            # seul ("ça coûte 49750") voyait ce nombre écraser AUSSI la
            # quantité si l'interpréteur avait (mal) rempli les deux champs.
            allowed_keys: set[str] = set()
            for slot in inferred_slots:
                correction_data.pop(slot.name, None)  # invalide l'ancienne valeur
                allowed_keys.add(slot.name)
                allowed_keys.update(slot.aliases)
                # Le slot "quantité" est presque toujours reformulé avec son
                # unité dans LA MÊME correction ("non c'est 200 tonnes") —
                # l'interpréteur extrait légitimement `unit`/`unit_mentioned`
                # en même temps que `quantity`. Sans cette exception, ce
                # narrowing (pensé pour isoler une correction de prix seul de
                # la quantité) jetait aussi l'unité fraîchement corrigée,
                # laissant l'ANCIENNE unité (potentiellement fausse) en place
                # à côté du nouveau nombre — même bug que dans
                # `agents/forms.py::run_form_step`, ici côté correction.
                if slot.name in _QUANTITY_SLOT_NAMES:
                    allowed_keys.update({"unit", "unit_mentioned"})
                # Si l'interpréteur n'a produit AUCUNE valeur pour ce slot
                # (typiquement `deadline`, absent de son schéma JSON), on lui
                # injecte le texte brut : la coercition du slot (ex.
                # _coerce_deadline, _coerce_number) en extraira la sous-partie
                # pertinente ("prix 49750 date 30 juillet" → price=49750 via
                # _coerce_number sur le 1er nombre, deadline=2026-07-30 via
                # _coerce_deadline sur "30 juillet").
                if correction_payload.get(slot.name) in (None, "", [], {}) and norm:
                    correction_payload[slot.name] = norm

            correction_payload = {
                k: v for k, v in correction_payload.items() if k in allowed_keys
            }

            result = run_form_step(spec, correction_state, correction_payload)
            patch = dict(result.patch or {})
            if patch:
                patch.setdefault("ag_ui_component", None)
                return patch

            return {
                "status": "WAITING_INPUT",
                "response_strategy": "ASK_MISSING_FIELD",
                "final_response": (
                    "Indiquez la valeur à corriger (ex: « quantité 20 tonnes ») ou tapez « annuler »."
                ),
                "ag_ui_component": None,
            }

    # If we have a specific form_step and normalized text,
    # inject it as the expected slot value
    if current_step and current_step not in ("CONFIRMING", "COMPLETE") and norm:
        if current_step not in extracted:
            extracted[current_step] = norm

    result = run_form_step(spec, state, extracted)

    patch = dict(result.patch or {})
    # Ensure the goal stays locked while the form is collecting slots (belt & suspenders).
    if not result.is_complete and patch.get("form_step") not in (None, "COMPLETE"):
        goal_locked = _FORM_TO_GOAL.get(form_id)
        if goal_locked:
            patch.setdefault("current_goal", goal_locked)
            patch.setdefault("goal_status", "ACTIVE")

    if result.is_complete or patch.get("form_step") == "COMPLETE":
        form_data = dict(patch.get("form_data") or state.get("form_data") or {})
        goal = _FORM_TO_GOAL.get(form_id)
        logger.info("[FormNode] Form %s complete → goal=%s", form_id, goal)
        patch.update({
            "active_form": None,
            "form_step": "COMPLETE",
            "form_data": form_data,
            "transaction_payload": form_data,
            "current_goal": goal,
            "goal_status": "ACTIVE",
            "status": "WAITING_CONFIRMATION",
            "waiting_for_confirmation": True,
            "execution_authorized": False,
            "missing_fields": [],
            "last_missing_field": None,
            "expected_input": "NONE",
            "response_strategy": "CONFIRMATION",
            "final_response": state.get("confirmation_summary") or patch.get("confirmation_summary"),
            "ag_ui_component": None,
        })

    return patch
