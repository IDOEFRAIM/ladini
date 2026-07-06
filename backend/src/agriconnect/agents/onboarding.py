
from __future__ import annotations

"""Unified onboarding state machine for all AgriConnect agents.

Eliminates the duplicated step-by-step user registration logic.
Both Market and Formation agents delegate here for new user flows.

Steps:
    COLLECT_ROLE → COLLECT_NAME → COLLECT_ZONE → CONFIRM_DETAILS → CREATE_PROFILE → DONE
"""

import ast
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional
import asyncio

logger = logging.getLogger("AgriConnect.Agents.Onboarding")

_SLOT_LABELS = {
    "role": "si vous êtes acheteur ou producteur",
    "name": "votre nom complet",
    "zone": "la zone où vous travaillez",
}



class OnboardingStep(str, Enum):
    COLLECT_ROLE = "COLLECT_ROLE"
    COLLECT_NAME = "COLLECT_NAME"
    COLLECT_ZONE = "COLLECT_ZONE"
    CONFIRM_DETAILS = "CONFIRM_DETAILS"
    CREATE_PROFILE = "CREATE_PROFILE"
    DONE = "DONE"


@dataclass
class OnboardingState:
    step: OnboardingStep = OnboardingStep.COLLECT_ROLE
    phone: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None
    zone_name: Optional[str] = None
    zone_id: Optional[str] = None
    error: Optional[str] = None
    prompt: str = ""
    completed: bool = False
    created_profile: Dict[str, Any] = field(default_factory=dict)
    _filled_slots: Dict[str, bool] = field(default_factory=dict, repr=False)

    def missing_slots(self) -> List[str]:
        missing: List[str] = []
        if not self.role:
            missing.append("role")
        if not self.name:
            missing.append("name")
        if not self.zone_id and not self.zone_name:
            missing.append("zone")
        return missing


@dataclass
class OnboardingResult:
    state: OnboardingState
    response_text: str = ""
    ag_ui_component: Optional[Dict[str, Any]] = None
    status: str = "WAITING_INPUT"

    def to_state_updates(self) -> Dict[str, Any]:
        updates: Dict[str, Any] = {
            "onboarding_step": self.state.step.value,
            "onboarding_prompt": self.response_text,
            "status": self.status,
            "ag_ui_component": self.ag_ui_component,
            "user_role": self.state.role,
            "user_name": self.state.name,
            "zone_name": self.state.zone_name,
            "zone_id": self.state.zone_id,
        }
        if self.state.completed:
            updates["is_onboarding"] = False
            updates["onboarding_step"] = None
            updates["user_phone"] = self.state.phone
            updates["user_name"] = self.state.name
            updates["user_role"] = self.state.role
            updates["zone_name"] = self.state.zone_name
            updates["zone_id"] = self.state.zone_id
            updates["user_context_loaded"] = True
            if self.state.created_profile:
                updates["user_id"] = str(self.state.created_profile.get("id", ""))
        return updates


LLMExtractor = Callable[[str, str], Coroutine[Any, Any, Optional[str]]]


def _is_valid(value: Any) -> bool:
    return value not in (None, "", [], {})


def _normalize_str(value: str) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    without_diacritics = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return without_diacritics.lower().strip()


def _normalize_role(value: Optional[str]) -> Optional[str]:
    """Valide et normalise les sorties de l'extracteur LLM (BUYER|PRODUCER)."""
    if not value:
        return None

    norm_val = str(value).strip().upper()
    if norm_val in {"BUYER", "PRODUCER"}:
        return norm_val

    return None


