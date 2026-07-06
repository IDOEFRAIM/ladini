"""
Agent Watcher - Surveillance Proactive des Risques Agricoles
Tâche de fond (Background Job) qui surveille météo, ravageurs, prix
et génère des alertes automatiques.
"""

import logging
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Any
import os

# Imports pour DB (adapter selon votre ORM)
# from prisma import Prisma
# from sqlalchemy import create_engine, select
# from .models import Alert, Zone, WeatherData

from agriconnect.services.google.openmeteo import OpenMeteoService
from futur.tools.sentinelle import SentinelleTool

logger = logging.getLogger("WatcherAgent")


class WatcherAgent:
    """
    Agent de surveillance proactive.
    
    Tâches :
    1. Scraper météo toutes les 6h
    2. Analyser les risques (sécheresse, invasion)
    3. Créer des alertes dans la table Alert
    4. Ne PAS envoyer directement (c'est le job du Broadcaster)
    """
    
    def __init__(self, db_client):
        self.db = db_client  # Prisma ou SQLAlchemy session
        self.meteo_service = OpenMeteoService()
        self.sentinel_tool = SentinelleTool()
        
        # Seuils d'alerte (à configurer)
        self.THRESHOLDS = {
            "rainfall_low": 10,  # mm/semaine → sécheresse
            "rainfall_high": 100,  # mm/24h → inondation
            "temp_high": 40,  # °C → canicule
            "temp_low": 10,  # °C pour cultures sensibles
        }
    
    async def run_cycle(self):
        """
        Cycle complet de surveillance (à exécuter toutes les 6h).
        """
        logger.info("🔍 WatcherAgent : Démarrage cycle de surveillance")
        
        try:
            # 1. Surveiller la météo pour toutes les zones
            await self._monitor_weather()
            
            # 2. Surveiller les ravageurs (données FEWS NET)
            await self._monitor_pests()
            
            # 3. Surveiller les prix (détection de chutes anormales)
            await self._monitor_prices()
            
            logger.info("✅ WatcherAgent : Cycle terminé avec succès")
            
        except Exception as e:
            logger.error(f"❌ WatcherAgent : Erreur cycle - {e}", exc_info=True)
    
    async def _monitor_weather(self):
        """
        Surveille la météo pour toutes les zones actives.
        Génère des alertes si anomalies détectées.
        """
        logger.info("🌦️ Surveillance météo en cours...")
        
        # Récupérer toutes les zones
        zones = await self.db.zone.find_many()
        
        for zone in zones:
            try:
                # Récupérer données météo (via API ou cache)
                weather = await self._fetch_weather_for_zone(zone)
                
                # Stocker dans la DB
                await self.db.weather_data.create(
                    data={
                        "zoneId": zone.id,
                        "temperature": weather["temperature"],
                        "humidity": weather["humidity"],
                        "rainfall_mm": weather["rainfall"],
                        "wind_speed": weather.get("wind_speed"),
                        "forecast_rain": weather.get("forecast_rain"),
                        "source": "OPENMETEO",
                        "recorded_at": datetime.utcnow(),
                    }
                )
                
                # Analyser les risques
                await self._analyze_weather_risks(zone, weather)
                
            except Exception as e:
                logger.error(f"❌ Erreur météo zone {zone.name}: {e}")
    
    async def _fetch_weather_for_zone(self, zone) -> Dict[str, Any]:
        """
        Récupère les données météo pour une zone.
        """
        # Extraire coordonnées (si stockées dans zone.coordinates)
        coords = zone.coordinates or {"lat": 12.3714, "lng": -1.5197}  # Fallback Ouaga
        
        # Appeler OpenMeteo
        data = self.meteo_service.get_forecast(
            latitude=coords["lat"],
            longitude=coords["lng"],
            days=7
        )
        
        return {
            "temperature": data.get("current", {}).get("temperature", 30),
            "humidity": data.get("current", {}).get("humidity", 50),
            "rainfall": data.get("daily", {}).get("precipitation_sum", [0])[0],
            "wind_speed": data.get("current", {}).get("windspeed", 0),
            "forecast_rain": sum(data.get("daily", {}).get("precipitation_sum", [0])[:7]),
        }
    
    async def _analyze_weather_risks(self, zone, weather: Dict[str, Any]):
        """
        Analyse les données météo et crée des alertes si nécessaire.
        """
        alerts_to_create = []
        
        # Risque de sécheresse
        if weather["rainfall"] < self.THRESHOLDS["rainfall_low"]:
            logger.warning(f"⚠️ Sécheresse détectée dans {zone.name}")
            alerts_to_create.append({
                "type": "WEATHER",
                "severity": "WARNING",
                "title": "⚠️ Risque de Sécheresse",
                "message": (
                    f"Très peu de pluie cette semaine ({weather['rainfall']:.1f}mm). "
                    f"Arrosez vos cultures sensibles et paillez le sol pour retenir l'humidité."
                ),
                "zoneId": zone.id,
                "created_by": "WATCHER_AGENT",
                "valid_until": datetime.utcnow() + timedelta(days=3),
            })
        
        # Risque d'inondation
        if weather["rainfall"] > self.THRESHOLDS["rainfall_high"]:
            logger.warning(f"⚠️ Risque d'inondation dans {zone.name}")
            alerts_to_create.append({
                "type": "WEATHER",
                "severity": "CRITICAL",
                "title": "🚨 Alerte Inondation",
                "message": (
                    f"Pluies intenses prévues ({weather['rainfall']:.1f}mm). "
                    f"Vérifiez vos drainages et protégez les semis."
                ),
                "zoneId": zone.id,
                "created_by": "WATCHER_AGENT",
                "valid_until": datetime.utcnow() + timedelta(days=1),
            })
        
        # Risque de canicule
        if weather["temperature"] > self.THRESHOLDS["temp_high"]:
            alerts_to_create.append({
                "type": "WEATHER",
                "severity": "WARNING",
                "title": "🌡️ Canicule",
                "message": (
                    f"Températures très élevées ({weather['temperature']:.1f}°C). "
                    f"Arrosez tôt le matin ou tard le soir. Protégez les jeunes plants."
                ),
                "zoneId": zone.id,
                "created_by": "WATCHER_AGENT",
                "valid_until": datetime.utcnow() + timedelta(days=2),
            })
        
        # Créer les alertes dans la DB
        for alert_data in alerts_to_create:
            await self.db.alert.create(data=alert_data)
            logger.info(f"✅ Alerte créée : {alert_data['title']} pour {zone.name}")
    
    async def _monitor_pests(self):
        """
        Surveille les ravageurs (via FEWS NET ou autres sources).
        """
        logger.info("🐛 Surveillance ravageurs en cours...")
        
        # TODO: Intégrer scraper FEWS NET ou système de signalement communautaire
        # Si X% d'utilisateurs signalent "chenilles" dans une zone → alerte automatique
        
        # Exemple simplifié :
        zones_at_risk = await self._detect_pest_outbreaks()
        
        for zone_id, pest_info in zones_at_risk.items():
            await self.db.alert.create(
                data={
                    "type": "PEST",
                    "severity": "CRITICAL",
                    "title": f"🐛 Alerte Ravageur : {pest_info['name']}",
                    "message": (
                        f"Invasion de {pest_info['name']} signalée dans votre zone. "
                        f"Traitement recommandé : {pest_info['treatment']}"
                    ),
                    "zoneId": zone_id,
                    "target_crop": pest_info.get("crop"),
                    "created_by": "WATCHER_AGENT",
                    "valid_until": datetime.utcnow() + timedelta(days=5),
                }
            )
    
    async def _detect_pest_outbreaks(self) -> Dict[str, Dict]:
        """
        Détection des invasions de ravageurs.
        
        Logique possible :
        - Scraping FEWS NET
        - Analyse des conversations (NLP sur les plaintes utilisateurs)
        - API gouvernementale
        """
        # Placeholder : retourne un dictionnaire {zone_id: pest_info}
        return {}
    
    async def _monitor_prices(self):
        """
        Surveille les prix du marché.
        Alerte si chute anormale (opportunité de vente) ou hausse (achat malin).
        """
        logger.info("💰 Surveillance prix marché en cours...")
        
        # Récupérer les prix moyens par zone et crop
        # Si prix MAIS chute de 30% en 1 semaine → alerte "Vendez maintenant !"
        
        # TODO: Implémenter logique de détection d'anomalies de prix
        pass


