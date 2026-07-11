
from __future__ import annotations

"""Unified onboarding — data-driven slot-filling with free ordering.

Design
------
- ONE LLM call per turn extracts every field it can (role, name, zone, confirm).
- No hardcoded acknowledgement lexicon: only the LLM decides YES/NO.
- Any slot can be updated at any moment, including after a confirmation
  prompt — a change re-triggers confirmation with the new summary.
- ``step`` is derived from what is filled, not from a rigid sequence.
"""

import ast
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger("AgriConnect.Agents.Onboarding")


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


def _normalize_role(value: Optional[str]) -> Optional[str]:
    """Valide et normalise les sorties de l'extracteur LLM (BUYER|PRODUCER)."""
    if not value:
        return None

    norm_val = str(value).strip().upper()
    if norm_val in {"BUYER", "PRODUCER"}:
        return norm_val

    return None


def _merge_extracted(ob_state: OnboardingState, extracted: Dict[str, Optional[str]]) -> bool:
    """Apply extracted values onto *ob_state*. Return True if any slot changed.

    A change ALWAYS invalidates a prior confirmation — the caller uses this to
    decide whether to re-ask "confirmes-tu ?" after a mid-flow correction.
    """
    changed = False

    role = _normalize_role(extracted.get("role"))
    if role and role != ob_state.role:
        ob_state.role = role
        ob_state._filled_slots["role"] = True
        changed = True

    raw_name = extracted.get("name")
    if raw_name:
        name = str(raw_name).strip()
        if name and name.lower() != (ob_state.name or "").lower():
            ob_state.name = name
            ob_state._filled_slots["name"] = True
            changed = True

    raw_zone = extracted.get("zone")
    if raw_zone:
        zone = str(raw_zone).strip()
        if zone and zone.upper() != (ob_state.zone_name or "").upper():
            ob_state.zone_name = zone
            ob_state.zone_id = None
            ob_state._filled_slots["zone"] = True
            changed = True

    return changed


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



def _unwrap_tool_payload(raw: Any, *, _depth: int = 0) -> Optional[Dict[str, Any]]:
    if raw is None or _depth > 6:
        return None
    if isinstance(raw, dict):
        for key in ("data", "result", "payload", "response"):
            value = raw.get(key)
            if value is not None and value is not raw:
                nested = _unwrap_tool_payload(value, _depth=_depth + 1)
                if nested:
                    return nested
        raw_result = raw.get("raw_result")
        if raw_result is not None:
            nested = _unwrap_tool_payload(raw_result, _depth=_depth + 1)
            if nested:
                return nested
        for key in ("zones", "items", "results"):
            collection = raw.get(key)
            if isinstance(collection, list):
                nested = _unwrap_tool_payload(collection, _depth=_depth + 1)
                if nested:
                    return nested
        return raw
    if isinstance(raw, list):
        for item in raw:
            nested = _unwrap_tool_payload(item, _depth=_depth + 1)
            if nested:
                return nested
        return None
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        if stripped[0] in "[{":
            try:
                return _unwrap_tool_payload(json.loads(stripped), _depth=_depth + 1)
            except Exception:
                try:
                    return _unwrap_tool_payload(ast.literal_eval(stripped), _depth=_depth + 1)
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

BulkExtractor = Callable[[str], Coroutine[Any, Any, Dict[str, Optional[str]]]]


_WELCOME = (
    "Bienvenue sur AgriConnect ! Je suis votre assistant agricole.\n"
    "Je vous aide a acheter ou vendre des produits agricoles, gerer vos stocks, "
    "et trouver les meilleurs prix dans votre zone.\n\n"
    "Pour vous inscrire, dites-moi votre nom, votre role (acheteur ou producteur) "
    "et votre ville. Vous pouvez tout dire d'un coup ou etape par etape, dans l'ordre "
    "qui vous arrange."
)


