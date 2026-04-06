import os
import datetime

from gee_service import initialize_ee, fetch_rainfall_batch

from risk_engine import analyze_risk


# Configuration GEE

SERVICE_ACCOUNT_KEY = os.path.join(os.path.dirname(__file__), "apikey.json")

GEMAIL = "ton-service-account@ton-projet.iam.gserviceaccount.com" # Mettre l'email du JSON ici



# Seuils de Risque (Logique Métier)

# Ces seuils peuvent être ajustés selon la saison

RISK_THRESHOLDS = {

    "DROUGHT_LIMIT": 10.0,   # Moins de 10mm en 10 jours = Danger Sécheresse

    "FLOOD_LIMIT": 150.0,    # Plus de 150mm = Danger Inondation/Lessivage

    "OPTIMAL_MIN": 20.0,     # Minimum pour semer

}



# Configuration du traitement

BATCH_SIZE = 500  # Nombre d'agriculteurs traités en une seule requête GEEimport os



# Configuration GEE

SERVICE_ACCOUNT_KEY = os.path.join(os.path.dirname(__file__), "apikey.json")

GEMAIL = "ton-service-account@ton-projet.iam.gserviceaccount.com" # Mettre l'email du JSON ici



# Seuils de Risque (Logique Métier)

# Ces seuils peuvent être ajustés selon la saison

RISK_THRESHOLDS = {

    "DROUGHT_LIMIT": 10.0,   # Moins de 10mm en 10 jours = Danger Sécheresse

    "FLOOD_LIMIT": 150.0,    # Plus de 150mm = Danger Inondation/Lessivage

    "OPTIMAL_MIN": 20.0,     # Minimum pour semer

}



# Configuration du traitement

BATCH_SIZE = 500  # Nombre d'agriculteurs traités en une seule requête GEE
# RISK_THRESHOLDS est déjà défini localement ci-dessus



def analyze_risk(rainfall_mm):

    """

    Détermine le statut de danger pour un agriculteur basé sur la pluie.

    """

    if rainfall_mm is None:

        return {"level": "UNKNOWN", "message": "Donnée non disponible"}

        

    rainfall_mm = float(rainfall_mm)

    

    if rainfall_mm < RISK_THRESHOLDS["DROUGHT_LIMIT"]:

        return {

            "level": "CRITICAL",

            "type": "SECHERESSE",

            "message": f"Alerte : Seulement {rainfall_mm:.1f}mm reçus. Stress hydrique imminent.",

            "action": "Reporter semis / Irrigation d'urgence"

        }

    

    elif rainfall_mm > RISK_THRESHOLDS["FLOOD_LIMIT"]:

        return {

            "level": "WARNING",

            "type": "INONDATION",

            "message": f"Alerte : {rainfall_mm:.1f}mm reçus. Risque de lessivage des sols.",

            "action": "Suspendre fertilisation / Créer canaux drainage"

        }

        

    elif rainfall_mm >= RISK_THRESHOLDS["OPTIMAL_MIN"]:

        return {

            "level": "OPTIMAL",

            "type": "BONNES_CONDITIONS",

            "message": f"Succès : {rainfall_mm:.1f}mm. Sol humide.",

            "action": "Conditions favorables pour semis/croissance"

        }

        

    else:

        return {

            "level": "NORMAL",

            "type": "RAS",

            "message": f"Pluie modérée : {rainfall_mm:.1f}mm.",

            "action": "Surveillance continue"

        }




# --- SIMULATION DE TA BASE DE DONNÉES ---

def get_farmers_from_db():

    """Simule la récupération de tes utilisateurs"""

    return [

        {"id": 1, "name": "Moussa (Bobo)", "lat": 11.17, "lon": -4.29},

        {"id": 2, "name": "Jean (Dédougou)", "lat": 12.46, "lon": -3.46},

        {"id": 3, "name": "Fatou (Ouaga)", "lat": 12.37, "lon": -1.52},

        # Imagine 1000 autres ici...

    ]



def save_alert_to_db(farmer_id, risk_data, rainfall):

    """Simule l'envoi de notif ou sauvegarde"""

    if risk_data["level"] in ["CRITICAL", "WARNING"]:

        print(f"🔔 NOTIFICATION ENVOYÉE à Farmer {farmer_id}: {risk_data['message']}")

    else:

        print(f"📝 Log: Farmer {farmer_id} - {risk_data['type']} ({rainfall:.1f}mm)")



# --- LE WORKER PRINCIPAL ---

def run_weather_job():

    print("🚀 Démarrage du Job Météo AgriConnect...")

    

    # 1. Init GEE

    initialize_ee()

    

    # 2. Définir la période (ex: les 10 derniers jours pour voir la tendance)

    today = datetime.date.today()

    start_date = (today - datetime.timedelta(days=10)).strftime('%Y-%m-%d')

    end_date = today.strftime('%Y-%m-%d')

    print(f"📅 Analyse de la période : {start_date} au {end_date}")

    

    # 3. Récupérer les agriculteurs

    farmers = get_farmers_from_db()

    

    # 4. Fetching Scalable (Par lot si besoin, ici tout d'un coup)

    print("📡 Interrogation de CHIRPS (NASA)...")

    gee_results = fetch_rainfall_batch(farmers, start_date, end_date)

    

    # 5. Traitement et Alertes

    print("🧠 Analyse des risques...")

    for result in gee_results:

        props = result['properties']

        

        farmer_id = props.get('farmer_id')

        rainfall = props.get('first') # 'first' est le nom par défaut du reducer

        

        # Gestion des cas où il n'y a pas de donnée (ex: hors de la carte)

        if rainfall is None:

            rainfall = 0.0

            

        # Analyse Intelligence

        risk = analyze_risk(rainfall)

        

        # Action (Sauvegarde/Notif)

        save_alert_to_db(farmer_id, risk, rainfall)



    print("✅ Job terminé avec succès.")



if __name__ == "__main__":

    run_weather_job()