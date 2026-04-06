import ee
from gee_auth import initialize_ee

class NASAProvider:
    def __init__(self):
        initialize_ee()
        # On utilise le produit Level 4 (le plus précis car il fusionne satellite et modèles)
        self.dataset = ee.ImageCollection("NASA/SMAP/SPL4SMGP/007")

    def get_realtime_moisture(self, lat, lon):
        """
        Récupère l'humidité du sol (0-5cm) pour les dernières 72 heures.
        """
        point = ee.Geometry.Point([lon, lat])
        
        # On prend l'image la plus récente disponible
        latest_image = self.dataset.sort('system:time_start', False).first()
        
        # On extrait la bande 'sm_surface' (Soil Moisture Surface)
        # Unité : m3/m3 (volume d'eau par volume de sol)
        data = latest_image.select('sm_surface').sample(point, 30).first().getInfo()
        
        if not data:
            return None
            
        value = data.get('properties', {}).get('sm_surface')
        
        # Interprétation de la donnée
        status = "Sec"
        if value > 0.35: status = "Saturé (Risque d'anoxie)"
        elif value > 0.20: status = "Idéal (Humide)"
        elif value > 0.10: status = "Modéré"
            
        return {
            "source": "NASA-SMAP",
            "moisture_m3m3": round(value, 3),
            "status": status,
            "timestamp": latest_image.get('system:index').getInfo()
        }

if __name__ == "__main__":
    # Exemple de coordonnées (latitude, longitude)
    lat, lon = 11.17, -4.29
    provider = NASAProvider()
    result = provider.get_realtime_moisture(lat, lon)
    if result:
        print("Résultat NASA SMAP :")
        for k, v in result.items():
            print(f"  {k}: {v}")
    else:
        print("Aucune donnée NASA SMAP disponible pour ce point.")