def _ack_line(ob_state: OnboardingState) -> str:
    parts: List[str] = []
    if ob_state._filled_slots.get("name") and ob_state.name:
        parts.append(f"Enchante {ob_state.name}.")
    if ob_state._filled_slots.get("role") and ob_state.role:
        label = "acheteur" if ob_state.role == "BUYER" else "producteur"
        parts.append(f"Compris, vous etes {label}.")
    if ob_state._filled_slots.get("zone") and ob_state.zone_name:
        parts.append(f"Zone notee : {ob_state.zone_name}.")
    return " ".join(parts)


def _missing_labels(ob_state: OnboardingState) -> List[str]:
    labels: List[str] = []
    if not ob_state.role:
        labels.append("votre role (acheteur ou producteur)")
    if not ob_state.name:
        labels.append("votre nom")
    if not ob_state.zone_id:
        labels.append("votre ville ou province")
    return labels


def _collect_prompt(ob_state: OnboardingState) -> str:
    ack = _ack_line(ob_state)
    missing = _missing_labels(ob_state)
    if not missing:
        return ack or "Merci."
    if len(missing) == 1:
        ask = f"Il me manque {missing[0]}."
    else:
        joined = ", ".join(missing[:-1]) + f" et {missing[-1]}"
        ask = f"Il me manque encore {joined}."
    return f"{ack} {ask}".strip()


async def run_onboarding_step(
    ob_state: OnboardingState,
    user_text: str,
    mcp_runtime: Any,
    *,
    extracted_entities: Optional[Dict[str, Any]] = None,
    llm_extract_all: Optional[BulkExtractor] = None,
) -> OnboardingResult:
    """One-shot slot-filling with free ordering and mid-flow corrections.

    Design principles :
    - ONE LLM call per turn extracts every field it can (role, name, zone, confirm).
    - No hardcoded acknowledgement tokens : the LLM alone decides YES/NO.
    - Any field can be updated at any time (even after "confirm ?") — a change
      invalidates the confirmation and re-asks it with the new summary.
    - The state machine is data-driven : ``step`` is derived from what is filled,
      not from a rigid sequence.
    """
    ob_state._filled_slots = {}
    text = (user_text or "").strip()

    if not isinstance(ob_state.step, OnboardingStep):
        ob_state.step = OnboardingStep.COLLECT_ROLE

    # Welcome : cold start (no text yet, no data collected).
    if not text and not ob_state.role and not ob_state.name and not ob_state.zone_name:
        ob_state.step = OnboardingStep.COLLECT_ROLE
        return OnboardingResult(
            state=ob_state,
            response_text=_WELCOME,
            ag_ui_component={
                "type": "ChoiceComponent",
                "props": {
                    "title": "Vous etes",
                    "options": [
                        {"label": "Acheteur", "value": "BUYER"},
                        {"label": "Producteur", "value": "PRODUCER"},
                    ],
                },
            },
        )

    # Bulk extract : one shot, everything the LLM can pull from this turn.
    extracted: Dict[str, Optional[str]] = {"role": None, "name": None, "zone": None, "confirm": None}
    entities = extracted_entities or {}
    if entities:
        extracted["role"] = entities.get("role")
        extracted["name"] = entities.get("name")
        extracted["zone"] = (
            entities.get("zone")
            or entities.get("zone_name")
            or entities.get("location")
        )

    if text and llm_extract_all:
        try:
            llm_out = await llm_extract_all(text)
        except Exception:
            llm_out = {}
        for key in ("role", "name", "zone", "confirm"):
            if not extracted.get(key) and llm_out.get(key):
                extracted[key] = llm_out.get(key)

    changed = _merge_extracted(ob_state, extracted)

    # Zone : validate against catalog only if we have a name but no id yet.
    if ob_state.zone_name and not ob_state.zone_id:
        await _resolve_zone_id(ob_state, mcp_runtime)
        if not ob_state.zone_id:
            failed = ob_state.zone_name
            ob_state.zone_name = None
            hint = await _build_zone_catalog_hint(mcp_runtime)
            if hint:
                msg = (
                    f"Je n'ai pas trouve la zone '{failed}'. "
                    f"Merci d'indiquer une ville ou province valide.{hint}"
                )
            else:
                msg = (
                    f"Je n'ai pas pu verifier la zone '{failed}'. "
                    "Indiquez simplement votre ville ou province "
                    "(ex : Ouagadougou, Bobo-Dioulasso, Koudougou)."
                )
            ob_state.step = OnboardingStep.COLLECT_ROLE
            return OnboardingResult(state=ob_state, response_text=msg)

    confirm = extracted.get("confirm")
    all_present = bool(ob_state.role and ob_state.name and ob_state.zone_id and ob_state.phone)

    # Create : all slots filled, explicit YES, nothing modified this turn.
    if all_present and confirm == "YES" and not changed:
        ob_state.step = OnboardingStep.CREATE_PROFILE
        return await _step_create_profile(ob_state, mcp_runtime)

    # Confirmation : slots complete but awaiting explicit YES (or a correction).
    if all_present:
        ob_state.step = OnboardingStep.CONFIRM_DETAILS
        summary = _build_confirmation_prompt(ob_state)
        if changed:
            prefix_parts = [_ack_line(ob_state), "J'ai mis a jour vos informations."]
            summary = " ".join(p for p in prefix_parts if p).strip() + " " + summary
        elif confirm == "NO":
            summary = (
                "Pas de souci — dites-moi ce que je dois corriger "
                "(le bon nom, le bon role ou la bonne zone). "
                f"Recapitulatif actuel : {summary}"
            )
        return OnboardingResult(
            state=ob_state,
            response_text=summary,
            ag_ui_component=_confirmation_component(),
        )

    # Still collecting : ask for whichever slot(s) remain, in a friendly free-order tone.
    ob_state.step = OnboardingStep.COLLECT_ROLE
    return OnboardingResult(state=ob_state, response_text=_collect_prompt(ob_state))