async def _prefill_onboarding_slots(
    ob_state: OnboardingState,
    text: str,
    entities: Dict[str, Any],
    llm_extract: Optional[LLMExtractor],
) -> None:
    """Alimente chaque slot manquant avec les infos déjà données (ordre libre)."""

    cleaned_text = (text or "").strip()

    normalized = _normalize_role(entities.get("role"))
    if not normalized and cleaned_text and llm_extract:
        try:
            extracted = await llm_extract("role", cleaned_text)
            normalized = _normalize_role(extracted)
        except Exception:
            normalized = None
    collecting = ob_state.step in {
        OnboardingStep.COLLECT_ROLE,
        OnboardingStep.COLLECT_NAME,
        OnboardingStep.COLLECT_ZONE,
    }
    if normalized and normalized != (ob_state.role or "") and ob_state.step != OnboardingStep.CONFIRM_DETAILS:
        ob_state.role = normalized
        ob_state._filled_slots["role"] = True

    candidate = entities.get("name")
    if not candidate and cleaned_text and llm_extract:
        try:
            candidate = await llm_extract("name", cleaned_text)
        except Exception:
            candidate = None
    if candidate and _is_valid(candidate):
        normalized_name = str(candidate).strip()
        if normalized_name and normalized_name != (ob_state.name or "") and ob_state.step != OnboardingStep.CONFIRM_DETAILS:
            ob_state.name = normalized_name
            ob_state._filled_slots["name"] = True

    zone_candidate = (
        entities.get("zone_name")
        or entities.get("location")
        or entities.get("zone")
    )
    if not zone_candidate and cleaned_text and llm_extract:
        try:
            zone_candidate = await llm_extract("zone", cleaned_text)
        except Exception:
            zone_candidate = None
    if zone_candidate:
        normalized_zone = str(zone_candidate).strip()
        if normalized_zone and normalized_zone != (ob_state.zone_name or "") and ob_state.step != OnboardingStep.CONFIRM_DETAILS:
            ob_state.zone_name = normalized_zone
            ob_state.zone_id = None
            ob_state._filled_slots["zone"] = True


def _determine_next_step(ob_state: OnboardingState) -> OnboardingStep:
    if not ob_state.role:
        return OnboardingStep.COLLECT_ROLE
    if not ob_state.name:
        return OnboardingStep.COLLECT_NAME
    if not ob_state.zone_id:
        return OnboardingStep.COLLECT_ZONE
    if ob_state.step == OnboardingStep.CREATE_PROFILE:
        return OnboardingStep.CREATE_PROFILE
    if ob_state.step == OnboardingStep.DONE:
        return OnboardingStep.DONE
    return OnboardingStep.CONFIRM_DETAILS


def _role_mapping_ack(role: str) -> str:
    if role == "BUYER":
        return (
            "Compris. Les commerçants, grossistes ou acheteurs qui cherchent des produits "
            "sont enregistrés comme acheteurs pour accéder aux offres disponibles."
        )
    return (
        "D'accord, les agriculteurs, éleveurs ou vendeurs d'engrais/intrants sont traités "
        "comme fournisseurs/producteurs afin de proposer leurs produits aux clients."
    )


def _role_capabilities_message(role: Optional[str]) -> str:
    role_norm = str(role or "").upper()
    if role_norm == "BUYER":
        return (
            "Je peux t’aider à trouver des produits par zone, comparer les producteurs, "
            "passer des commandes ou précommandes et suivre leur statut. Dis-moi ce que tu recherches."
        )
    if role_norm == "PRODUCER":
        return (
            "Je peux t’aider à publier tes offres, gérer ton stock, contacter des acheteurs "
            "ou lancer des appels d’offres. Quel produit veux-tu mettre en avant ?"
        )
    return (
        "Je suis prêt à t’accompagner pour acheter ou vendre sur AgriConnect : précise-moi ton besoin et on démarre."
    )


def _is_positive_ack(text: str) -> bool:
    norm = _normalize_str(text)
    return norm in {"oui", "yes", "ok", "okay", "daccord", "dac", "valide", "cestbon"}




def _unwrap_tool_payload(raw: Any) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        for key in ("data", "result", "payload", "response"):
            value = raw.get(key)
            if value is not None and value is not raw:
                nested = _unwrap_tool_payload(value)
                if nested:
                    return nested
        raw_result = raw.get("raw_result")
        if raw_result is not None:
            nested = _unwrap_tool_payload(raw_result)
            if nested:
                return nested
        for key in ("zones", "items", "results"):
            collection = raw.get(key)
            if isinstance(collection, list):
                nested = _unwrap_tool_payload(collection)
                if nested:
                    return nested
        return raw
    if isinstance(raw, list):
        for item in raw:
            nested = _unwrap_tool_payload(item)
            if nested:
                return nested
        return None
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        if stripped[0] in "[{":
            try:
                return _unwrap_tool_payload(json.loads(stripped))
            except Exception:
                try:
                    return _unwrap_tool_payload(ast.literal_eval(stripped))
                except Exception:
                    return None
        return None
    return None


