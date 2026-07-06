import asyncio
from datetime import datetime, date, timedelta
from typing import List, Dict, Any
import aiohttp
from pydantic import BaseModel, Field

class FormattedWeatherData(BaseModel):
    record_date: str
    forecast_horizon_days: int
    
    # Ambiance Air
    temp_min: float
    temp_max: float
    temp_mean: float
    precipitation_mm: float
    precipitation_probability: float
    humidity_percent: float
    wind_speed_kmh: float
    uv_index_max: float

    # Agronomie & Sols (Agrégés proprement depuis le flux horaire)
    et0_evapotranspiration_mm: float
    soil_temperature_6cm: float          # Moyenne de la journée sur la couche arable
    soil_moisture_3_to_9cm: float        # Moyenne de la journée sur la couche racinaire
    
    # Indicateurs Métiers calculés pour l'Agent IA
    gdd_contribution: float
    water_deficit_mm: float
    fertilization_allowed: bool
    treatment_allowed: bool
    disease_risk_level: str
    
    raw_payload: Dict[str, Any]


class OpenMeteoService:
    BASE_URL = "https://api.open-meteo.com/v1/forecast"

    @classmethod
    async def fetch_weather_data(cls, latitude: float, longitude: float) -> List[FormattedWeatherData]:
        """
        Récupère les données d'ambiance en daily et les données agronomiques en hourly 
        pour éviter l'erreur 400, puis fusionne le tout.
        """
        params = {
            "latitude": latitude,
            "longitude": longitude,
            # Variables d'ambiance supportées de manière stable en daily
            "daily": [
                "temperature_2m_max",
                "temperature_2m_min",
                "temperature_2m_mean",
                "precipitation_sum",
                "precipitation_probability_max",
                "relative_humidity_2m_mean",
                "wind_speed_10m_max",
                "uv_index_max"
            ],
            # Variables hydro-agronomiqes disponibles de manière stable en hourly
            "hourly": [
                "et0_fao_evapotranspiration",
                "soil_temperature_6cm",
                "soil_moisture_3_to_9cm"
            ],
            "timezone": "Africa/Abidjan",
            "past_days": 1
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(cls.BASE_URL, params=params) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        print(f"❌ Erreur API Open-Meteo: Statut {response.status}")
                        print(f"📝 Rejet de l'API : {error_text}")
                        return []
                    
                    data = await response.json()
                    return cls._format_response(data)
            except Exception as e:
                print(f"❌ Échec de la connexion [Lat: {latitude}, Lng: {longitude}]: {str(e)}")
                return []

    @classmethod
    def _format_response(cls, data: Dict[str, Any]) -> List[FormattedWeatherData]:
        daily_data = data.get("daily", {})
        hourly_data = data.get("hourly", {})
        
        if not daily_data or "time" not in daily_data:
            return []

        formatted_records = []
        today_date = date.today()
        total_days = len(daily_data["time"])

        # Pré-découpage des données horaires par tranche de 24 heures
        hourly_times = hourly_data.get("time", [])
        hourly_et0 = hourly_data.get("et0_fao_evapotranspiration", [])
        hourly_soil_temp = hourly_data.get("soil_temperature_6cm", [])
        hourly_soil_moist = hourly_data.get("soil_moisture_3_to_9cm", [])

        for i in range(total_days):
            record_date_str = daily_data["time"][i]
            record_date_obj = datetime.strptime(record_date_str, "%Y-%m-%d").date()

            diff_days = (record_date_obj - today_date).days
            forecast_horizon = 0 if diff_days <= 0 else diff_days

            # 1. Extraction des données quotidiennes d'ambiance
            t_max = daily_data.get("temperature_2m_max", [0.0]*total_days)[i]
            t_min = daily_data.get("temperature_2m_min", [0.0]*total_days)[i]
            t_mean = daily_data.get("temperature_2m_mean", [0.0]*total_days)[i]
            precip = daily_data.get("precipitation_sum", [0.0]*total_days)[i] or 0.0
            precip_prob = daily_data.get("precipitation_probability_max", [0.0]*total_days)[i] or 0.0
            humidity = daily_data.get("relative_humidity_2m_mean", [0.0]*total_days)[i] or 0.0
            wind = daily_data.get("wind_speed_10m_max", [0.0]*total_days)[i] or 0.0
            uv_idx = daily_data.get("uv_index_max", [0.0]*total_days)[i] or 0.0

            # 2. Agrégation manuelle et sécurisée des données horaires pour la journée courante (i)
            start_hour = i * 24
            end_hour = start_hour + 24
            
            day_et0_slices = hourly_et0[start_hour:end_hour]
            day_soil_temp_slices = hourly_soil_temp[start_hour:end_hour]
            day_soil_moist_slices = hourly_soil_moist[start_hour:end_hour]

            # Calculs des moyennes / sommes sur les 24 heures de la journée
            et0_sum = sum(filter(None, day_et0_slices)) if day_et0_slices else 0.0
            soil_temp_mean = (sum(filter(None, day_soil_temp_slices)) / len(day_soil_temp_slices)) if day_soil_temp_slices else 0.0
            soil_moist_mean = (sum(filter(None, day_soil_moist_slices)) / len(day_soil_moist_slices)) if day_soil_moist_slices else 0.0

            # ⚙️ CALCULS AGRONOMIQUES
            adjusted_max = min(t_max, 35.0)
            adjusted_min = max(t_min, 10.0)
            gdd = max(((adjusted_max + adjusted_min) / 2.0) - 10.0, 0.0)

            water_deficit = max(et0_sum - precip, 0.0)
            
            # Logique d'aide à la décision (Intrants)
            fertilization_allowed = not (wind > 19.0 or precip > 15.0 or (precip > 2.0 and precip_prob > 75.0))
            treatment_allowed = wind <= 19.0

            # Évaluation du risque de maladies
            if humidity > 85.0 and (18.0 <= t_mean <= 26.0):
                disease_risk = "CRITICAL"
            elif humidity > 70.0 and (15.0 <= t_mean <= 28.0):
                disease_risk = "MEDIUM"
            elif humidity < 50.0 and t_max > 32.0:
                disease_risk = "HIGH_OIDIUM_RISK"
            else:
                disease_risk = "LOW"

            record = FormattedWeatherData(
                record_date=record_date_str,
                forecast_horizon_days=forecast_horizon,
                temp_min=t_min,
                temp_max=t_max,
                temp_mean=t_mean,
                precipitation_mm=precip,
                precipitation_probability=precip_prob,
                humidity_percent=humidity,
                wind_speed_kmh=wind,
                uv_index_max=uv_idx,
                et0_evapotranspiration_mm=round(et0_sum, 2),
                soil_temperature_6cm=round(soil_temp_mean, 1),
                soil_moisture_3_to_9cm=round(soil_moist_mean, 3),
                gdd_contribution=round(gdd, 2),
                water_deficit_mm=round(water_deficit, 2),
                fertilization_allowed=fertilization_allowed,
                treatment_allowed=treatment_allowed,
                disease_risk_level=disease_risk,
                raw_payload={"daily_index": i, "api_timezone": data.get("timezone")}
            )
            formatted_records.append(record)

        return formatted_records


if __name__ == "__main__":
    async def main():
        lat, lng = 12.3714, -1.5197
        print(f"📡 Relance finale du collecteur Haute-Précision [Lat: {lat}, Lng: {lng}]...\n")
        
        results = await OpenMeteoService.fetch_weather_data(lat, lng)
        
        if results:
            for r in results[:3]:
                print(f"--- 📅 RAPPORT AGRONOMIQUE : {r.record_date} (Horizon: {r.forecast_horizon_days}) ---")
                print(f"🌡️ Temp Air  : {r.temp_min}°C à {r.temp_max}°C | Moyenne : {r.temp_mean}°C | UV : {r.uv_index_max}")
                print(f"🟫 Terre     : Temp Sol (Moyenne) : {r.soil_temperature_6cm}°C | Humidité Sol : {r.soil_moisture_3_to_9cm} m³/m³")
                print(f"💧 Eau       : Évapotranspiration (ET0 Cumulée) : {r.et0_evapotranspiration_mm}mm | Pluie : {r.precipitation_mm}mm")
                print(f"📈 Croissance : +{r.gdd_contribution} GDD | Déficit Eau : {r.water_deficit_mm}mm")
                print(f"🛡️ Décisions : Fertilisation OK : {'✅' if r.fertilization_allowed else '❌'} | Phyto OK : {'✅' if r.treatment_allowed else '❌'}")
                print(f"⚠️ Maladies  : Risque : {r.disease_risk_level}\n")
        else:
            print("❌ Le traitement a échoué.")

    asyncio.run(main())