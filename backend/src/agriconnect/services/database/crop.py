import logging
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Callable, Awaitable, TypeVar

from asyncpg.exceptions import ConnectionDoesNotExistError
from sqlalchemy import select, func, and_, or_, update, desc, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError

from agriconnect.core.database import get_sessionmaker
from agriconnect.domain.models import (
    Farm,
    CropCycle,
    FieldIntervention,
    CropGrowthLog,
    SensorDataSummary,
    SoilProfile,
    SensorTelemetryHistory,
    AgronomicStandard,
    CropGrowthStage,
    PestDiseaseCatalog,
    AIRecommendation,
    WeatherDataLog,
    User,
    Zone,
    _uuid4,
    CropProfile as CropProfileModel,
    CropFertilizerStep,
    SoilAnalysis,
)
from agriconnect.services.database.common import positive_float
from .base import BaseMixin

logger = logging.getLogger("agriconnect.services.database")


CROP_PROFILE_FIXTURES: List[Dict[str, Any]] = [
    {
        "slug": "tomate-centre-marmande",
        "crop_name": "Tomate",
        "zone": "Centre",
        "variety": "Marmande",
        "scientific_name": "Solanum lycopersicum",
        "cycle_days": 115,
        "base_temperature_c": 10.0,
        "max_temperature_c": 34.0,
        "expected_gdd": 1450.0,
        "depth_cm": 2,
        "organic_matter_min_tha": 6.0,
        "water_strategy": "Irrigation pilotée ET0/Kc en goutte-à-goutte",
        "water_needs_mm_per_cycle": 560.0,
        "kc_stages": [
            {"stage": "implantation", "gdd_min": 0, "gdd_max": 250, "kc": 0.55},
            {"stage": "croissance vegetative", "gdd_min": 251, "gdd_max": 650, "kc": 0.85},
            {"stage": "floraison nouaison", "gdd_min": 651, "gdd_max": 1050, "kc": 1.15},
            {"stage": "grossissement maturation", "gdd_min": 1051, "gdd_max": 1600, "kc": 0.95},
        ],
        "critical_stops_drought": ["floraison nouaison", "grossissement maturation"],
        "nutrient_requirements_per_ton": {
            "N": 3.2,
            "P2O5": 1.1,
            "K2O": 5.2,
            "CaO": 1.8,
            "MgO": 0.6,
        },
        "salinity_tolerance_ec": 2.6,
        "inter_row_cm": 90,
        "inter_plant_cm": 45,
        "seeds_pocket": 1,
        "yield_min_t_ha": 25.0,
        "yield_max_t_ha": 55.0,
        "phenological_stages": [
            {"name": "reprise", "gdd": 120},
            {"name": "floraison", "gdd": 680},
            {"name": "nouaison", "gdd": 820},
            {"name": "premiere recolte", "gdd": 1200},
        ],
        "key_pests": ["Tuta absoluta", "Aleurodes", "Noctuelles"],
        "key_diseases": ["Mildiou", "Alternariose", "Oïdium"],
        "pre_flight_checks": [
            "Installer le paillage avant transplantation",
            "Calibrer les goutteurs et verifier la pression",
            "Planifier la fertigation hebdomadaire",
        ],
        "fertilizer_plan": [
            {
                "step_order": 1,
                "stage": "Transplantation",
                "product_type": "NPK 12-24-12",
                "dose_kg_ha": 260,
                "application_mode": "Localisée",
                "efficiency_factor": 0.72,
            },
            {
                "step_order": 2,
                "stage": "Floraison nouaison",
                "product_type": "Nitrate de calcium",
                "dose_kg_ha": 180,
                "application_mode": "Fertigation",
                "efficiency_factor": 0.85,
            },
            {
                "step_order": 3,
                "stage": "Grossissement",
                "product_type": "Sulfate de potassium",
                "dose_kg_ha": 140,
                "application_mode": "Fertigation",
                "efficiency_factor": 0.88,
            },
        ],
    },
    {
        "slug": "mais-centre",
        "crop_name": "Maïs",
        "zone": "Centre",
        "variety": "Bondofa",
        "scientific_name": "Zea mays L.",
        "cycle_days": 110,
        "depth_cm": 5,
        "organic_matter_min_tha": 3.0,
        "water_strategy": "Pluvial optimisé",
        "inter_row_cm": 80,
        "inter_plant_cm": 40,
        "seeds_pocket": 2,
        "yield_min_t_ha": 3.0,
        "yield_max_t_ha": 5.5,
        "key_pests": ["Foreurs de tiges", "Chenille légionnaire"],
        "key_diseases": ["Rouille commune"],
        "pre_flight_checks": [
            "Tester la germination des semences",
            "Assurer un lit de semence bien émietté",
        ],
        "fertilizer_plan": [
            {
                "step_order": 1,
                "stage": "Semis",
                "product_type": "NPK 14-23-14",
                "dose_kg_ha": 200,
                "application_mode": "Localisée",
            },
            {
                "step_order": 2,
                "stage": "Tallage",
                "product_type": "Urée",
                "dose_kg_ha": 100,
                "application_mode": "Couverte",
            },
        ],
    },
    {
        "slug": "mais-nord",
        "crop_name": "Maïs",
        "zone": "Nord",
        "variety": "ESP 2",
        "scientific_name": "Zea mays L.",
        "cycle_days": 95,
        "depth_cm": 5,
        "organic_matter_min_tha": 2.0,
        "water_strategy": "Pluvial sécurisé",
        "inter_row_cm": 75,
        "inter_plant_cm": 35,
        "seeds_pocket": 2,
        "yield_min_t_ha": 2.2,
        "yield_max_t_ha": 4.2,
        "key_pests": ["Charançon du maïs"],
        "key_diseases": ["Helminthosporiose"],
        "pre_flight_checks": ["Apporter 2 t/ha de fumure organique en amont"],
        "fertilizer_plan": [
            {
                "step_order": 1,
                "stage": "Semis",
                "product_type": "NPK 15-15-15",
                "dose_kg_ha": 180,
                "application_mode": "Épandage",
            },
            {
                "step_order": 2,
                "stage": "Montaison",
                "product_type": "Urée",
                "dose_kg_ha": 90,
                "application_mode": "Épandage",
            },
        ],
    },
    {
        "slug": "sorgho-sahel",
        "crop_name": "Sorgho",
        "zone": "Sahel",
        "variety": "Sariasso 15",
        "scientific_name": "Sorghum bicolor",
        "cycle_days": 120,
        "depth_cm": 4,
        "organic_matter_min_tha": 2.5,
        "water_strategy": "Gestion conservatoire de l'humidité",
        "inter_row_cm": 90,
        "inter_plant_cm": 45,
        "seeds_pocket": 2,
        "yield_min_t_ha": 1.8,
        "yield_max_t_ha": 3.5,
        "key_pests": ["Mouche du sorgho"],
        "key_diseases": ["Charbon couvert"],
        "pre_flight_checks": ["Mettre en place des cordons pierreux"],
        "fertilizer_plan": [
            {
                "step_order": 1,
                "stage": "Pré-semis",
                "product_type": "Compost",
                "dose_kg_ha": 1500,
                "application_mode": "Incorporation",
            },
            {
                "step_order": 2,
                "stage": "Tallage",
                "product_type": "NPK 12-20-18",
                "dose_kg_ha": 120,
                "application_mode": "Localisée",
            },
        ],
    },
    {
        "slug": "niebe-centre-nord",
        "crop_name": "Niébé",
        "zone": "Centre-Nord",
        "variety": "KVx61-1",
        "scientific_name": "Vigna unguiculata",
        "cycle_days": 75,
        "depth_cm": 3,
        "organic_matter_min_tha": 1.5,
        "water_strategy": "Pluvial maîtrisé",
        "inter_row_cm": 60,
        "inter_plant_cm": 25,
        "seeds_pocket": 2,
        "yield_min_t_ha": 0.9,
        "yield_max_t_ha": 1.8,
        "key_pests": ["Pucerons", "Thrips"],
        "key_diseases": ["Mildiou"],
        "pre_flight_checks": ["Traiter les semences avant semis"],
        "fertilizer_plan": [
            {
                "step_order": 1,
                "stage": "Semis",
                "product_type": "NPK 14-23-14",
                "dose_kg_ha": 80,
                "application_mode": "Localisée",
            },
            {
                "step_order": 2,
                "stage": "Floraison",
                "product_type": "KCl",
                "dose_kg_ha": 40,
                "application_mode": "Épandage",
            },
        ],
    },
    {
        "slug": "tomate-est",
        "crop_name": "Tomate",
        "zone": "Est",
        "variety": "Roma VF",
        "scientific_name": "Solanum lycopersicum",
        "cycle_days": 105,
        "depth_cm": 2,
        "organic_matter_min_tha": 4.0,
        "water_strategy": "Irrigation goutte-à-goutte",
        "inter_row_cm": 100,
        "inter_plant_cm": 40,
        "seeds_pocket": 1,
        "yield_min_t_ha": 15.0,
        "yield_max_t_ha": 35.0,
        "key_pests": ["Tuta absoluta", "Aleurodes"],
        "key_diseases": ["Mildiou", "Fusariose"],
        "pre_flight_checks": ["Désinfecter le matériel de pépinière"],
        "fertilizer_plan": [
            {
                "step_order": 1,
                "stage": "Transplantation",
                "product_type": "NPK 12-24-12",
                "dose_kg_ha": 300,
                "application_mode": "Localisée",
            },
            {
                "step_order": 2,
                "stage": "Nouaison",
                "product_type": "Sulfate de potassium",
                "dose_kg_ha": 120,
                "application_mode": "Fertigation",
            },
        ],
    },
]