# ============================================
# BACKGROUND JOB RUNNER (APScheduler)
# ============================================

from apscheduler.schedulers.asyncio import AsyncIOScheduler

def start_watcher_agent(db_client):
    """
    Démarre le WatcherAgent en tâche de fond.
    
    Usage:
        # Dans main.py FastAPI
        @app.on_event("startup")
        async def startup():
            start_watcher_agent(prisma_client)
    """
    scheduler = AsyncIOScheduler()
    watcher = WatcherAgent(db_client)
    
    # Exécuter toutes les 6 heures
    scheduler.add_job(
        watcher.run_cycle,
        'interval',
        hours=6,
        id='watcher_cycle',
        replace_existing=True
    )
    
    # Exécution immédiate au démarrage
    scheduler.add_job(
        watcher.run_cycle,
        'date',
        run_date=datetime.now() + timedelta(seconds=10),
        id='watcher_initial'
    )
    
    scheduler.start()
    logger.info("🚀 WatcherAgent démarré (cycle toutes les 6h)")


if __name__ == "__main__":
    # Test standalone
    import asyncio
    from prisma import Prisma
    
    async def test():
        db = Prisma()
        await db.connect()
        
        watcher = WatcherAgent(db)
        await watcher.run_cycle()
        
        await db.disconnect()
    
    asyncio.run(test())
