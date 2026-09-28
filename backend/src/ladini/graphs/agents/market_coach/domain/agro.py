from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from ladini.domain.commercial_offer import (
    CommercialOffer,
    CommercialQuantity,
    PriceBasis,
    convert_commercial_quantity_to_base_unit,
    offer_execution_payload,
)
from ladini.graphs.agents.market_coach.actions.common import (
    normalize_quantity_to_kg,
    require,
    require_phone,
)
from ladini.graphs.agents.market_coach.actions.tooling import ToolId

from .model import DomainContext, DomainResult

logger = logging.getLogger("Ladini.MarketCoach.FutureProduction")


@dataclass
class AgronomyService:
    """(2026-09-14, Deep Intent Architecture Cleanup) : nettoyée — les
    méthodes `get_cycles`/`get_standards`/`get_economics`/`get_risks`/
    `start_cycle`/`record_intervention`/`record_observation`/
    `update_stage`/`update_soil` sont SUPPRIMÉES (leurs intents
    correspondants n'existent plus dans `INTENT_CONFIG`, aucun autre
    appelant trouvé — code mort confirmé). Ne restent que la production
    future (marketplace) et les exploitations (FARM_*)."""

    context: DomainContext

    def get_my_farms(self, command: FarmGetMyListCommand) -> DomainResult:
        args: Dict[str, Any] = {"phone": str(command.phone)}
        return DomainResult(tool_id=ToolId.GET_PRODUCER_FARM, tool_args=args)

    def declare_crop_cycle(
        self, state: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> DomainResult:
        """Déclare une production future (précommandable) — écriture UNIQUE de `MarketOffer`.

        Phase B2c.3 : `payload["commercial_offer"]` (Phase A/B1, sérialisation de
        `domain/commercial_offer.py::CommercialOffer`) est, quand présent, l'AUTORITÉ — construit et
        certifié par le validator (`nodes/validation.py`, même moteur que SALES_PUBLISH_PRODUCT,
        voir `services/domain/commercial_gate.py`) AVANT que ce point ne soit jamais atteint : un prix
        sans base fiable (« 4 000 000 » nu, ni « par tonne » ni « pour tout ») n'arrive jamais ici,
        le validator redemande. `quantity`/`unit`/`price_per_unit` sont alors DÉRIVÉS de l'offre
        (`offer_execution_payload`), jamais relus des champs plats. L'offre elle-même voyage jusqu'à
        `declare_future_production`, qui en dérive `MarketOffer.pricing_snapshot` et vérifie le
        dual-write legacy (même garde que `create_product`, voir `services/database/pricing_persistence.py`).

        Repli LEGACY (`commercial_offer` absent) : conserve le comportement PRÉ-B2c.3 tel quel — la
        seule façon d'atteindre cette branche est un appelant qui contourne le validator (ex: un test
        direct de ce service) ; `pricing_snapshot` reste alors NULL (sémantique inconnue), jamais
        « probablement par unité »."""
        phone = require_phone(state)
        farm_id = str(require(payload, "farm_id"))
        product = str(require(payload, "product"))
        production_type = str(require(payload, "production_type")).upper().strip()
        if production_type not in {"CROP", "LIVESTOCK"}:
            raise ValueError("production_type must be CROP or LIVESTOCK")

        estimated_available_at = str(require(payload, "estimated_available_at"))

        commercial_offer_data = payload.get("commercial_offer")
        offer = CommercialOffer.from_dict(commercial_offer_data) if commercial_offer_data else None
        commercial_offer_out: Optional[Dict[str, Any]] = None

        if offer is not None:
            verdict = offer.validate()
            if not verdict.is_valid:
                raise ValueError(
                    f"offre commerciale non certifiable: {verdict.status} "
                    f"{verdict.missing_fields}{verdict.conflicts}"
                )
            if offer.pricing is not None and offer.pricing.basis == PriceBasis.PER_PACKAGE:
                # Fail-closed (mandat B2c.3 §11) : MarketOffer n'a pas de conditionnement/paliers —
                # jamais deviner un contenu ni convertir arbitrairement vers un prix par unité.
                raise ValueError(
                    "Le prix par conditionnement (« la caisse de 25 kg ») n'est pas encore pris en "
                    "charge pour une production future. Indiquez un prix par unité ou pour l'ensemble "
                    "du lot."
                )
            derived = offer_execution_payload(offer)
            qty, unit, price_per_unit = derived["quantity"], derived["unit"], derived["price"]
            commercial_offer_out = offer.to_dict()
            logger.info(
                "FUTURE_OFFER_CERTIFIED | flow_id=…%s | basis=%s",
                str(phone or "")[-4:],
                offer.pricing.basis.value if offer.pricing and offer.pricing.basis else None,
            )
        else:
            qty_raw = float(require(payload, "quantity"))
            unit_in = payload.get("unit")
            price_per_unit = float(require(payload, "price"))
            if production_type == "CROP":
                qty, unit = normalize_quantity_to_kg(qty_raw, unit_in)
                # (2026-09-28, hardening P0 — même bug que `sales.py::publish_product`,
                # confirmé par audit) : `price_per_unit` est documenté comme "par
                # `unit`" — si la quantité a été convertie (TONNE -> KG), le prix
                # doit être re-basé dans la même proportion, jamais laissé
                # inchangé pendant que `unit` change sous ses pieds.
                converted = convert_commercial_quantity_to_base_unit(
                    CommercialQuantity(qty_raw, str(unit_in or "KG")), unit
                )
                price_rescale_factor = converted[1] if converted is not None else 1.0
                price_per_unit = price_per_unit / price_rescale_factor
            else:
                qty = qty_raw
                unit = str(unit_in or "HEAD").upper().strip()

        production_payload: Dict[str, Any] = {
            "farm_id": farm_id,
            "production_type": production_type,
            "product": product,
            "quantity": qty,
            "unit": unit,
            "estimated_available_at": estimated_available_at,
            "price_per_unit": price_per_unit,
            "commercial_offer": commercial_offer_out,
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
            production_payload["expected_harvest_date"] = payload[
                "expected_harvest_date"
            ]
        if payload.get("planted_at"):
            production_payload["planted_at"] = payload["planted_at"]

        args: Dict[str, Any] = {"payload": production_payload, "phone": phone}
        return DomainResult(tool_id=ToolId.DECLARE_FUTURE_PRODUCTION, tool_args=args)

    def create_farm(self, command: FarmCreateCommand) -> DomainResult:
        args: Dict[str, Any] = {
            "producer_id": str(command.phone),
            "farm_name": str(command.farm_name),
            "zone_id": str(command.zone),
        }
        return DomainResult(tool_id=ToolId.GET_OR_CREATE_FARM, tool_args=args)

    def update_farm(self, command: FarmUpdateCommand) -> DomainResult:
        args: Dict[str, Any] = {"farm_id": str(command.farm_id)}
        return DomainResult(tool_id=ToolId.UPDATE_FARM, tool_args=args)


@dataclass(frozen=True)
class FarmGetMyListCommand:
    phone: str


@dataclass(frozen=True)
class FarmCreateCommand:
    phone: str
    farm_name: str
    zone: str
    surface: Optional[float] = None  # optional, not sent to tool for now


@dataclass(frozen=True)
class FarmUpdateCommand:
    phone: str
    farm_id: str
    farm_name: Optional[str] = None
    surface: Optional[float] = None