def _extract_zone_labels(raw: Any) -> List[str]:
    labels: List[str] = []
    def _handle_entry(entry: Any) -> None:
        if isinstance(entry, dict):
            label = entry.get("label") or entry.get("name")
        else:
            label = str(entry).strip()
        if label:
            labels.append(str(label).strip())

    def _walk(obj: Any) -> None:
        if isinstance(obj, list):
            for item in obj:
                _walk(item)
            return
        if isinstance(obj, dict):
            for key in ("data", "zones", "items", "results"):
                val = obj.get(key)
                if isinstance(val, list):
                    _walk(val)
                    return
            _handle_entry(obj)
            return
        if isinstance(obj, str):
            stripped = obj.strip()
            if stripped.startswith(("[", "{")):
                try:
                    parsed = json.loads(stripped)
                    _walk(parsed)
                    return
                except Exception:
                    pass
                try:
                    parsed = ast.literal_eval(stripped)
                    _walk(parsed)
                    return
                except Exception:
                    pass
            parts = [p.strip() for p in stripped.split(",") if p.strip()]
            if len(parts) > 1:
                for part in parts:
                    _handle_entry(part)
            else:
                _handle_entry(stripped)
            return
        _handle_entry(obj)

    _walk(raw)
    return list(dict.fromkeys(labels))

async def run_onboarding_step(
    ob_state: OnboardingState,
    user_text: str,
    mcp_runtime: Any,
    *,
    extracted_entities: Optional[Dict[str, Any]] = None,
    llm_extract: Optional[LLMExtractor] = None,
) -> OnboardingResult:

    entities = extracted_entities or {}
    text = (user_text or "").strip()

    if not isinstance(ob_state.step, OnboardingStep):
        ob_state.step = OnboardingStep.COLLECT_ROLE

    ob_state._filled_slots = {}
    if ob_state.step != OnboardingStep.CONFIRM_DETAILS:
        await _prefill_onboarding_slots(ob_state, text, entities, llm_extract)

    if ob_state.step not in {OnboardingStep.CREATE_PROFILE, OnboardingStep.DONE}:
        ob_state.step = _determine_next_step(ob_state)

    # 3. Routage vers l'étape active
    if ob_state.step == OnboardingStep.COLLECT_ROLE:
        return await _step_collect_role(ob_state, text, entities, llm_extract)  


    if ob_state.step == OnboardingStep.COLLECT_NAME:
        return await _step_collect_name(ob_state, text, entities, llm_extract)
    
    if ob_state.step == OnboardingStep.COLLECT_ZONE:
        return await _step_collect_zone(ob_state, text, entities, mcp_runtime, llm_extract)
    
    if ob_state.step == OnboardingStep.CONFIRM_DETAILS:
        return await _step_confirm_details(ob_state, text, mcp_runtime, llm_extract)
    
    if ob_state.step == OnboardingStep.CREATE_PROFILE:
        return await _step_create_profile(ob_state, mcp_runtime)

    # 4. Fallback de sécurité
    ob_state.step = OnboardingStep.COLLECT_ROLE
    return OnboardingResult(
        state=ob_state,
        response_text="Pour commencer, êtes-vous acheteur (BUYER) ou producteur (PRODUCER) ?",
    )


def _acknowledge_recent_slots(ob_state: OnboardingState) -> str:
    parts: List[str] = []
    if ob_state._filled_slots.get("name") and ob_state.name:
        parts.append(f"Enchanté {ob_state.name} !")
    if ob_state._filled_slots.get("role") and ob_state.role:
        role_label = "acheteur" if ob_state.role == "BUYER" else "producteur"
        parts.append(f"Compris, vous êtes {role_label}.")
    if ob_state._filled_slots.get("zone") and ob_state.zone_name:
        parts.append(f"Zone notée : {ob_state.zone_name}.")
    return " ".join(parts)


