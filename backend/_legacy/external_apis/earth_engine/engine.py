import ee
import json
import os

# Chemin vers le fichier de clé du compte de service
SERVICE_ACCOUNT_KEY = os.path.join(os.path.dirname(__file__), "apikey.json")

def initialize_ee():
    try:
        # Authentification avec le compte de service
        ee.Initialize(credentials=ee.ServiceAccountCredentials('', SERVICE_ACCOUNT_KEY))
    except Exception as e:
        print(f"Erreur d'initialisation Earth Engine : {e}")

def get_chirps_rainfall(lat, lon, start_date, end_date):
    # Définir le point GPS du champ
    poi = ee.Geometry.Point(lon, lat)
    
    # Charger la collection CHIRPS Daily
    chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY") \
                .filterBounds(poi) \
                .filterDate(start_date, end_date)
    
    # Calculer le cumul total sur la période
    total_rainfall = chirps.select('precipitation').sum()
    
    # Extraire la valeur numérique pour le point précis
    stats = total_rainfall.reduceRegion(
        reducer=ee.Reducer.first(),
        geometry=poi,
        scale=5000 # Résolution de 5km de CHIRPS
    ).getInfo()
    
    return stats.get('precipitation')

if __name__ == "__main__":
    initialize_ee()
    # Exemple : Un champ près de Bobo-Dioulasso
    latitude = 11.17
    longitude = -4.29
    pluie = get_chirps_rainfall(latitude, longitude, '2025-05-01', '2025-05-20')
    print(f"Pluie cumulée du 1er au 20 mai 2025 : {pluie:.2f} mm")