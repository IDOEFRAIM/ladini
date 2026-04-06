import requests
import json
import os
import time

class SoilGridScraper:
    def __init__(self, properties=None, depths=None, values=None):
        self.url = "https://rest.isric.org/soilgrids/v2.0/properties/query"
        self.properties = properties or ["phh2o", "nitrogen", "soc", "sand", "clay", "cec", "bdod"]
        self.depths = depths or ["0-5cm", "15-30cm"]
        self.values = values or ["mean", "uncertainty"]
        self.output_dir = "backend/sources/raw_data/soil_grids"
        os.makedirs(self.output_dir, exist_ok=True)

    @staticmethod
    def get_representative_zones():
        return {
            "Zone_Maraichere_Loumbila": (12.4833, -1.3833),    # Sortie Est de Ouaga (production réelle)
            "Bassin_Coton_Ouest": (11.4000, -4.0000),          # Déjà OK
            "Verger_Kou_Bobo": (11.2333, -4.4333),             # Périphérie agricole de Bobo
            "Pole_Agro_Industriel_Sud": (10.6400, -4.7600),    # Déjà OK
            "Zone_Cerealiere_Ouest": (12.2500, -2.3627),       # Déjà OK
            "Bassin_Soja_Est": (12.0600, 0.3600),              # Déjà OK
            "Resilience_Sahelienne_Nord": (14.0300, -0.0300)   # Déjà OK
        }

    def _is_valid(self, data):
        """Vérifie si le JSON contient au moins une valeur de pH non nulle."""
        try:
            # On teste la couche pH qui est la plus commune
            val = data['layers']['phh2o']['0-5cm']['mean']
            return val is not None
        except KeyError:
            return False

    def fetch_point(self, lat, lon, max_retries=4):
        """Récupère les données avec un décalage progressif si les valeurs sont null."""
        # Décalages : (0,0), puis Nord, Sud, Est, Ouest d'environ 500m
        offsets = [(0, 0), (0.005, 0), (-0.005, 0), (0, 0.005), (0, -0.005)]
        
        for i in range(min(max_retries + 1, len(offsets))):
            d_lat, d_lon = offsets[i]
            target_lat, target_lon = lat + d_lat, lon + d_lon
            
            params = {
                "lon": target_lon, "lat": target_lat,
                "property": self.properties, "depth": self.depths, "value": self.values
            }
            
            try:
                response = requests.get(self.url, params=params, timeout=15)
                if response.status_code != 200: continue
                
                raw_data = response.json()
                results = {"metadata": {"lat": target_lat, "lon": target_lon, "original_lat": lat}, "layers": {}}

                for layer in raw_data.get('properties', {}).get('layers', []):
                    name = layer.get('name')
                    results["layers"][name] = {}
                    for depth in layer.get('depths', []):
                        label = depth.get('label')
                        v = depth.get('values', {})
                        mean_val = v.get('mean') / 10 if name == "phh2o" and v.get('mean') else v.get('mean')
                        results["layers"][name][label] = {"mean": mean_val, "uncertainty": v.get('uncertainty')}

                if self._is_valid(results):
                    return results
                else:
                    print(f"      ⚠️ Point ({target_lat}, {target_lon}) vide, tentative suivante...")
            except Exception:
                continue
            
        return {"error": "Toutes les tentatives de décalage ont échoué (valeurs null)."}

    def run_strategic_scraping(self):
        zones = self.get_representative_zones()
        for zone_name, coords in zones.items():
            print(f"📡 Analyse : {zone_name}...")
            data = self.fetch_point(coords[0], coords[1])
            
            if "error" not in data:
                filepath = os.path.join(self.output_dir, f"{zone_name.lower()}.json")
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
                print(f"   ✅ Succès : {zone_name}")
            else:
                print(f"   ❌ Échec critique : {data['error']}")
            time.sleep(1) # Politesse envers l'API

if __name__ == "__main__":
    scraper = SoilGridScraper()
    scraper.run_strategic_scraping()