def _format_missing_prompt(ob_state: OnboardingState) -> str:
    missing = [
        _SLOT_LABELS[key]
        for key in ob_state.missing_slots()
        if key in _SLOT_LABELS
    ]
    if not missing:
        return "Merci ! J'ai toutes les informations nécessaires."
    if len(missing) == 1:
        return (
            f"Il me manque encore {missing[0]}. "
            "Vous pouvez me la donner dans l'ordre qui vous arrange."
        )
    joined = ", ".join(missing[:-1]) + f" et {missing[-1]}"
    return (
        f"Il me manque encore {joined}. "
        "Donnez-moi ce qui manque dans l'ordre qui vous convient."
    )


def _build_guidance(ob_state: OnboardingState, extra_hint: str = "") -> str:
    ack = _acknowledge_recent_slots(ob_state)
    prompt = _format_missing_prompt(ob_state)
    guidance = " ".join(part for part in (ack, prompt, extra_hint) if part).strip()
    return guidance or prompt or ack or extra_hint


async def _resolve_zone_id(ob_state: OnboardingState, mcp_runtime: Any) -> bool:
    if not ob_state.zone_name or ob_state.zone_id or mcp_runtime is None:
        return bool(ob_state.zone_id)

    try:
        zone_response = await mcp_runtime.call_db("get_zone_by_name", name=ob_state.zone_name)
        payload = _unwrap_tool_payload(zone_response)

        if isinstance(payload, dict):
            zone_id = payload.get("zone_id") or payload.get("id")
            zone_label = payload.get("zone_name") or payload.get("name") or payload.get("label")

            if zone_id:
                ob_state.zone_id = str(zone_id)
            if zone_label:
                ob_state.zone_name = str(zone_label)
    except Exception:
        return False

    return bool(ob_state.zone_id)


async def _build_zone_catalog_hint(mcp_runtime: Any) -> str:
    if mcp_runtime is None:
        return ""

    try:
        zone_catalog = await mcp_runtime.call_db("get_available_zones") or []
        payload = _unwrap_tool_payload(zone_catalog) or zone_catalog
        labels = _extract_zone_labels(payload)
        if labels:
            sample = ", ".join(labels[:10])
            return f" Zones valides : {sample}."
    except Exception:
        return ""

    return ""


def _confirmation_component() -> Dict[str, Any]:
    return {
        "type": "ChoiceComponent",
        "props": {
            "title": "Confirmez vos informations",
            "options": [
                {"label": "Oui, tout est correct", "value": "OUI"},
                {"label": "Non, corriger", "value": "NON"},
            ],
        },
    }


async def _step_collect_role(
    ob_state: OnboardingState,
    text: str,
    entities: Dict[str, Any],
    llm_extract: Optional[LLMExtractor],
) -> OnboardingResult:
    role_hint = (
        "Pour votre rôle, dites simplement 'acheteur' ou 'producteur'."
        if not ob_state.role
        else ""
    )

    response_text = _build_guidance(ob_state, role_hint)
    return OnboardingResult(state=ob_state, response_text=response_text)



async def _step_collect_name(
    ob_state: OnboardingState,
    text: str,
    entities: Dict[str, Any],
    llm_extract: Optional[LLMExtractor],
) -> OnboardingResult:
    name_hint = (
        "Indiquez votre nom complet (ex: Jean Kaboré)."
        if not ob_state.name
        else ""
    )
    response = _build_guidance(ob_state, name_hint)

    ag_component = None
    if not ob_state.name:
        ag_component = {
            "type": "FormInputComponent",
            "props": {
                "title": "Inscription AgriConnect",
                "fields": [
                    {"name": "name", "label": "Votre nom complet", "type": "text", "required": True},
                ],
            },
        }
        if not response:
            response = "Quel est votre nom complet ?"

    return OnboardingResult(state=ob_state, response_text=response, ag_ui_component=ag_component)


