from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import logging
from typing import Any, Dict, Optional

from agriconnect.infrastructure.database.db import get_async_db
from pydantic import ValidationError

from .shared_math import CropProfile, SahelianCropProfile

logger = logging.getLogger("BurkinaCropTool")


class CropProfileNotFoundError(LookupError):
    """Raised when the requested crop profile does not exist in DB."""


class CropProfileValidationError(ValueError):
    """Raised when DB data cannot be validated as SahelianCropProfile."""


class CropProfileBackendUnavailableError(RuntimeError):
    """Raised when the DB backend/tables required for crop profiles are unavailable."""


def _is_undefined_table_error(exc: Exception) -> bool:
    """Return True if exception indicates a missing relation/table in Postgres."""
    # SQLAlchemy DBAPIError often wraps the original asyncpg/psycopg2 exception in `.orig`.
    orig = getattr(exc, "orig", None)

    # asyncpg: UndefinedTableError(sqlstate='42P01')
    sqlstate = getattr(orig, "sqlstate", None) or getattr(exc, "sqlstate", None)
    if sqlstate == "42P01":
        return True

    # psycopg2: errors.UndefinedTable has pgcode='42P01'
    pgcode = getattr(orig, "pgcode", None) or getattr(exc, "pgcode", None)
    if pgcode == "42P01":
        return True

    # Fallback: class name checks
    if (getattr(orig, "__class__", None) and orig.__class__.__name__ in {"UndefinedTableError", "UndefinedTable"}):
        return True
    if exc.__class__.__name__ in {"UndefinedTableError", "UndefinedTable"}:
        return True

    return False