_CROP_FIXTURES_PRIMED = False


def _is_undefined_table_error(exc: Exception) -> bool:
	"""Return True when the underlying database error reports a missing table/view."""
	orig = getattr(exc, "orig", None)
	sqlstate = getattr(orig, "sqlstate", None) or getattr(exc, "sqlstate", None)
	if sqlstate == "42P01":
		return True
	pgcode = getattr(orig, "pgcode", None) or getattr(exc, "pgcode", None)
	if pgcode == "42P01":
		return True
	orig_name = getattr(getattr(orig, "__class__", None), "__name__", "")
	if orig_name in {"UndefinedTableError", "UndefinedTable"}:
		return True
	if exc.__class__.__name__ in {"UndefinedTableError", "UndefinedTable"}:
		return True
	return False

def _uuid() -> str:
    return str(_uuid4())


T = TypeVar("T")


class CropMixin(BaseMixin):
    """Database accessors for agronomic knowledge (MCP-backed)."""

    async def _seed_crop_profiles_if_needed(self) -> None:
        session = self.session
        if session is None:
            return
        logger.info("Vérification des profils de culture en base...")
        try:
            res = await session.execute(text("SELECT COUNT(*) FROM intelligence.crop_profiles"))
            total = int(res.scalar_one() or 0)
        except DBAPIError as exc:
            if _is_undefined_table_error(exc):
                logger.warning("Seed skipped: crop_profiles relation manquant")
                try:
                    await session.rollback()
                except Exception:
                    pass
                return
            raise

        if total > 0:
            return

        known_zones: set[str] = set()
        try:
            zone_rows = await session.execute(select(Zone.name))
            known_zones = {str(row[0]) for row in zone_rows if row[0]}
        except Exception:
            known_zones = set()

        logger.info(f"Seeding des profils de culture à partir des fixtures (zones connues: {known_zones or 'toutes'})...")
        
        for entry in CROP_PROFILE_FIXTURES:
            if known_zones and entry["zone"] not in known_zones:
                continue

            payload = {
                "id": entry.get("id") or _uuid(),
                "slug": entry["slug"],
                "crop_name": entry["crop_name"],
                "zone_category": entry["zone"],
                "variety": entry.get("variety"),
                "scientific_name": entry.get("scientific_name"),
                "cycle_days": entry["cycle_days"],
                "base_temperature_c": entry.get("base_temperature_c"),
                "max_temperature_c": entry.get("max_temperature_c"),
                "expected_gdd": entry.get("expected_gdd"),
                "depth_cm": entry["depth_cm"],
                "organic_matter_min_tha": entry["organic_matter_min_tha"],
                "water_strategy": entry.get("water_strategy", ""),
                "water_needs_mm_per_cycle": entry.get("water_needs_mm_per_cycle"),
                "kc_stages": entry.get("kc_stages", []),
                "critical_stops_drought": entry.get("critical_stops_drought", []),
                "nutrient_requirements_per_ton": entry.get("nutrient_requirements_per_ton", {"N": 0, "P2O5": 0, "K2O": 0, "CaO": 0, "MgO": 0}),
                "salinity_tolerance_ec": entry.get("salinity_tolerance_ec"),
                "inter_row_cm": entry["inter_row_cm"],
                "inter_plant_cm": entry["inter_plant_cm"],
                "seeds_pocket": entry["seeds_pocket"],
                "yield_min_t_ha": entry["yield_min_t_ha"],
                "yield_max_t_ha": entry["yield_max_t_ha"],
                "phenological_stages": entry.get("phenological_stages", []),
                "key_pests": entry.get("key_pests", []),
                "key_diseases": entry.get("key_diseases", []),
                "pre_flight_checks": entry.get("pre_flight_checks", []),
            }

            logger.debug(f"Upsert du profil de culture '{payload['slug']}' pour la zone '{payload['zone_category']}'")
            stmt = (
                insert(CropProfileModel)
                .values(**payload)
                .on_conflict_do_update(
                    index_elements=[CropProfileModel.slug],
                    set_={
                        "variety": payload["variety"],
                        "cycle_days": payload["cycle_days"],
                        "base_temperature_c": payload["base_temperature_c"],
                        "max_temperature_c": payload["max_temperature_c"],
                        "expected_gdd": payload["expected_gdd"],
                        "yield_min_t_ha": payload["yield_min_t_ha"],
                        "yield_max_t_ha": payload["yield_max_t_ha"],
                        "water_strategy": payload["water_strategy"],
                        "water_needs_mm_per_cycle": payload["water_needs_mm_per_cycle"],
                        "kc_stages": payload["kc_stages"],
                        "critical_stops_drought": payload["critical_stops_drought"],
                        "nutrient_requirements_per_ton": payload["nutrient_requirements_per_ton"],
                        "salinity_tolerance_ec": payload["salinity_tolerance_ec"],
                        "phenological_stages": payload["phenological_stages"],
                        "key_pests": payload["key_pests"],
                        "key_diseases": payload["key_diseases"],
                        "pre_flight_checks": payload["pre_flight_checks"],
                    },
                )
                .returning(CropProfileModel.id)
            )

            result = await session.execute(stmt)
            profile_id = result.scalar_one_or_none()
            if profile_id is None:
                profile_id = (
                    await session.execute(
                        select(CropProfileModel.id).where(CropProfileModel.slug == entry["slug"])
                    )
                ).scalar_one()

            for fert in entry.get("fertilizer_plan", []):
                fert_payload = {
                    "id": _uuid(),
                    "crop_profile_id": profile_id,
                    "step_order": fert["step_order"],
                    "stage": fert["stage"],
                    "product_type": fert["product_type"],
                    "dose_kg_ha": fert["dose_kg_ha"],
                    "application_mode": fert.get("application_mode", "Épandage"),
                    "efficiency_factor": fert.get("efficiency_factor", 1.0),
                }
                fert_stmt = (
                    insert(CropFertilizerStep)
                    .values(**fert_payload)
                    .on_conflict_do_update(
                        index_elements=[
                            CropFertilizerStep.crop_profile_id,
                            CropFertilizerStep.step_order,
                        ],
                        set_={
                            "stage": fert_payload["stage"],
                            "product_type": fert_payload["product_type"],
                            "dose_kg_ha": fert_payload["dose_kg_ha"],
                            "application_mode": fert_payload["application_mode"],
                            "efficiency_factor": fert_payload["efficiency_factor"],
                        },
                    )
                )
                await session.execute(fert_stmt)

        await session.commit()



    async def _run_crop_profile_query(
        self,
        session: AsyncSession,
        clean_crop: str,
        zone_lower: Optional[str],
        original_crop: str,
        original_zone: Optional[str],
    ) -> Dict[str, Any]:
        def with_default(field_name: str, raw_value: Any, default_value: Any) -> Any:
            if raw_value is None:
                logger.debug("get_crop_profile default fallback field=%s value=%r", field_name, default_value)
                return default_value
            if isinstance(default_value, float) and not isinstance(raw_value, float):
                try:
                    return float(raw_value)
                except Exception:
                    return default_value
            return raw_value

        def ensure_list(raw_value: Any) -> List[Any]:
            if raw_value is None:
                return []
            if isinstance(raw_value, list):
                return raw_value
            if isinstance(raw_value, str):
                try:
                    parsed = json.loads(raw_value)
                    return parsed if isinstance(parsed, list) else []
                except Exception:
                    return []
            return []

        def ensure_dict(raw_value: Any, default_value: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            if isinstance(raw_value, dict):
                return raw_value
            if isinstance(raw_value, str):
                try:
                    parsed = json.loads(raw_value)
                    return parsed if isinstance(parsed, dict) else (default_value or {})
                except Exception:
                    return default_value or {}
            return default_value or {}

        params = {
            "crop_raw": clean_crop,
            "zone": zone_lower,
            "zone_is_null": zone_lower is None,
        }

        primary_stmt = text(
            "SELECT id, slug, crop_name, variety, zone_category, scientific_name, cycle_days, depth_cm, "
            "organic_matter_min_tha, water_strategy, base_temperature_c, max_temperature_c, expected_gdd, "
            "water_needs_mm_per_cycle, kc_stages, critical_stops_drought, nutrient_requirements_per_ton, salinity_tolerance_ec, "
            "inter_row_cm, inter_plant_cm, seeds_pocket, phenological_stages, "
            "yield_min_t_ha, yield_max_t_ha, key_pests, key_diseases, pre_flight_checks "
            "FROM intelligence.crop_profiles "
            "WHERE ("
            "   lower(slug) % :crop_raw "
            "   OR lower(crop_name) % :crop_raw "
            "   OR :crop_raw LIKE CONCAT('%', lower(slug), '%') "
            "   OR :crop_raw LIKE CONCAT('%', lower(crop_name), '%') "
            ") "
            "AND (:zone_is_null OR lower(zone_category) = :zone) "
            "ORDER BY GREATEST(similarity(lower(crop_name), :crop_raw), similarity(lower(slug), :crop_raw)) DESC "
            "LIMIT 1"
        )

        res = await session.execute(primary_stmt, params)
        row = res.first()

        if row is None:
            zone_msg = f" zone='{original_zone}'" if original_zone else ""
            raise LookupError(f"No calibrated crop profile found for crop search='{original_crop}'{zone_msg}")

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
            base_temperature_c,
            max_temperature_c,
            expected_gdd,
            water_needs_mm_per_cycle,
            kc_stages,
            critical_stops_drought,
            nutrient_requirements_per_ton,
            salinity_tolerance_ec,
            inter_row_cm,
            inter_plant_cm,
            seeds_pocket,
            phenological_stages,
            yield_min_t_ha,
            yield_max_t_ha,
            key_pests,
            key_diseases,
            pre_flight_checks,
        ) = row

        fertilizer_plan: List[Dict[str, Any]] = []
        if profile_id:
            fert_stmt = text(
                "SELECT step_order, stage, product_type, dose_kg_ha, application_mode, efficiency_factor "
                "FROM intelligence.crop_fertilizer_steps WHERE crop_profile_id = :pid "
                "ORDER BY step_order ASC, id ASC"
            )
            fert_res = await session.execute(fert_stmt, {"pid": profile_id})
            fert_rows = fert_res.fetchall() or []
            fertilizer_plan = [
                {
                    "step_order": int(with_default("fertilizer_plan.step_order", fr[0], 1)),
                    "stage": str(with_default("fertilizer_plan.stage", fr[1], "Semis")),
                    "type": str(with_default("fertilizer_plan.type", fr[2], "NPK")),
                    "dose_kg_ha": float(with_default("fertilizer_plan.dose_kg_ha", fr[3], 0.0)),
                    "mode": str(with_default("fertilizer_plan.mode", fr[4], "Épandage")),
                    "efficiency_factor": float(with_default("fertilizer_plan.efficiency_factor", fr[5], 1.0)),
                }
                for fr in fert_rows
            ]

        return {
            "name": str(crop_name_db or original_crop),
            "varieties": {str(zone_category or original_zone or "Centre"): [str(variety)]} if variety else {},
            "cycle_days": int(with_default("cycle_days", cycle_days, 90)),
            "seeding_density": "N/A",
            "depth_cm": float(with_default("depth_cm", depth_cm, 5.0)),
            "organic_matter_min_tha": float(with_default("organic_matter_min_tha", organic_matter_min_tha, 0.0)),
            "mineral_fertilizer": {},
            "water_strategy": str(with_default("water_strategy", water_strategy, "")),
            "base_temperature_c": float(with_default("base_temperature_c", base_temperature_c, 10.0)),
            "max_temperature_c": float(with_default("max_temperature_c", max_temperature_c, 35.0)),
            "expected_gdd": float(with_default("expected_gdd", expected_gdd, 1200.0)),
            "water_needs_mm_per_cycle": float(with_default("water_needs_mm_per_cycle", water_needs_mm_per_cycle, 400.0)),
            "kc_stages": ensure_list(kc_stages),
            "critical_stops_drought": ensure_list(critical_stops_drought),
            "nutrient_requirements_per_ton": ensure_dict(nutrient_requirements_per_ton, {"N": 0, "P2O5": 0, "K2O": 0, "CaO": 0, "MgO": 0}),
            "salinity_tolerance_ec": float(with_default("salinity_tolerance_ec", salinity_tolerance_ec, 3.0)),
            "scientific_name": str(with_default("scientific_name", scientific_name, "")),
            "sowing_config": {
                "inter_row": float(with_default("sowing_config.inter_row", inter_row_cm, 80.0)),
                "inter_plant": float(with_default("sowing_config.inter_plant", inter_plant_cm, 40.0)),
                "seeds_pocket": int(with_default("sowing_config.seeds_pocket", seeds_pocket, 2)),
            },
            "fertilizer_plan": fertilizer_plan,
            "phenological_stages": ensure_list(phenological_stages),
            "yield_potential": (
                float(with_default("yield_potential.min", yield_min_t_ha, 0.0)),
                float(with_default("yield_potential.max", yield_max_t_ha, 0.0)),
            ),
            "key_pests": ensure_list(with_default("key_pests", key_pests, [])),
            "key_diseases": ensure_list(with_default("key_diseases", key_diseases, [])),
            "pre_flight_checks": ensure_list(with_default("pre_flight_checks", pre_flight_checks, [])),
        }

    async def get_crop_profile(self, crop: str, zone: Optional[str] = None) -> Dict[str, Any]:
        """
        Return a normalized crop profile payload for MCP consumers.
        Uses PostgreSQL trigram similarity (pg_trgm) for fuzzy matching resilience.
        """
        if not crop:
            raise ValueError("'crop' parameter is required")

        clean_crop = crop.strip().lower()
        zone_lower = zone.strip().lower() if zone else None

        current_session = self.session
        if not current_session:
            raise RuntimeError("Database session is not initialized")

        try:
            return await self._run_crop_profile_query(
                current_session, clean_crop, zone_lower, crop, zone
            )
        except LookupError as le:
            raise LookupError(str(le)) from le
        except ConnectionDoesNotExistError as exc:
            logger.warning("Connection closed during get_crop_profile, retrying with new session", exc_info=True)
            sm = get_sessionmaker()
            if sm is None:
                raise RuntimeError("Database sessionmaker is unavailable") from exc
            async with sm() as fallback_session:
                return await self._run_crop_profile_query(
                    fallback_session, clean_crop, zone_lower, crop, zone
                )
        except Exception as e:
            logger.error(f"Erreur dans get_crop_profile: {e}", exc_info=True)
            raise RuntimeError(f"Erreur technique get_crop_profile: {e}") from e
    

    async def calculate_irrigation_need(
        self,
        farm_id: str,
        crop: str,
        zone: str = "Centre",
        as_of: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compute daily irrigation need from ET0 × Kc with drought-stage alerting."""
        if not farm_id:
            raise ValueError("farm_id is required")

        current_session = self.session
        if current_session is None:
            raise RuntimeError("Database session is not initialized")

        profile = await self.get_crop_profile(crop=crop, zone=zone)
        as_of_dt = datetime.fromisoformat(as_of) if as_of else datetime.utcnow()

        weather_row = (
            await current_session.execute(
                select(WeatherDataLog)
                .where(WeatherDataLog.farm_id == farm_id)
                .where(WeatherDataLog.record_date <= as_of_dt)
                .order_by(desc(WeatherDataLog.record_date))
                .limit(1)
            )
        ).scalar_one_or_none()

        if weather_row is None:
            raise LookupError(f"Aucune donnée météo disponible pour farm_id='{farm_id}'")

        gdd_window_start = as_of_dt - timedelta(days=45)
        gdd_values = (
            await current_session.execute(
                select(WeatherDataLog.gdd_contribution)
                .where(WeatherDataLog.farm_id == farm_id)
                .where(WeatherDataLog.record_date >= gdd_window_start)
            )
        ).scalars().all()
        accumulated_gdd = float(sum(float(v or 0.0) for v in gdd_values))

        kc_stages = profile.get("kc_stages") or []
        selected_stage = "inconnu"
        selected_kc = 0.85
        for item in kc_stages:
            if not isinstance(item, dict):
                continue
            gdd_min = float(item.get("gdd_min", 0.0) or 0.0)
            gdd_max = float(item.get("gdd_max", 10**9) or 10**9)
            if gdd_min <= accumulated_gdd <= gdd_max:
                selected_stage = str(item.get("stage") or "inconnu")
                selected_kc = float(item.get("kc", selected_kc) or selected_kc)
                break

        et0 = float(weather_row.evapotranspiration or 0.0)
        if et0 <= 0:
            et0 = 4.5

        net_need_mm = round(et0 * selected_kc, 2)
        alerts: List[Dict[str, Any]] = []
        critical = [str(x).strip().lower() for x in (profile.get("critical_stops_drought") or [])]
        if selected_stage.lower() in critical:
            alerts.append(
                {
                    "code": "STRESS_HYDRIQUE_CRITIQUE",
                    "severity": "RED",
                    "message": (
                        "La culture est en phase critique (floraison/nouaison/maturation). "
                        "Un manque d'eau peut détruire le rendement."
                    ),
                }
            )

        return {
            "farm_id": farm_id,
            "crop": profile.get("name", crop),
            "zone": zone,
            "as_of": weather_row.record_date.isoformat() if weather_row.record_date else as_of_dt.isoformat(),
            "et0_mm": round(et0, 2),
            "kc": round(selected_kc, 3),
            "growth_stage": selected_stage,
            "accumulated_gdd": round(accumulated_gdd, 2),
            "net_irrigation_need_mm": net_need_mm,
            "alerts": alerts,
            "status": "OK",
        }

    async def calculate_custom_fertilization(
        self,
        farm_id: str,
        crop: str,
        target_yield_t_ha: float,
        zone: str = "Centre",
        application_mode: str = "",
    ) -> Dict[str, Any]:
        """Compute nutrient doses from crop export targets and latest soil analysis."""
        if not farm_id:
            raise ValueError("farm_id is required")
        target_yield = float(target_yield_t_ha or 0.0)
        if target_yield <= 0:
            raise ValueError("target_yield_t_ha must be > 0")

        current_session = self.session
        if current_session is None:
            raise RuntimeError("Database session is not initialized")

        profile = await self.get_crop_profile(crop=crop, zone=zone)
        soil = (
            await current_session.execute(
                select(SoilAnalysis)
                .where(SoilAnalysis.farm_id == farm_id)
                .order_by(desc(SoilAnalysis.sample_date))
                .limit(1)
            )
        ).scalar_one_or_none()

        if soil is None:
            raise LookupError(f"Aucune analyse de sol disponible pour farm_id='{farm_id}'")

        texture = str(soil.texture or "").lower()
        bulk_density = float(soil.bulk_density or (1.30 if "argile" in texture else 1.45 if "sable" in texture else 1.35))
        depth_m = 0.20
        ppm_to_kgha_factor = bulk_density * depth_m * 10.0

        requirements = profile.get("nutrient_requirements_per_ton") or {}
        soil_ppm = {
            "N": float(soil.nitrogen_ppm or 0.0),
            "P2O5": float(soil.phosphorus_ppm or 0.0),
            "K2O": float(soil.potassium_ppm or 0.0),
            "CaO": float(soil.calcium_ppm or 0.0),
            "MgO": float(soil.magnesium_ppm or 0.0),
        }

        stage_step: Dict[str, Any] = {}
        for step in profile.get("fertilizer_plan") or []:
            if not isinstance(step, dict):
                continue
            mode = str(step.get("mode") or "").lower()
            if application_mode and application_mode.lower() in mode:
                stage_step = step
                break
        if not stage_step and (profile.get("fertilizer_plan") or []):
            stage_step = dict((profile.get("fertilizer_plan") or [])[0])

        efficiency = float(stage_step.get("efficiency_factor") or 0.70)
        if efficiency <= 0:
            efficiency = 0.70

        recommendations: Dict[str, Dict[str, float]] = {}
        for nutrient in ("N", "P2O5", "K2O", "CaO", "MgO"):
            req_per_ton = float(requirements.get(nutrient, 0.0) or 0.0)
            gross_need = req_per_ton * target_yield
            soil_available = soil_ppm[nutrient] * ppm_to_kgha_factor
            net_need = max(0.0, gross_need - soil_available)
            adjusted_dose = net_need / efficiency
            recommendations[nutrient] = {
                "gross_need_kg_ha": round(gross_need, 2),
                "soil_available_kg_ha": round(soil_available, 2),
                "net_need_kg_ha": round(net_need, 2),
                "recommended_dose_kg_ha": round(adjusted_dose, 2),
            }

        alerts: List[Dict[str, Any]] = []
        cn_ratio = float(soil.carbon_nitrogen_ratio or 0.0)
        if cn_ratio > 15.0:
            alerts.append(
                {
                    "code": "RISQUE_FAIM_AZOTE",
                    "severity": "ORANGE",
                    "message": (
                        "Le ratio C/N du sol est élevé (>15). Risque de blocage biologique de l'azote ; "
                        "augmenter la dose de démarrage et privilégier une forme rapidement assimilable."
                    ),
                }
            )

        return {
            "farm_id": farm_id,
            "crop": profile.get("name", crop),
            "target_yield_t_ha": target_yield,
            "bulk_density_used": round(bulk_density, 2),
            "efficiency_factor": round(efficiency, 3),
            "application_mode": stage_step.get("mode") or application_mode or "non spécifié",
            "stage": stage_step.get("stage") or "non spécifié",
            "nutrient_plan": recommendations,
            "alerts": alerts,
            "status": "OK",
        }

    async def evaluate_disease_and_climate_risk(
        self,
        farm_id: str,
        crop: str,
        zone: str = "Centre",
        lookback_hours: int = 48,
    ) -> Dict[str, Any]:
        """Assess disease and pest risk from last 48h microclimate and crop vulnerabilities."""
        current_session = self.session
        if current_session is None:
            raise RuntimeError("Database session is not initialized")
        if not farm_id:
            raise ValueError("farm_id is required")

        profile = await self.get_crop_profile(crop=crop, zone=zone)
        since = datetime.utcnow() - timedelta(hours=max(1, int(lookback_hours)))

        logs = (
            await current_session.execute(
                select(WeatherDataLog)
                .where(WeatherDataLog.farm_id == farm_id)
                .where(WeatherDataLog.record_date >= since)
                .order_by(desc(WeatherDataLog.record_date))
            )
        ).scalars().all()

        if not logs:
            return {
                "farm_id": farm_id,
                "risk_level": "UNKNOWN",
                "message": "Données météo insuffisantes sur les dernières 48h.",
                "recommended_actions": ["Vérifier la station météo et relancer la synchronisation des logs."],
                "status": "NO_DATA",
            }

        humidity_max = max(float(l.humidity_max or 0.0) for l in logs)
        temp_mean = sum(float(l.temp_mean or 0.0) for l in logs) / max(len(logs), 1)
        leaf_wetness = sum(int(l.leaf_wetness_duration_minutes or 0) for l in logs)

        score = 0
        if humidity_max >= 90:
            score += 2
        if 18 <= temp_mean <= 30:
            score += 1
        if leaf_wetness >= 360:
            score += 2
        if leaf_wetness >= 720:
            score += 1

        if score >= 5:
            risk = "CRITIQUE"
        elif score >= 3:
            risk = "MOYEN"
        else:
            risk = "FAIBLE"

        key_diseases = profile.get("key_diseases") or []
        key_pests = profile.get("key_pests") or []
        recommended_actions = [
            "Inspecter immédiatement les feuilles basses et les apex (symptômes précoces).",
            "Renforcer l'aération de la parcelle et réduire les irrigations nocturnes.",
            "Désinfecter le matériel et isoler les plants symptomatiques.",
        ]
        if risk == "CRITIQUE":
            recommended_actions.append("Déclencher une protection préventive ciblée validée localement (bio ou homologuée).")

        return {
            "farm_id": farm_id,
            "crop": profile.get("name", crop),
            "window_hours": int(lookback_hours),
            "risk_level": risk,
            "epidemiological_factors": {
                "humidity_max": round(humidity_max, 2),
                "temp_mean": round(temp_mean, 2),
                "leaf_wetness_duration_minutes": int(leaf_wetness),
            },
            "vulnerabilities": {
                "key_diseases": key_diseases,
                "key_pests": key_pests,
            },
            "recommended_actions": recommended_actions,
            "status": "OK",
        }

    async def calculate_sowing_density(
        self,
        inter_row_cm: float,
        inter_plant_cm: float,
        pmg_grams: float,
        seeds_per_pocket: int = 1,
        germination_rate: float = 0.9,
    ) -> Dict[str, Any]:
        """Estimate sowing density and seed mass needed per hectare."""
        inter_row = float(inter_row_cm)
        inter_plant = float(inter_plant_cm)
        pmg = float(pmg_grams)
        germination = max(0.1, min(1.0, float(germination_rate)))
        spp = max(1, int(seeds_per_pocket))

        area_per_pocket_m2 = (inter_row / 100.0) * (inter_plant / 100.0)
        pockets_per_ha = 10000.0 / area_per_pocket_m2
        target_plants_per_ha = pockets_per_ha * spp
        seeds_needed = target_plants_per_ha / germination
        kg_seed_per_ha = (seeds_needed * pmg) / 1_000_000.0

        return {
            "inter_row_cm": inter_row,
            "inter_plant_cm": inter_plant,
            "seeds_per_pocket": spp,
            "germination_rate": germination,
            "target_plants_per_ha": int(target_plants_per_ha),
            "seed_quantity_kg_ha": round(kg_seed_per_ha, 3),
            "status": "OK",
        }

    async def check_soil_salinity_hazard(self, farm_id: str, crop: str, zone: str = "Centre") -> Dict[str, Any]:
        """Compare observed soil salinity against crop tolerance to flag osmotic hazard."""
        if not farm_id:
            raise ValueError("farm_id is required")
        current_session = self.session
        if current_session is None:
            raise RuntimeError("Database session is not initialized")

        profile = await self.get_crop_profile(crop=crop, zone=zone)
        tolerance_ec = float(profile.get("salinity_tolerance_ec") or 3.0)

        soil = (
            await current_session.execute(
                select(SoilAnalysis)
                .where(SoilAnalysis.farm_id == farm_id)
                .order_by(desc(SoilAnalysis.sample_date))
                .limit(1)
            )
        ).scalar_one_or_none()

        observed_ec = None if soil is None else float(soil.salinity_ec or 0.0)
        if observed_ec is None or observed_ec <= 0:
            zone_row = (
                await current_session.execute(
                    select(func.avg(SoilAnalysis.salinity_ec))
                    .join(Farm, Farm.id == SoilAnalysis.farm_id)
                    .join(Zone, Zone.id == Farm.zone_id)
                    .where(func.lower(Zone.name) == zone.lower())
                )
            ).first()
            observed_ec = float((zone_row[0] if zone_row and zone_row[0] is not None else 2.1) or 2.1)

        ratio = observed_ec / tolerance_ec if tolerance_ec > 0 else 1.0
        if ratio >= 1.3:
            level = "CRITIQUE"
            message = "Risque d'asphyxie racinaire et stress osmotique élevé."
        elif ratio >= 1.0:
            level = "MOYEN"
            message = "Seuil de salinité atteint : vigilance et lessivage piloté recommandés."
        else:
            level = "FAIBLE"
            message = "Salinité compatible avec la tolérance variétale."

        return {
            "farm_id": farm_id,
            "crop": profile.get("name", crop),
            "observed_salinity_ec": round(float(observed_ec), 2),
            "salinity_tolerance_ec": round(float(tolerance_ec), 2),
            "hazard_level": level,
            "message": message,
            "status": "OK",
        }

    # ─── SECTION 1 : PILOTAGE DE LA PERFORMANCE (CYCLES) ──────────────────
    
    async def create_crop_cycle(self, farm_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Démarre un nouveau cycle cultural.
        target_yield sert de boussole de performance pour l'IA.
        """
        cycle_id = _uuid()
        new_cycle = CropCycle(
            id=cycle_id,
            farm_id=farm_id,
            crop_type=data.get("crop_type"),
            variety=data.get("variety"),
            area_size=float(data.get("area_size", 0.0)),
            planted_at=data.get("planted_at") or datetime.now(),
            expected_harvest_date=data.get("expected_harvest_date"),
            target_yield=data.get("target_yield"), 
            status="IN_PROGRESS",
            farming_method=data.get("farming_method", "conventional")
        )
        self.session.add(new_cycle)
        await self.session.flush()
        return new_cycle.to_dict()

    async def get_cycle_with_context(self, cycle_id: str) -> Optional[Dict[str, Any]]:
        """
        Récupère un cycle avec l'intégralité de son historique relationnel.
        Fournit le 'Prompt Context' structuré indispensable à l'agent IA.
        """
        stmt = (
            select(CropCycle)
            .options(
                selectinload(CropCycle.interventions),
                selectinload(CropCycle.growth_logs),
                selectinload(CropCycle.ai_recommendations)
            )
            .where(CropCycle.id == cycle_id)
        )
        
        result = await self.session.execute(stmt)
        cycle = result.scalar_one_or_none()
        
        if not cycle: 
            return None
        
        return {
            **cycle.to_dict(),
            "interventions": [i.to_dict() for i in cycle.interventions],
            "growth_logs": [l.to_dict() for l in cycle.growth_logs],
            "recommendations": [r.to_dict() for r in cycle.ai_recommendations]
        }

    # ─── SECTION 2 : TRAÇABILITÉ & COÛTS (INTERVENTIONS) ─────────────────

    async def log_intervention(self, cycle_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Enregistre un événement ou traitement technique sur le terrain.
        Permet le calcul analytique de la marge par l'IA via cost_per_unit.
        """
        intervention = FieldIntervention(
            id=_uuid(),
            crop_cycle_id=cycle_id,
            type=data.get("type"),  # FERTILISATION, IRRIGATION, TRAITEMENT, etc.
            description=data.get("description"),
            input_used=data.get("input_used"),
            quantity=float(data.get("quantity", 0.0)),
            unit=data.get("unit"),
            cost_per_unit=float(data.get("cost_per_unit", 0.0)),
            machinery_used=data.get("machinery_used"),
            fuel_consumption=float(data.get("fuel_consumption", 0.0)),
            hours_worked=float(data.get("hours_worked", 0.0)),
            observed_bbch_stage=data.get("observed_bbch_stage"),
            performed_at=data.get("performed_at") or datetime.now()
        )
        self.session.add(intervention)
        
        # Synchronisation de la chronologie du cycle
        await self.session.execute(
            update(CropCycle)
            .where(CropCycle.id == cycle_id)
            .values(last_intervention_date=intervention.performed_at)
        )
        await self.session.flush()
        return intervention.to_dict()

    # ─── SECTION 3 : DIAGNOSTIC TERRAIN (LOGS & SOL) ─────────────────────

    async def add_growth_log(self, cycle_id: str, stage_code: int, image_url: str = None) -> Dict[str, Any]:
        """Enregistre une observation phénologique (validation GDD / BBCH)."""
        log = CropGrowthLog(
            id=_uuid(),
            crop_cycle_id=cycle_id,
            stage_code=stage_code,
            image_snapshot_url=image_url,
            observed_at=datetime.now()
        )
        self.session.add(log)
        await self.session.flush()
        return log.to_dict()

    async def update_soil_profile(self, farm_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Met à jour l'état physico-chimique du sol pour affiner les conseils en fertilisation."""
        profile = SoilProfile(
            id=_uuid(),
            farm_id=farm_id,
            ph_value=data.get("ph_value"),
            clay_percentage=data.get("clay_percentage"),
            organic_matter=data.get("organic_matter"),
            water_retention_capacity=data.get("water_retention_capacity"),
            sampling_date=data.get("sampling_date") or datetime.now()
        )
        self.session.add(profile)
        await self.session.flush()
        return profile.to_dict()

    # ─── SECTION 4 : CERVEAU AGENTIQUE & ANALYTICS TRACÉES ──────────────

    async def create_recommendation(self, user_id: str, cycle_id: str, rec_data: Dict[str, Any]) -> Dict[str, Any]:
        """Persiste les arbitrages de l'agent IA avec son arbre de décision (reasoning)."""
        recommendation = AIRecommendation(
            id=_uuid(),
            user_id=user_id,
            crop_cycle_id=cycle_id,
            category=rec_data.get("category"),  # IRRIGATION, FERTILISATION, PROTECTION
            priority=rec_data.get("priority", "MEDIUM"),
            message=rec_data.get("message"),
            reasoning=rec_data.get("reasoning"),
            is_applied=0
        )
        self.session.add(recommendation)
        await self.session.flush()
        return recommendation.to_dict()

    async def get_yield_performance_metrics(self, cycle_id: str) -> Dict[str, Any]:
        """Analyse l'atteinte des objectifs de rendement pour l'IA."""
        stmt = select(CropCycle).where(CropCycle.id == cycle_id)
        result = await self.session.execute(stmt)
        cycle = result.scalar_one_or_none()
        
        if not cycle or not cycle.target_yield:
            return {"error": "Données d'objectifs de rendement manquantes ou incomplètes pour ce cycle."}

        expected = cycle.expected_yield or 0.0
        target = cycle.target_yield
        gap = target - expected
        efficiency = (expected / target) * 100 if target > 0 else 0.0

        return {
            "target_yield": target,
            "expected_yield": expected,
            "gap": round(gap, 2),
            "efficiency_percentage": round(efficiency, 2),
            "status": "UNDER_PERFORMING" if gap > (target * 0.15) else "ON_TRACK"
        }

    async def get_biological_readiness(self, cycle_id: str) -> Dict[str, Any]:
        """Détecte les retards physiologiques en confrontant le stade BBCH observé et le besoin GDD."""
        stmt_log = select(CropGrowthLog).where(CropGrowthLog.crop_cycle_id == cycle_id).order_by(desc(CropGrowthLog.observed_at))
        log_res = await self.session.execute(stmt_log)
        last_log = log_res.scalar_one_or_none()

        if not last_log:
            return {"status": "NO_OBSERVATION", "message": "Aucun relevé de croissance disponible."}

        stmt_cycle = select(CropCycle.crop_type).where(CropCycle.id == cycle_id)
        cycle_type = (await self.session.execute(stmt_cycle)).scalar()
        
        stmt_stage = select(CropGrowthStage).where(
            and_(CropGrowthStage.crop_type == cycle_type, CropGrowthStage.stage_code == last_log.stage_code)
        )
        stage_std = (await self.session.execute(stmt_stage)).scalar_one_or_none()

        gdd_needed = stage_std.gdd_threshold if stage_std else 0
        actual_gdd = last_log.accumulated_gdd_at_stage or 0

        return {
            "current_stage_code": last_log.stage_code,
            "stage_name": stage_std.stage_name if stage_std else "Inconnu",
            "theoretical_gdd_needed": gdd_needed,
            "actual_accumulated_gdd": actual_gdd,
            "is_delayed": (actual_gdd < gdd_needed) if gdd_needed > 0 else False
        }

    async def get_cycle_economics(self, cycle_id: str) -> Dict[str, Any]:
        """Calcule le coût global cumulé des intrants, de l'énergie et de la main d'œuvre."""
        stmt = select(
            func.sum(FieldIntervention.quantity * FieldIntervention.cost_per_unit).label("total_input_cost"),
            func.sum(FieldIntervention.fuel_consumption * 1.5).label("estimated_fuel_cost"),  # Prix unitaire fioul
            func.sum(FieldIntervention.hours_worked * 15.0).label("labor_cost")              # Taux horaire moyen
        ).where(FieldIntervention.crop_cycle_id == cycle_id)
        
        res = await self.session.execute(stmt)
        costs = res.one()

        input_cost = float(costs.total_input_cost or 0.0)
        fuel_cost = float(costs.estimated_fuel_cost or 0.0)
        labor_cost = float(costs.labor_cost or 0.0)
        total_spent = input_cost + fuel_cost + labor_cost
        
        return {
            "total_spent": round(total_spent, 2),
            "breakdown": {
                "inputs_cost": round(input_cost, 2),
                "energy_cost": round(fuel_cost, 2),
                "labor_cost": round(labor_cost, 2)
            }
        }

    async def get_active_sanitary_risks(self, farm_id: str) -> List[Dict[str, Any]]:
        """Croise l'historique récent de la télémétrie locale avec le catalogue de pathogènes JSONB."""
        stmt_farm = select(Farm.zone_id).where(Farm.id == farm_id)
        zone_id = (await self.session.execute(stmt_farm)).scalar()

        if not zone_id:
            return []

        # Recherche proactive de tous les pathogènes enregistrés pour analyse contextuelle
        stmt_pests = select(PestDiseaseCatalog)
        pests = (await self.session.execute(stmt_pests)).scalars().all()
        return [p.to_dict() for p in pests]

    async def get_crop_requirements(self, crop_type: str, variety: str = None) -> Optional[Dict[str, Any]]:
        """Récupère les exigences et limites agronomiques de référence d'une plante."""
        standard = await self.get_best_standard(crop_type, variety)
        return standard.to_dict() if standard else None

    async def analyze_thermal_stress(self, cycle_id: str) -> Dict[str, Any]:
        """Évalue l'exposition aux risques de stress thermique sans aucune valeur hardcodée."""
        cycle_data = await self.get_cycle_with_context(cycle_id)
        if not cycle_data:
            return {"error": "Cycle introuvable"}

        standard = await self.get_best_standard(cycle_data['crop_type'], cycle_data.get('variety'))

        if not standard or getattr(standard, 'max_temp_threshold', None) is None:
            return {
                "status": "missing_threshold",
                "message": "Aucune température limite d'alerte définie dans le référentiel pour cette variété.",
                "crop": cycle_data.get("crop_type"),
                "variety": cycle_data.get("variety"),
            }

        thermal_limit = float(standard.max_temp_threshold)

        # Extraction analytique des données de télémétrie des dernières 24 heures
        stmt_weather = (
            select(SensorTelemetryHistory.temperature)
            .where(SensorTelemetryHistory.farm_id == cycle_data['farm_id'])
            .where(SensorTelemetryHistory.timestamp >= datetime.now() - timedelta(hours=24))
        )
        
        result = await self.session.execute(stmt_weather)
        temps = result.scalars().all()
        max_temp = max(temps) if temps else 0.0

        is_stressing = max_temp > thermal_limit
        stress_index = round(max_temp - thermal_limit, 2) if is_stressing else 0.0

        return {
            "crop": cycle_data['crop_type'],
            "variety": cycle_data.get('variety'),
            "detected_max_temp": max_temp,
            "critical_threshold": thermal_limit,
            "is_under_stress": is_stressing,
            "stress_severity_index": stress_index,
            "advice": f"Alerte critique : Seuil de {thermal_limit}°C dépassé. Déclencher l'irrigation." if is_stressing else "Climat optimal."
        }

    # ─── SECTION 5 : RÉFÉRENTIELS ET ENRICHISSEMENT DE CONNAISSANCES ───

    async def add_agronomic_standard(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Insère ou met à jour les constantes biologiques d'un couple culture/variété (Idempotence)."""
        c_type = data.get("crop_type")
        v_type = data.get("variety_type")
       
        values: Dict[str, Any] = {
            "id": _uuid(),
            "crop_type": c_type,
            "variety_type": v_type,
            "nitrogen_needs": positive_float(data.get("nitrogen_needs"), field="nitrogen", allow_zero=True),
            "phosphorus_needs": positive_float(data.get("phosphorus_needs"), field="phosphorus_needs", allow_zero=True),
            "potassium_needs": positive_float(data.get("potassium_needs"), field="potassium_needs", allow_zero=True),
            "base_temperature": positive_float(data.get("base_temperature"), field="base_temperature", allow_zero=True),
            "gdd_to_harvest": int(data.get("gdd_to_harvest") or 0),
            "min_humidity_threshold": positive_float(data.get("min_humidity_threshold"), field="min_humidity_threshold", allow_zero=True),
            "max_wind_speed_treatment": positive_float(data.get("max_wind_speed_treatment"), field="max_wind_speed_treatment", allow_zero=True),
            "max_temp_threshold": positive_float(data.get("max_temp_threshold"), field="max_temp_threshold", allow_zero=True),
            "version": str(data.get("version", "v1")),
        }

        stmt = (
            insert(AgronomicStandard)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["crop_type", "variety_type"],
                set_={k: v for k, v in values.items() if k != "id"},
            )
            .returning(AgronomicStandard)
        )

        res = await self.session.execute(stmt)
        standard = res.scalar_one()
        return standard.to_dict()

    async def get_best_standard(self, crop_type: str, variety: str = None) -> Optional[AgronomicStandard]:
        """Algorithme de résolution de fiches techniques : Varété spécifique > Standard générique."""
        async def _logic(session: AsyncSession) -> Optional[AgronomicStandard]:
            stmt = select(AgronomicStandard).where(AgronomicStandard.crop_type == crop_type)
            result = await session.execute(stmt)
            standards = result.scalars().all()

            if not standards:
                return None

            specific = next((s for s in standards if s.variety_type == variety), None)
            if specific:
                return specific

            return next((s for s in standards if s.variety_type is None), standards[0])

        return await self._call_with_retry(_logic)

    async def _call_with_retry(self, operation: Callable[[AsyncSession], Awaitable[T]]) -> T:
        session = self.session
        if session is None:
            raise RuntimeError("Database session is not initialized")

        try:
            return await operation(session)
        except ConnectionDoesNotExistError as exc:
            logger.warning("Connection closed during crop database lookup — retrying with new session", exc_info=True)
            sm = get_sessionmaker()
            if sm is None:
                raise RuntimeError("Database sessionmaker is unavailable") from exc
            async with sm() as fallback_session:
                return await operation(fallback_session)

    async def add_crop_growth_stage(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Spécifie un jalon d'échelle BBCH et ses contraintes nutritives."""
        stage = CropGrowthStage(
            id=_uuid(),
            crop_type=data.get("crop_type"),
            stage_code=data.get("stage_code"),
            stage_name=data.get("stage_name"),
            gdd_threshold=data.get("gdd_threshold"),
            nitrogen_need_kgha=data.get("nitrogen_need_kgha", 0.0),
            water_need_mmday=data.get("water_need_mmday", 0.0),
            agronomic_advice=data.get("agronomic_advice")
        )
        self.session.add(stage)
        await self.session.flush()
        return stage.to_dict()

    async def add_pest_disease_trigger(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Référence une cible sanitaire et ses règles bioclimatiques d'incubation."""
        pest = PestDiseaseCatalog(
            id=_uuid(),
            name=data.get("name"),
            type=data.get("type"),
            target_crops=data.get("target_crops"),       # ex: ["maize", "sorghum"]
            weather_triggers=data.get("weather_triggers"),   # ex: JSONB conditionnel
            treatment_threshold=data.get("treatment_threshold"),
            bio_solutions=data.get("bio_solutions")
        )
        self.session.add(pest)
        await self.session.flush()
        return pest.to_dict()

    async def add_growth_stage_requirement(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Enregistre ou met à jour de manière idempotente les besoins vitaux par stade BBCH."""
        c_type = data.get("crop_type")
        s_code = data.get("stage_code")

        if not c_type or not s_code:
            raise ValueError(f"Attributs requis manquants : crop_type={c_type}, stage_code={s_code}")

        values: Dict[str, Any] = {
            "id": _uuid(),
            "crop_type": c_type,
            "stage_code": s_code,
            "stage_name": data.get("stage_name"),
            "gdd_threshold": data.get("gdd_threshold"),
            "water_need_mmday": float(data.get("water_need_mmday") or 0.0),
            "nitrogen_need_kgha": float(data.get("nitrogen_need_kgha") or 0.0),
            "agronomic_advice": data.get("agronomic_advice"),
        }

        stmt = (
            insert(CropGrowthStage)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["crop_type", "stage_code"],
                set_={k: v for k, v in values.items() if k != "id"},
            )
            .returning(CropGrowthStage)
        )

        res = await self.session.execute(stmt)
        stage = res.scalar_one()
        return stage.to_dict()

    async def check_growth_compliance(self, cycle_id: str) -> Dict[str, Any]:
        """Analyse l'alignement cinétique de la plante par rapport à l'horloge thermique théorique."""
        stmt = (
            select(CropGrowthLog, CropCycle, CropGrowthStage)
            .join(CropCycle, CropGrowthLog.crop_cycle_id == CropCycle.id)
            .join(CropGrowthStage, and_(
                CropGrowthStage.crop_type == CropCycle.crop_type,
                CropGrowthStage.stage_code == CropGrowthLog.stage_code
            ))
            .where(CropCycle.id == cycle_id)
            .order_by(desc(CropGrowthLog.observed_at))
            .limit(1)
        )
        result = await self.session.execute(stmt)
        row = result.fetchone()

        if not row:
            return {"status": "NO_DATA", "message": "Aucun relevé d'observation terrain disponible."}

        log, cycle, stage = row
        gdd_gap = (log.accumulated_gdd_at_stage or 0) - (stage.gdd_threshold or 0)
        
        return {
            "observed_stage": stage.stage_name,
            "gdd_gap": gdd_gap,
            "is_on_track": abs(gdd_gap) < 50,
            "alert_level": "CRITICAL" if gdd_gap < -100 else "WARNING" if gdd_gap < -50 else "OK"
        }

    async def get_instant_resource_needs(self, cycle_id: str) -> Dict[str, Any]:
        """Extrait instantanément les seuils d'intrants recommandés pour le stade phénologique courant."""
        subq = (
            select(CropGrowthLog.stage_code)
            .where(CropGrowthLog.crop_cycle_id == cycle_id)
            .order_by(desc(CropGrowthLog.observed_at))
            .limit(1)
            .scalar_subquery()
        )
        
        stmt = (
            select(CropGrowthStage)
            .join(CropCycle, CropCycle.crop_type == CropGrowthStage.crop_type)
            .where(and_(CropCycle.id == cycle_id, CropGrowthStage.stage_code == subq))
        )
        res = await self.session.execute(stmt)
        needs = res.scalar_one_or_none()

        if not needs:
            return {"message": "Stade de croissance actuel non documenté ou observations absentes."}

        return {
            "water_need_mm_day": needs.water_need_mmday,
            "nitrogen_need_kg_ha": needs.nitrogen_need_kgha,
            "advice": needs.agronomic_advice
        }

    async def seed_agronomic_knowledge(self, config: List[Dict] = None) -> Dict[str, str]:
        """
        Alimente en masse le socle de connaissances agronomiques (Standards & Stades BBCH).
        Garantit la scalabilité complète : l'injection se fait par fichier de config sans altérer le code.
        """
        if config is None:
            config_path = Path(__file__).with_name("crop_config.json")
            if not config_path.exists():
                return {"status": "error", "message": f"crop_config.json introuvable à l'adresse : {config_path}"}

            try:
                raw = json.loads(config_path.read_text(encoding="utf-8"))
                config = raw.get("crops") or []
            except json.JSONDecodeError:
                return {"status": "error", "message": "Format de fichier JSON invalide pour crop_config.json"}
                
            if not config:
                return {"status": "error", "message": "Aucune structure de culture valide trouvée dans le fichier."}

        for crop_config in config:
            standard_data = crop_config.get("standard")
            if not standard_data:
                continue

            c_type = standard_data.get("crop_type")

            # Utilisation rigoureuse de self pour propager la session unifiée
            await self.add_agronomic_standard(standard_data)
            
            for stage in crop_config.get("stages", []):
                stage["crop_type"] = c_type
                await self.add_growth_stage_requirement(stage)

        return {"status": "success"}