async def _step_collect_zone(
    ob_state: OnboardingState,
    text: str,
    entities: Dict[str, Any],
    mcp_runtime: Any,
    llm_extract: Optional[LLMExtractor],
) -> OnboardingResult:
    zone_name = ob_state.zone_name

    if not zone_name:
        zone_hint = "Pouvez-vous indiquer une ville ou province où vous travaillez ?"
        response_text = _build_guidance(ob_state, zone_hint)
        return OnboardingResult(
            state=ob_state,
            response_text=response_text or zone_hint,
        )

    if zone_name:
        ob_state.zone_name = str(zone_name).strip()
        ob_state.zone_id = None

    await _resolve_zone_id(ob_state, mcp_runtime)

    if not ob_state.zone_id:
        hint = await _build_zone_catalog_hint(mcp_runtime)

        response_text = (
            f"Je n'ai pas trouvé la zone '{ob_state.zone_name or text}'. "
            "Merci d'indiquer une ville ou province valide."
        )
        if hint:
            response_text += hint
        response_text = _build_guidance(ob_state, response_text)
        return OnboardingResult(
            state=ob_state,
            response_text=response_text,
        )
    
    if ob_state.name and ob_state.phone and ob_state.zone_id and ob_state.role:
        ob_state.step = OnboardingStep.CONFIRM_DETAILS
        summary = _build_confirmation_prompt(ob_state)
        return OnboardingResult(
            state=ob_state,
            response_text=summary,
            ag_ui_component=_confirmation_component(),
        )

    zone_hint = "Indiquez une ville ou province où vous opérez."
    response_text = _build_guidance(ob_state, zone_hint)
    return OnboardingResult(
        state=ob_state,
        response_text=response_text or zone_hint,
    )


def _build_confirmation_prompt(ob_state: OnboardingState) -> str:
    name = ob_state.name or "(non renseigné)"
    role = ob_state.role or "(non renseigné)"
    role_label = "Acheteur" if role == "BUYER" else "Producteur" if role == "PRODUCER" else role
    zone = ob_state.zone_name or "(non renseignée)"
    return (
        f"Merci {name}! Récapitulatif : Nom={name}, Rôle={role_label}, Zone={zone}. "
        "Confirmez-vous ces informations ? (oui/non)"
    )


