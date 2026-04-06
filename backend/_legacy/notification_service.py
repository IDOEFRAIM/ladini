import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import List, Dict, Any

from sqlalchemy.orm import Session
from sqlalchemy import select

from agriconnect.tools.db_handler import get_db
from agriconnect.services.models import User, Zone, UserCrop
from agriconnect.tools.sentinelle import SentinelleTool
from agriconnect.tools.shared_math import SahelAgroMath
from agriconnect.tools.crop import BurkinaCropTool
from agriconnect.utils.sms_adapter import SMSAdapter

logger = logging.getLogger(__name__)

class NotificationService:
    """
    Service d'orchestration des notifications quotidiennes aux agriculteurs.
    Combine Météo (Sentinelle), Agronomie (SahelAgroMath) et Fiches techniques (INERA).
    """

    def __init__(self):
        self.sentinelle = SentinelleTool()
        self.crop_tool = BurkinaCropTool()
        self.sms_adapter = SMSAdapter()

    def send_daily_agro_report(self, dry_run: bool = False):
        """
        Exécute le flux complet d'envoi des notifications journalières.
        
        Étapes:
        1. Récupération des utilisateurs et de leurs cultures actives.
        2. Regroupement par Zone (Batching) pour optimiser les appels météo.
        3. Pour chaque zone :
           a. Récupération météo (Sentinelle).
           b. Calcul des métriques agro (SahelAgroMath).
        4. Pour chaque utilisateur/culture :
           a. Contextualisation avec les règles de culture (BurkinaCropTool).
           b. Génération du message (Conseil de Décision).
           c. Envoi (Simulation via Logger pour l'instant).
        """
        db = get_db()
        if not db:
            logger.error("Impossible d'obtenir une connexion DB.")
            return

        session: Session = db.SessionLocal()
        try:
            logger.info("🚀 Démarrage du batch de notifications journalières...")
            
            # 1. Fetch Users with Active Crops
            # On récupère User + Zone + UserCrop
            stmt = (
                select(User, Zone, UserCrop)
                .join(Zone, User.zone_id == Zone.id)
                .join(UserCrop, User.id == UserCrop.user_id)
                .where(UserCrop.status == "ACTIVE")
                # TODO: Ajouter .where(User.alerts_enabled == True) quand la colonne existera
            )
            results = session.execute(stmt).all()

            if not results:
                logger.info("Aucun utilisateur avec culture active trouvé. Fin du batch.")
                return

            # Group by Zone for Batching
            users_by_zone = defaultdict(list)
            for user, zone, crop in results:
                users_by_zone[zone.name].append({
                    "user": user,
                    "zone": zone,
                    "crop": crop
                })

            logger.info(f"📍 {len(users_by_zone)} zones identifiées pour {len(results)} cultures actives.")

            # Process by Zone
            for zone_name, items in users_by_zone.items():
                logger.info(f"Traitement de la zone : {zone_name}")
                
                # 2. Weather Logic (Batch Call)
                # On utilise la localisation de la zone
                # SentinelleTool attend un dict avec "zone" ou "village"
                location_profile = {"zone": zone_name}
                weather_data = self.sentinelle._fetch_real_weather(location_profile)
                
                # 3. Agro-Maths
                # Calcul de base : Delta T (Temp, Humidité requise par OpenMeteo qui manquent parfois, ici on simule ou on utilise ce qu'on a)
                # Note: Sentinelle._fetch_real_weather retourne un format simplifié.
                # Essayons d'extraire ce qu'on peut.
                temp = weather_data.get("temp_c", 30.0)
                # OpenMeteo via Sentinelle ne retourne pas l'humidité dans _format_open_meteo_response.
                # On va devoir estimer ou modifier Sentinelle. Pour l'instant, simulons une RH moyenne pour le calcul.
                # (Amélioration future: Ajouter l'humidité dans SentinelleTool)
                rh_est = 40.0 # Estimation sèche sahélienne par défaut
                
                delta_t, dt_advice = SahelAgroMath.calculate_delta_t(temp, rh_est)
                et0 = weather_data.get("et0", 5.0)
                
                logger.debug(f"Météo {zone_name}: Temp={temp}°C, ET0={et0}mm, DeltaT={delta_t}")

                # 4. Dispatch per User
                for item in items:
                    user = item["user"]
                    crop = item["crop"]
                    
                    self._process_single_user_notification(
                        user=user,
                        crop=crop,
                        weather=weather_data,
                        agro_metrics={"delta_t": delta_t, "et0": et0, "dt_advice": dt_advice},
                        dry_run=dry_run
                    )

        except Exception as e:
            logger.error(f"Erreur critique lors du batch de notification: {e}", exc_info=True)
        finally:
            session.close()

    def _process_single_user_notification(
        self, 
        user: User, 
        crop: UserCrop, 
        weather: Dict[str, Any], 
        agro_metrics: Dict[str, Any],
        dry_run: bool
    ):
        """Logique de décision unitaire."""
        
        # 1. Règles INERA
        crop_name = crop.crop_name
        math_profile = self.crop_tool.get_math_profile(crop_name)
        
        # Ajustement sensibilité sécheresse
        irrigation_threshold = 6.0
        if math_profile and math_profile.drought_sensitive:
            irrigation_threshold = 5.0 # Plus sensible

        # 2. Construction du Message (Scénarios)
        message = ""
        
        # Scénario A : Traitement Phytosanitaire (Delta T optimal)
        if 2 <= agro_metrics["delta_t"] <= 8 and weather.get("wind_kph", 0) < 10:
            message = (
                f"Bonjour {user.name}, à {weather.get('source', 'votre zone')} ce matin :\n"
                f"Delta T = {agro_metrics['delta_t']} (Optimal). Vent faible.\n"
                f"✅ Bon moment pour traiter votre {crop_name} si nécessaire."
            )
        
        # Scénario B : Irrigation (ET0 élevé)
        elif agro_metrics["et0"] > irrigation_threshold:
             senstivity_msg = "sensible à la sécheresse" if (math_profile and math_profile.drought_sensitive) else "en demande d'eau"
             message = (
                f"Alerte Irrigation {user.name} :\n"
                f"L'évapotranspiration est forte ({agro_metrics['et0']} mm) aujourd'hui.\n"
                f"Le {crop_name} est {senstivity_msg}.\n"
                f"💧 Pensez à irriguer vos parcelles ce soir."
            )
        
        # Scénario C : Info générale
        else:
            message = (
                f"Météo Agri {user.name} : {weather.get('temp_c')}°C, "
                f"Pluie prévue : {weather.get('precip_mm')}mm.\n"
                f"Bonne journée aux champs !"
            )

        # 3. Optimisation SMS
        sms_content = self.sms_adapter.compress_for_sms(message)
        
        # 4. Envoi
        if dry_run:
            logger.info(f"[DRY-RUN] To {user.phone or 'NoPhone'}: {sms_content}")
        else:
            # TODO: Remplacer par un vrai appel SMS Provider
            logger.info(f"📨 [SMS SENT] To {user.phone}: {sms_content}")

        # 3. Optimisation SMS
        sms_content = self.sms_adapter.compress_for_sms(message)
        
        # 4. Envoi
        if dry_run:
            logger.info(f"[DRY-RUN] To {user.phone or 'NoPhone'}: {sms_content}")
        else:
            # TODO: Remplacer par un vrai appel SMS Provider
            # sms_provider.send(user.phone, sms_content)
            logger.info(f"📨 [SMS SENT] To {user.phone}: {sms_content}")

# Instance globale pour import facile
notification_service = NotificationService()
