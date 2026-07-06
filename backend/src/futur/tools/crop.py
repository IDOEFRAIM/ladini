import logging
from typing import Any, Dict, Optional

from agriconnect.infrastructure.mcp.client import UnifiedMCPClient
from pydantic import ValidationError

from .shared_math import CropProfile, SahelianCropProfile

logger = logging.getLogger("BurkinaCropTool")


class CropProfileNotFoundError(LookupError):
    """Raised when the requested crop profile does not exist in DB."""


class CropProfileValidationError(ValueError):
    """Raised when payload cannot be validated as SahelianCropProfile."""


class CropProfileBackendUnavailableError(RuntimeError):
    """Raised when no MCP/DB fallback is available for crop profiles."""


class BurkinaCropTool:
    """MCP-first agronomic knowledge accessor used by FormationAdvisor."""

    MCP_TOOL_NAMES = ("get_sahelian_crop_profile", "get_crop_profile")

    def __init__(
        self,
        *,
        session_id: str = "formation.advisor",
        mcp_client: Optional[UnifiedMCPClient] = None,
    ) -> None:
        self.MATH_PROFILES = {
            "maïs": CropProfile("Maïs", 10, 35, {"ini": 0.3, "mid": 1.2, "end": 0.6}, 90, True),
            "niébé": CropProfile("Niébé", 12, 36, {"ini": 0.4, "mid": 1.0, "end": 0.35}, 70, False),
            "sorgho": CropProfile("Sorgho", 10, 40, {"ini": 0.3, "mid": 1.1, "end": 0.55}, 110, False),
        }
        self._session_id = session_id
        self._mcp_client = mcp_client

    # --- MCP + fallback plumbing -------------------------------------------------

    def _ensure_mcp_client(self) -> UnifiedMCPClient:
        if self._mcp_client is None:
            self._mcp_client = UnifiedMCPClient(session_id=self._session_id)
        return self._mcp_client

    async def _call_mcp_profile(self, crop: str, zone: str) -> Dict[str, Any]:
        client = self._ensure_mcp_client()
        last_exc: Optional[Exception] = None
        for tool_name in self.MCP_TOOL_NAMES:
            try:
                raw = await client.call_tool(tool_name, {"crop": crop, "zone": zone})
                if not isinstance(raw, dict):
                    raise CropProfileBackendUnavailableError("Réponse MCP invalide (type inattendu)")
                if raw.get("status") == "error":
                    msg = raw.get("message") or "Erreur MCP"
                    err_type = (raw.get("error_type") or "").lower()
                    if err_type in {"lookuperror", "cropprofilenotfounderror"}:
                        raise CropProfileNotFoundError(msg)
                    raise CropProfileBackendUnavailableError(msg)
                payload = raw.get("data") or raw.get("result") or raw.get("content") or raw
                if not isinstance(payload, dict):
                    raise CropProfileBackendUnavailableError("Payload MCP get_crop_profile invalide")
                return payload
            except CropProfileNotFoundError:
                raise
            except Exception as exc:
                last_exc = exc
        raise CropProfileBackendUnavailableError(
            f"Aucun tool MCP de profil culture disponible ({', '.join(self.MCP_TOOL_NAMES)})"
        ) from last_exc

    def _math_profile_to_sahelian(self, crop: str, zone: str) -> Optional[SahelianCropProfile]:
        fallback = self.MATH_PROFILES.get((crop or "").lower())
        if not fallback:
            return None
        payload = {
            "name": fallback.name,
            "varieties": {zone.capitalize(): [fallback.name]},
            "cycle_days": max(30, fallback.cycle_days),
            "seeding_density": "N/A",
            "depth_cm": 5.0,
            "organic_matter_min_tha": 0.0,
            "mineral_fertilizer": {},
            "water_strategy": "Standard",
            "scientific_name": fallback.name,
            "sowing_config": {"inter_row": 80.0, "inter_plant": 40.0, "seeds_pocket": 2},
            "fertilizer_plan": [],
            "yield_potential": (1.0, 2.0),
            "key_pests": [],
            "key_diseases": [],
            "pre_flight_checks": [],
        }
        return SahelianCropProfile.model_validate(payload)

    async def _fetch_profile(self, crop: str, zone: str) -> SahelianCropProfile:
        if not crop:
            raise ValueError("Culture requise")
        try:
            payload = await self._call_mcp_profile(crop, zone)
            return SahelianCropProfile.model_validate(payload)
        except CropProfileNotFoundError:
            raise
        except ValidationError as exc:
            logger.error("Payload MCP invalide pour crop=%s zone=%s: %s", crop, zone, exc)
            raise CropProfileValidationError(str(exc)) from exc
        except Exception as mcp_exc:
            logger.warning("MCP get_crop_profile indisponible (%s); tentative profil math", mcp_exc)
            fallback = self._math_profile_to_sahelian(crop, zone)
            if fallback:
                return fallback
            raise CropProfileBackendUnavailableError(
                "Aucun profil agronomique disponible via MCP ou fallback local"
            ) from mcp_exc

    # --- public helpers ----------------------------------------------------------

    async def get_technical_sheet(self, crop: str, zone: str) -> str:
        try:
            profile = await self._fetch_profile(crop, zone)
        except CropProfileNotFoundError:
            return f"Culture '{crop}' non répertoriée en base INERA."
        varieties = profile.varieties.get(zone.capitalize(), [profile.name])
        return (
            f"📍 **FICHE TECHNIQUE : {profile.name.upper()} ({zone.upper()})**\n"
            f"--- \n"
            f"🧬 **Variété :** {', '.join(varieties)}\n"
            f"⏱️ **Cycle :** {profile.cycle_days} jours\n"
            f"📏 **Semis :** {profile.sowing_config.inter_row}cm x {profile.sowing_config.inter_plant}cm\n"
            f"💩 **Fumure Orga :** {profile.organic_matter_min_tha} t/ha\n"
            f"💧 **Eau :** {profile.water_strategy}"
        )

    async def calculate_inputs(self, crop: str, surface_ha: float) -> Dict[str, Any]:
        try:
            profile = await self._fetch_profile(crop, "Centre")
        except CropProfileNotFoundError:
            return {}
        inputs: Dict[str, float] = {}
        for step in profile.fertilizer_plan:
            product = step.type or "Engrais"
            inputs[product + "_kg"] = inputs.get(product + "_kg", 0.0) + float(step.dose_kg_ha) * float(surface_ha)
        return inputs

    def get_math_profile(self, crop: str) -> Optional[CropProfile]:
        return self.MATH_PROFILES.get((crop or "").lower())