async def _step_confirm_details(
    ob_state: OnboardingState,
    text: str,
    mcp_runtime: Any,
    llm_extract: Optional[LLMExtractor],
) -> OnboardingResult:
    summary = _build_confirmation_prompt(ob_state)

    if text and llm_extract and (ob_state.prompt or "") == "CORRECTION_FIELD":
        try:
            field = await llm_extract("correction_field", text)
        except Exception:
            field = None
        field_norm = str(field or "").strip().upper()
        ob_state.prompt = ""

        if field_norm == "ROLE":
            ob_state.step = OnboardingStep.COLLECT_ROLE
            ob_state.role = None
            return OnboardingResult(state=ob_state, response_text="D'accord. Êtes-vous acheteur (BUYER) ou producteur (PRODUCER) ?")
        if field_norm == "NAME":
            ob_state.step = OnboardingStep.COLLECT_NAME
            ob_state.name = None
            return OnboardingResult(state=ob_state, response_text="Quel est votre nom complet ?")
        if field_norm == "ZONE":
            ob_state.step = OnboardingStep.COLLECT_ZONE
            ob_state.zone_id = None
            ob_state.zone_name = None
            return OnboardingResult(state=ob_state, response_text="Dans quelle zone (ville ou province) opérez-vous ?")

        ob_state.step = OnboardingStep.CONFIRM_DETAILS
        ob_state.prompt = "CORRECTION_FIELD"
        return OnboardingResult(
            state=ob_state,
            response_text="Que souhaitez-vous corriger : rôle, nom, ou zone ?",
            ag_ui_component={
                "type": "ChoiceComponent",
                "props": {
                    "title": "Corriger mes informations",
                    "options": [
                        {"label": "Rôle", "value": "ROLE"},
                        {"label": "Nom", "value": "NAME"},
                        {"label": "Zone", "value": "ZONE"},
                    ],
                },
            },
        )

    if text and llm_extract:
        try:
            decision = await llm_extract("confirm", text)
        except Exception:
            decision = None

        decision_norm = str(decision or "").strip().upper()
        if not decision_norm and _is_positive_ack(text):
            decision_norm = "YES"
        if decision_norm == "YES":
            ob_state.step = OnboardingStep.CREATE_PROFILE
            ob_state.prompt = ""
            return await _step_create_profile(ob_state, mcp_runtime)

        correction_requested = decision_norm == "NO"

        if correction_requested:
            new_role = None
            try:
                extracted_role = await llm_extract("role", text)
                new_role = _normalize_role(extracted_role)
            except Exception:
                new_role = None
            if new_role and new_role != ob_state.role:
                ob_state.role = new_role
                updated_summary = _build_confirmation_prompt(ob_state)
                return OnboardingResult(
                    state=ob_state,
                    response_text=f"{_role_mapping_ack(new_role)} {updated_summary}",
                    ag_ui_component=_confirmation_component(),
                )
            try:
                correction_field = await llm_extract("correction_field", text)
            except Exception:
                correction_field = None
            field_norm = str(correction_field or "").strip().upper()
            if field_norm == "ROLE":
                ob_state.step = OnboardingStep.COLLECT_ROLE
                ob_state.role = None
                return OnboardingResult(state=ob_state, response_text="D'accord. Êtes-vous acheteur (BUYER) ou producteur (PRODUCER) ?")
            if field_norm == "NAME":
                ob_state.step = OnboardingStep.COLLECT_NAME
                ob_state.name = None
                return OnboardingResult(state=ob_state, response_text="Quel est votre nom complet ?")
            if field_norm == "ZONE":
                ob_state.step = OnboardingStep.COLLECT_ZONE
                ob_state.zone_id = None
                ob_state.zone_name = None
                return OnboardingResult(state=ob_state, response_text="Dans quelle zone (ville ou province) opérez-vous ?")

            ob_state.step = OnboardingStep.CONFIRM_DETAILS
            ob_state.prompt = "CORRECTION_FIELD"
            return OnboardingResult(
                state=ob_state,
                response_text="Que souhaitez-vous corriger : rôle, nom, ou zone ?",
                ag_ui_component={
                    "type": "ChoiceComponent",
                    "props": {
                        "title": "Corriger mes informations",
                        "options": [
                            {"label": "Rôle", "value": "ROLE"},
                            {"label": "Nom", "value": "NAME"},
                            {"label": "Zone", "value": "ZONE"},
                        ],
                    },
                },
            )

        role_candidate = None
        name_candidate = None
        zone_candidate = None

        try:
            role_candidate = _normalize_role(await llm_extract("role", text))
        except Exception:
            role_candidate = None

        try:
            name_candidate = await llm_extract("name", text)
        except Exception:
            name_candidate = None

        try:
            zone_candidate = await llm_extract("zone", text)
        except Exception:
            zone_candidate = None

        changed = False
        role_changed = False

        if role_candidate and role_candidate != ob_state.role:
            ob_state.role = role_candidate
            changed = True
            role_changed = True

        if name_candidate and _is_valid(name_candidate):
            normalized_name = str(name_candidate).strip()
            if normalized_name and normalized_name != (ob_state.name or ""):
                ob_state.name = normalized_name
                changed = True

        if zone_candidate and _is_valid(zone_candidate):
            normalized_zone = str(zone_candidate).strip()
            if normalized_zone and normalized_zone != (ob_state.zone_name or ""):
                ob_state.zone_name = normalized_zone
                ob_state.zone_id = None
                changed = True

        if changed:
            await _resolve_zone_id(ob_state, mcp_runtime)
            if ob_state.zone_name and not ob_state.zone_id:
                hint = await _build_zone_catalog_hint(mcp_runtime)
                ob_state.step = OnboardingStep.COLLECT_ZONE
                response_text = (
                    f"Je n'ai pas trouvé la zone '{ob_state.zone_name}'. "
                    "Merci d'indiquer une ville ou province valide."
                )
                if hint:
                    response_text += hint
                return OnboardingResult(state=ob_state, response_text=response_text)

            updated_summary = _build_confirmation_prompt(ob_state)
            if role_changed:
                updated_summary = f"{_role_mapping_ack(ob_state.role or '')} {updated_summary}"
            ob_state.step = OnboardingStep.CONFIRM_DETAILS
            ob_state.prompt = ""
            return OnboardingResult(
                state=ob_state,
                response_text=updated_summary,
                ag_ui_component=_confirmation_component(),
            )

    # 3. Retour par défaut avec le composant UI de choix
    return OnboardingResult(
        state=ob_state,
        response_text=summary,
        ag_ui_component=_confirmation_component(),
    )


