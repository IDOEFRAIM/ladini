import json
import requests
from datetime import datetime, timedelta
import logging

class OpenMeteoCollector:
    REGIONS = [
        {"name": "Bobo-Dioulasso", "lat": 11.1772, "lon": -4.2979, "type": "Coton/Maïs"},
        {"name": "Dédougou", "lat": 12.4634, "lon": -3.4607, "type": "Grenier du Faso"},
        {"name": "Ouahigouya", "lat": 13.5828, "lon": -2.4216, "type": "Maraîchage/Sorgho"},
        {"name": "Fada N'Gourma", "lat": 12.0627, "lon": 0.3578, "type": "Élevage/Sésame"}
    ]

    METRICS = [
        "temperature_2m_max",
        "temperature_2m_min",
        "apparent_temperature_max",
        "apparent_temperature_min",
        "sunrise",
        "sunset",
        "precipitation_sum",
        "rain_sum",
        "showers_sum",
        "snowfall_sum",
        "precipitation_hours",
        "windspeed_10m_max",
        "windgusts_10m_max",
        "winddirection_10m_dominant",
        "shortwave_radiation_sum",
        "et0_fao_evapotranspiration",
        "weathercode",
        "uv_index_max",
        "uv_index_clear_sky_max",
        "soil_temperature_0_to_7cm_min",
        "soil_temperature_0_to_7cm_max",
        "soil_moisture_0_to_7cm_mean",
        "soil_moisture_7_to_28cm_mean",
        "soil_moisture_28_to_100cm_mean",
        "soil_moisture_100_to_255cm_mean"
    ]

    BASE_URL = "https://archive-api.open-meteo.com/v1/archive"

    def __init__(self, days_back=60, storage_type="print"):
        self.days_back = days_back
        self.storage_type = storage_type
        self.logger = logging.getLogger("AgriConnect.OpenMeteo")
        self.logger.setLevel(logging.INFO)

    def get_date_range(self):
        end_date = datetime.now() - timedelta(days=1)
        start_date = end_date - timedelta(days=self.days_back)
        return start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')

    def fetch_region_data(self, region, start, end):
        params = {
            "latitude": region["lat"],
            "longitude": region["lon"],
            "start_date": start,
            "end_date": end,
            "daily": ",".join(self.METRICS),
            "timezone": "GMT"
        }
        try:
            response = requests.get(self.BASE_URL, params=params, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Erreur fetch pour {region['name']}: {str(e)}")
            return None

    @staticmethod
    def simple_alert(record):
        if record["max_temp_c"] is not None and record["rainfall_mm"] is not None:
            if record["max_temp_c"] > 35 and record["rainfall_mm"] == 0:
                return "HIGH_HEAT_NO_RAIN"
        return "NORMAL"

    def process_region_data(self, raw_data, region_info, alert_fn=None):
        if not raw_data or "daily" not in raw_data:
            return []
        processed = []
        daily = raw_data["daily"]
        dates = daily["time"]
        for i, date in enumerate(dates):
            record = {
                "region": region_info["name"],
                "crop_type": region_info["type"],
                "date": date,
                "rainfall_mm": daily.get("rain_sum", [None])[i],
                "max_temp_c": daily.get("temperature_2m_max", [None])[i],
                "soil_moisture_index": daily.get("soil_moisture_0_to_7cm_mean", [None])[i]
            }
            record["alert"] = (alert_fn or self.simple_alert)(record)
            processed.append(record)
        return processed

    def store_results(self, data, **kwargs):
        if self.storage_type == "print":
            self.logger.info(f"Storing {len(data)} records (simulation).")
        elif self.storage_type == "json":
            fname = kwargs.get("filename", "agri_data.json")
            with open(fname, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.logger.info(f"Data written to {fname}")
        # Extend here for S3, SQL, etc.

    def run(self):
        self.logger.info("Démarrage du job AgriConnect Data Collection")
        start_str, end_str = self.get_date_range()
        all_results = []
        for region in self.REGIONS:
            self.logger.info(f"Traitement de la zone : {region['name']}")
            raw_data = self.fetch_region_data(region, start_str, end_str)
            if raw_data:
                clean_data = self.process_region_data(raw_data, region)
                all_results.extend(clean_data)
        self.store_results(all_results)
        summary = {
            "status": "success",
            "date_processed": datetime.now().isoformat(),
            "records_collected": len(all_results),
            "period": f"{start_str} to {end_str}",
            "sample_data": all_results[:2]
        }
        self.logger.info(f"Succès. {len(all_results)} enregistrements collectés.")
        return summary

# Pour test local
if __name__ == "__main__":
    #revoir les metrics
    collector = OpenMeteoCollector(days_back=60, storage_type="print")
    print(json.dumps(collector.run(), indent=2, ensure_ascii=False))