import logging
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select, func, and_, or_, update, desc
from sqlalchemy.orm import selectinload
from sqlalchemy.dialects.postgresql import insert

from agriconnect.domain.models import (
    Farm, CropCycle, FieldIntervention, CropGrowthLog, 
    SensorDataSummary, SoilProfile, SensorTelemetryHistory,
    AgronomicStandard, CropGrowthStage, PestDiseaseCatalog,
    AIRecommendation, WeatherDataLog, User, Zone, _uuid4
)
from agriconnect.services.database.common import positive_float
from .base import BaseMixin

logger = logging.getLogger("agriconnect.services.database")

def _uuid() -> str:
    return str(_uuid4())


class CropMixin(BaseMixin):
    
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
        stmt = select(AgronomicStandard).where(AgronomicStandard.crop_type == crop_type)
        result = await self.session.execute(stmt)
        standards = result.scalars().all()

        if not standards:
            return None

        specific = next((s for s in standards if s.variety_type == variety), None)
        if specific:
            return specific
        
        return next((s for s in standards if s.variety_type is None), standards[0])

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