async def _resolve_zone_id(ob_state: OnboardingState, mcp_runtime: Any) -> bool:
    if not ob_state.zone_name or ob_state.zone_id or mcp_runtime is None:
        return bool(ob_state.zone_id)

    try:
        zone_response = await mcp_runtime.call_db("get_zone_by_name", name=ob_state.zone_name)
        logger.debug("[_resolve_zone_id] raw response: %s", zone_response)
        payload = _unwrap_tool_payload(zone_response)

        if isinstance(payload, dict):
            if payload.get("ok") is False or payload.get("status") == "error":
                logger.warning("[_resolve_zone_id] MCP error for '%s': %s", ob_state.zone_name, payload.get("error") or payload.get("message"))
                return False
            zone_id = payload.get("zone_id") or payload.get("id")
            zone_label = payload.get("zone_name") or payload.get("name") or payload.get("label")

            if zone_id:
                ob_state.zone_id = str(zone_id)
            if zone_label:
                ob_state.zone_name = str(zone_label)
    except Exception as exc:
        logger.error("[_resolve_zone_id] Exception for '%s': %s", ob_state.zone_name, exc, exc_info=True)
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
    except Exception as exc:
        logger.warning("[_build_zone_catalog_hint] %s", exc)
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


def _build_confirmation_prompt(ob_state: OnboardingState) -> str:
    name = ob_state.name or "(non renseigne)"
    role = ob_state.role or "(non renseigne)"
    role_label = "Acheteur" if role == "BUYER" else "Producteur" if role == "PRODUCER" else role
    zone = ob_state.zone_name or "(non renseignee)"
    return (
        f"Recapitulatif : Nom = {name}, Role = {role_label}, Zone = {zone}. "
        "Est-ce correct ?"
    )


async def _step_create_profile(
    ob_state: OnboardingState,
    mcp_runtime: Any,
) -> OnboardingResult:
    if mcp_runtime is None:
        return OnboardingResult(
            state=ob_state,
            response_text="Service indisponible. Veuillez reessayer dans quelques instants.",
            status="ERROR"
        )

    if not ob_state.phone:
        ob_state.step = OnboardingStep.COLLECT_ROLE
        return OnboardingResult(
            state=ob_state,
            response_text="Impossible de creer votre profil sans numero de telephone. Veuillez reessayer.",
            status="ERROR",
        )

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