class BurkinaCropTool:
    """
    Outil spécialisé contenant la base de connaissance technique
    des cultures au Burkina Faso (INERA).
    VERSION HYBRIDE : Données en BDD (PostgreSQL/JSONB) + Logique en Python.
    """
    def __init__(self):
        # Profils agronomiques pour calculs (Legacy hardcoded for math logic only if needed)
        self.MATH_PROFILES = {
            "maïs": CropProfile("Maïs", 10, 35, {'ini': 0.3, 'mid': 1.2, 'end': 0.6}, 90, True),
            "niébé": CropProfile("Niébé", 12, 36, {'ini': 0.4, 'mid': 1.0, 'end': 0.35}, 70, False),
            "sorgho": CropProfile("Sorgho", 10, 40, {'ini': 0.3, 'mid': 1.1, 'end': 0.55}, 110, False)
        }

    async def _get_profile_from_db(self, crop: str, zone: str) -> SahelianCropProfile:
        """Fetch and validate crop profile from DB.

        Returns:
            SahelianCropProfile: validated deterministic profile.
        Raises:
            CropProfileNotFoundError: when crop/zone is unavailable.
            CropProfileValidationError: when DB payload violates profile contract.
            Exception: DB access errors.
        """
        deferred_backend_error: Optional[CropProfileBackendUnavailableError] = None

        try:
            async with get_async_db() as session:
                def with_default(field_name: str, raw_value: Any, default_value: Any) -> Any:
                    if raw_value is None:
                        logger.warning(
                            "Missing optional field '%s' for crop=%s zone=%s; defaulting to %r",
                            field_name,
                            crop,
                            zone,
                            default_value,
                        )
                        return default_value
                    return raw_value

                stmt = text(
                    "SELECT id, slug, crop_name, variety, zone_category, scientific_name, cycle_days, depth_cm, organic_matter_min_tha, water_strategy, inter_row_cm, inter_plant_cm, seeds_pocket, yield_min_t_ha, yield_max_t_ha, key_pests, key_diseases, pre_flight_checks"
                    " FROM crop_profiles"
                    " WHERE lower(slug) = lower(:slug) OR (lower(crop_name) = lower(:crop) AND lower(zone_category) = lower(:zone))"
                    " LIMIT 1"
                )
                try:
                    result = await session.execute(stmt, {"slug": crop.lower(), "crop": crop, "zone": zone})
                    row = result.first()
                except Exception as exc:
                    # If the schema isn't migrated yet, avoid triggering noisy transaction logs.
                    if _is_undefined_table_error(exc):
                        logger.warning("Crop profile tables missing; running without DB calibration")
                        deferred_backend_error = CropProfileBackendUnavailableError("crop profile tables missing")
                        try:
                            await session.rollback()
                        except Exception:
                            pass
                        row = None
                    else:
                        raise

                # If DB calibration tables are missing, skip further DB work.
                if deferred_backend_error is None:
                    if not row:
                        stmt2 = text(
                            "SELECT id, slug, crop_name, NULL as variety, zone_category, ''::text as scientific_name, cycle_days, depth_cm, organic_matter_min_tha, water_strategy, inter_row_cm, inter_plant_cm, seeds_pocket, yield_min_t_ha, yield_max_t_ha, key_pests, key_diseases, pre_flight_checks FROM crop_knowledge_compat WHERE lower(slug) = lower(:slug) OR lower(crop_name) = lower(:crop) LIMIT 1"
                        )
                        try:
                            result2 = await session.execute(stmt2, {"slug": crop.lower(), "crop": crop})
                            row = result2.first()
                        except Exception as exc:
                            if _is_undefined_table_error(exc):
                                logger.warning("Crop knowledge compat view missing; running without DB calibration")
                                deferred_backend_error = CropProfileBackendUnavailableError("crop knowledge compat view missing")
                                try:
                                    await session.rollback()
                                except Exception:
                                    pass
                                row = None
                            else:
                                raise

                if deferred_backend_error is None:
                    if not row:
                        raise CropProfileNotFoundError(
                            f"No calibrated crop profile found for crop='{crop}' zone='{zone}'"
                        )

                    (
                        profile_id,
                        _slug,
                        crop_name_db,
                        variety,
                        zone_category,
                        scientific_name,
                        cycle_days,
                        depth_cm,
                        organic_matter_min_tha,
                        water_strategy,
                        inter_row_cm,
                        inter_plant_cm,
                        seeds_pocket,
                        yield_min_t_ha,
                        yield_max_t_ha,
                        key_pests,
                        key_diseases,
                        pre_flight_checks,
                    ) = row

                    fert_stmt = text(
                        "SELECT step_order, stage, product_type, dose_kg_ha, application_mode FROM crop_fertilizer_steps WHERE crop_profile_id = :pid ORDER BY step_order ASC, id ASC"
                    )
                    try:
                        fert_res = await session.execute(fert_stmt, {"pid": profile_id})
                    except Exception as exc:
                        if _is_undefined_table_error(exc):
                            logger.warning("crop_fertilizer_steps missing; continuing with empty fertilizer plan")
                            fert_res = None
                        else:
                            raise
                    fertilizer_plan = [
                        {
                            "step_order": int(with_default("fertilizer_plan.step_order", fr[0], 1)),
                            "stage": str(with_default("fertilizer_plan.stage", fr[1], "Semis")),
                            "type": str(with_default("fertilizer_plan.type", fr[2], "NPK")),
                            "dose_kg_ha": float(with_default("fertilizer_plan.dose_kg_ha", fr[3], 0.0)),
                            "mode": str(with_default("fertilizer_plan.mode", fr[4], "Épandage")),
                        }
                        for fr in (fert_res.fetchall() if fert_res is not None else [])
                    ]

                    payload: Dict[str, Any] = {
                        "name": str(crop_name_db or crop),
                        "varieties": {str(zone_category or zone): [str(variety)]} if variety else {},
                        "cycle_days": int(with_default("cycle_days", cycle_days, 90)),
                        "seeding_density": "N/A",
                        "depth_cm": float(with_default("depth_cm", depth_cm, 5.0)),
                        "organic_matter_min_tha": float(with_default("organic_matter_min_tha", organic_matter_min_tha, 0.0)),
                        "mineral_fertilizer": {},
                        "water_strategy": str(with_default("water_strategy", water_strategy, "")),
                        "scientific_name": str(with_default("scientific_name", scientific_name, "")),
                        "sowing_config": {
                            "inter_row": float(with_default("sowing_config.inter_row", inter_row_cm, 80.0)),
                            "inter_plant": float(with_default("sowing_config.inter_plant", inter_plant_cm, 40.0)),
                            "seeds_pocket": int(with_default("sowing_config.seeds_pocket", seeds_pocket, 2)),
                        },
                        "fertilizer_plan": fertilizer_plan,
                        "yield_potential": (
                            float(with_default("yield_potential.min", yield_min_t_ha, 0.0)),
                            float(with_default("yield_potential.max", yield_max_t_ha, 0.0)),
                        ),
                        "key_pests": list(with_default("key_pests", key_pests, [])),
                        "key_diseases": list(with_default("key_diseases", key_diseases, [])),
                        "pre_flight_checks": list(with_default("pre_flight_checks", pre_flight_checks, [])),
                    }

                    return SahelianCropProfile.model_validate(payload)

            if deferred_backend_error is not None:
                raise deferred_backend_error
        except CropProfileNotFoundError:
            raise
        except CropProfileBackendUnavailableError:
            raise
        except ValidationError as exc:
            logger.error("Crop profile validation error for crop=%s zone=%s: %s", crop, zone, exc)
            raise CropProfileValidationError(f"Invalid crop profile payload for crop='{crop}' zone='{zone}'") from exc
        except DBAPIError as exc:
            # DB connection/config errors should not spam stack traces in local Gradio runs.
            logger.warning("DB error fetching crop profile for crop=%s zone=%s: %s", crop, zone, exc)
            raise
        except Exception as exc:
            logger.warning("Async error fetching crop profile for crop=%s zone=%s: %s", crop, zone, exc)
            raise

    async def get_technical_sheet(self, crop: str, zone: str) -> str:
        """Récupère la fiche technique pour une culture donnée (Via DB)."""
        try:
            p = await self._get_profile_from_db(crop, zone)
        except CropProfileNotFoundError:
            return f"Culture '{crop}' non répertoriée en base INERA."
        
        # Adaptation de l'affichage legacy si besoin, ou renvoi vers Advisor
        # Pour l'instant on garde le format texte simple
        vars_zone = p.varieties.get(zone.capitalize(), [p.name])
        
        return (
            f"📍 **FICHE TECHNIQUE : {p.name.upper()} ({zone.upper()})**\n"
            f"--- \n"
            f"🧬 **Variété :** {', '.join(vars_zone)}\n"
            f"⏱️ **Cycle :** {p.cycle_days} jours\n"
            f"📏 **Semis :** {p.sowing_config.inter_row}cm x {p.sowing_config.inter_plant}cm\n"
            f"💩 **Fumure Orga :** {p.organic_matter_min_tha} t/ha\n"
            f"💧 **Eau :** {p.water_strategy}"
        )

    async def calculate_inputs(self, crop: str, surface_ha: float) -> Dict[str, Any]:
        """Calcule les intrants nécessaires (Via DB logic)."""
        # On suppose une zone par défaut ou on fait une moyenne si zone inconnue
        # Pour simplifier, on prend le premier profil trouvé
        try:
            p = await self._get_profile_from_db(crop, "Centre")
        except CropProfileNotFoundError:
            return {}
        
        inputs = {}
        if p.fertilizer_plan:
            for item in p.fertilizer_plan:
                product_type = item.type or "Engrais"
                dose = float(item.dose_kg_ha) * float(surface_ha)
                # On regroupe par famille simplifié si besoin
                key = f"{product_type}_kg"
                inputs[key] = inputs.get(key, 0) + dose
                
        # Conversion primitive sacs (si on reconnait NPK/Urée)
        return inputs
    
    def get_math_profile(self, crop: str) -> Optional[CropProfile]:
        return self.MATH_PROFILES.get(crop.lower())
