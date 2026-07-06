"""Action handlers for the Agronomy domain."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from agriconnect.graphs.agents.market_coach.registry import register_action
from agriconnect.graphs.agents.market_coach.actions.common import require, require_phone, normalize_quantity_to_kg

@register_action("AGRO_GET_CYCLES", mode="READ")
def prep_agro_get_cycles(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare l'historique des cycles d'un domaine."""
    require_phone(state)
    farm_id = str(require(payload, "farm_id"))
    return "get_crop_cycles", {"farm_id": farm_id}


@register_action("AGRO_GET_STANDARDS", mode="READ")
def prep_agro_get_standards(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la consultation des fiches techniques."""
    product = str(require(payload, "product"))
    return "get_crop_requirements", {"crop_type": product}


@register_action("AGRO_GET_ECONOMICS", mode="READ")
def prep_agro_get_economics(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare le bilan financier d'une parcelle."""
    require_phone(state)
    cycle_id = str(require(payload, "cycle_id"))
    return "get_cycle_economics", {"cycle_id": cycle_id}


@register_action("AGRO_GET_RISKS", mode="READ")
def prep_agro_get_risks(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare l'analyse des risques sanitaires."""
    require_phone(state)
    farm_id = str(require(payload, "farm_id"))
    return "get_active_sanitary_risks", {"farm_id": farm_id}


@register_action("FARM_GET_MY_LIST", mode="READ")
def prep_farm_get_my_list(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Prépare la demande de listing des exploitations du producteur."""
    phone = require_phone(state)
    # Règle n°1 : Si FastMCP utilise 'get_producer_farm' pour lister, on garde ce nom,
    # mais assure-toi que le service DB renvoie bien le tableau de toutes les fermes.
    return "get_producer_farm", {"phone": phone}


@register_action("CROP_START_CYCLE", mode="WRITE")
def prep_crop_start_cycle(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    require_phone(state)
    farm_id = str(require(payload, "farm_id"))
    product = str(require(payload, "product"))
    surface = float(require(payload, "surface"))
    data: Dict[str, Any] = {"crop_type": product, "area_size": surface}
    if payload.get("variety"):
        data["variety"] = str(payload["variety"])
    if payload.get("target_yield"):
        data["target_yield"] = float(payload["target_yield"])
    return "create_crop_cycle", {"farm_id": farm_id, "data": data}


@register_action("DECLARE_CROP_CYCLE", mode="WRITE")
def prep_declare_crop_cycle(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    phone = require_phone(state)
    farm_id = str(require(payload, "farm_id"))
    product = str(require(payload, "product"))
    production_type = str(require(payload, "production_type")).upper().strip()
    if production_type not in {"CROP", "LIVESTOCK"}:
        raise ValueError("production_type must be CROP or LIVESTOCK")

    qty_raw = float(require(payload, "quantity_mentioned"))
    unit_in = payload.get("unit_mentioned")
    if production_type == "CROP":
        qty, unit = normalize_quantity_to_kg(qty_raw, unit_in)
    else:
        qty = qty_raw
        unit = str(unit_in or "HEAD").upper().strip()

    estimated_available_at = str(require(payload, "estimated_available_at"))
    price_per_unit = float(require(payload, "price_mentioned"))

    production_payload: Dict[str, Any] = {
        "farm_id": farm_id,
        "production_type": production_type,
        "product": product,
        "quantity_mentioned": qty,
        "unit": unit,
        "estimated_available_at": estimated_available_at,
        "price_per_unit": price_per_unit,
        "preorder_enabled": bool(payload.get("preorder_enabled", True)),
        "is_public": bool(payload.get("is_public", True)),
    }

    if payload.get("breed"):
        production_payload["breed"] = str(payload["breed"])
    if payload.get("surface"):
        production_payload["surface"] = payload["surface"]
    if payload.get("area_size"):
        production_payload["area_size"] = payload["area_size"]
    if payload.get("expected_harvest_date"):
        production_payload["expected_harvest_date"] = payload["expected_harvest_date"]
    if payload.get("planted_at"):
        production_payload["planted_at"] = payload["planted_at"]

    return "declare_future_production", {"payload": production_payload, "phone": phone}


@register_action("CROP_RECORD_INTERVENTION", mode="WRITE")
def prep_crop_record_intervention(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    require_phone(state)
    cycle_id = str(require(payload, "cycle_id"))
    intervention_type = str(require(payload, "intervention_type")).upper().strip()
    data: Dict[str, Any] = {"type": intervention_type}
    if payload.get("input_used"):
        data["input_used"] = str(payload["input_used"])
    if payload.get("quantity_mentioned"):
        data["quantity"] = float(payload["quantity_mentioned"])
    if payload.get("details"):
        data["description"] = str(payload["details"])
    return "log_intervention", {"cycle_id": cycle_id, "data": data}


@register_action("CROP_RECORD_OBSERVATION", mode="WRITE")
def prep_crop_record_observation(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    require_phone(state)
    cycle_id = str(require(payload, "cycle_id"))
    stage = payload.get("stage_code") or payload.get("stage_label")
    try:
        stage_code = int(stage)
    except (TypeError, ValueError):
        stage_code = 0
    return "add_growth_log", {"cycle_id": cycle_id, "stage_code": stage_code}


@register_action("CROP_UPDATE_STAGE", mode="WRITE")
def prep_crop_update_stage(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    require_phone(state)
    cycle_id = str(require(payload, "cycle_id"))
    stage_name = str(require(payload, "stage_name"))
    return "add_crop_growth_stage", {"data": {"cycle_id": cycle_id, "stage_name": stage_name}}


@register_action("CROP_UPDATE_SOIL", mode="WRITE")
def prep_crop_update_soil(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    require_phone(state)
    farm_id = str(require(payload, "farm_id"))
    ph = float(require(payload, "ph"))
    data: Dict[str, Any] = {"ph": ph}
    if payload.get("organic_matter"):
        data["organic_matter"] = float(payload["organic_matter"])
    return "update_soil_profile", {"farm_id": farm_id, "data": data}


@register_action("FARM_CREATE", mode="WRITE")
def prep_farm_create(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    phone = require_phone(state)
    name = str(require(payload, "farm_name"))
    zone = str(require(payload, "zone"))
    return "get_or_create_farm", {"producer_id": phone, "farm_name": name, "zone_id": zone}


@register_action("FARM_UPDATE", mode="WRITE")
def prep_farm_update(state: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    require_phone(state)
    farm_id = str(require(payload, "farm_id"))
    args: Dict[str, Any] = {"farm_id": farm_id}
    return "update_farm", args