async def _step_create_profile(
    ob_state: OnboardingState,
    mcp_runtime: Any,
) -> OnboardingResult:
    if mcp_runtime is None:
        return OnboardingResult(
            state=ob_state, 
            response_text="Service indisponible.", 
            status="ERROR"
        )

    # Validation stricte du rôle : si absent, on bloque la création
    if not ob_state.role:
        ob_state.step = OnboardingStep.COLLECT_ROLE
        return OnboardingResult(
            state=ob_state,
            response_text="Le rôle n'est pas défini. Êtes-vous acheteur ou producteur ?",
            status="ERROR",
        )

    create_payload = {
        "phone": ob_state.phone,
        "name": ob_state.name,
        "role": ob_state.role,
        "zone_id": ob_state.zone_id,
    }

    try:
        raw_result = await mcp_runtime.call_db("create_user_profile", data=create_payload)
        
        # Parsing robuste (dict ou JSON string)
        if isinstance(raw_result, str):
            try:
                import ast
                result_dict = ast.literal_eval(raw_result)
            except:
                result_dict = {}
        else:
            result_dict = raw_result if isinstance(raw_result, dict) else {}

        # 1. Cas Succès
        if result_dict.get("status") == "success" or "data" in result_dict:
            profile = await _reload_profile(mcp_runtime, ob_state.phone)
            ob_state.created_profile = profile
            ob_state.completed = True
            ob_state.step = OnboardingStep.DONE
            ob_state.error = None
            return OnboardingResult(
                state=ob_state,
                response_text=(
                    f"✅ Bienvenue {ob_state.name} ! Ton profil est prêt. "
                    f"{_role_capabilities_message(ob_state.role)}"
                ),
                status="SUCCESS",
            )
        
        # 2. Cas Conflit (Téléphone déjà utilisé)
        error_msg = str(result_dict.get("message", "")).lower()
        if "already exists" in error_msg or "unique" in error_msg:
            profile = await _reload_profile(mcp_runtime, ob_state.phone)
            ob_state.created_profile = profile
            ob_state.completed = True
            ob_state.step = OnboardingStep.DONE
            return OnboardingResult(
                state=ob_state,
                response_text=(
                    f"Content de te revoir, {ob_state.name} ! Ton compte est déjà actif. "
                    f"{_role_capabilities_message(ob_state.role)}"
                ),
                status="SUCCESS",
            )
            
        # 3. Cas Erreur métier
        ob_state.step = OnboardingStep.CONFIRM_DETAILS
        return OnboardingResult(
            state=ob_state,
            response_text=f"Impossible de valider le profil : {result_dict.get('message', 'Erreur inconnue')}",
            status="ERROR",
        )

    except Exception as exc:
        ob_state.step = OnboardingStep.CONFIRM_DETAILS
        return OnboardingResult(
            state=ob_state,
            response_text="Une erreur technique est survenue. Veuillez réessayer plus tard.",
            status="ERROR",
        )
    



async def _reload_profile(mcp_runtime: Any, phone: Optional[str]) -> Dict[str, Any]:
    if not phone or mcp_runtime is None:
        return {}
    try:
        data = await mcp_runtime.call_db("get_user_by_phone", phone=phone)
        if isinstance(data, dict):
            if data.get("data") and isinstance(data["data"], dict):
                return data["data"]
            return data
        return {}
    except Exception:
        return {}