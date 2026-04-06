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