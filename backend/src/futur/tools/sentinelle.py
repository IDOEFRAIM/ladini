
import json
import logging
from typing import Dict, Any, List, Optional
import requests
import re
import math
from .shared_math import SahelAgroMath

logger = logging.getLogger("SentinelleTool")

class SentinelleTool:
    """
    Outil d'analyse avancée pour l'Agent Sentinelle.
    Calcul les métriques composées (Stress hydrique, Risques).
    Utilise SahelAgroMath pour la cohérence des formules.
    """
    def __init__(self, llm_client=None):
        self.llm = llm_client
        self.model_planner = "llama-3.1-8b-instant"

    # ------------------------------------------------------------------ #
    # Utilitaires métiers                                                #
    # ------------------------------------------------------------------ #



    def _fetch_real_weather(self, location_profile: Dict[str, Any]) -> Dict[str, Any]:
        """Récupère la météo réelle via OpenMeteo."""
        # 1. Résolution des coordonnées
        # Par défaut Bobo-Dioulasso si non trouvé
        # Resolve coordinates (defaults applied inside helper)
        region_name = location_profile.get("zone", "").lower() or location_profile.get("village", "").lower()
        lat, lon = self._resolve_coords(region_name)

        # Call OpenMeteo and parse into sentinel-friendly structure
        try:
            data = self._call_open_meteo(lat, lon)
            current = data.get("current_weather", {})
            daily = data.get("daily", {})
            return self._format_open_meteo_response(current, daily)
        except Exception as e:
            logger.warning("Echec OpenMeteo: %s. Utilisation de valeurs par défaut.", e)
            return {"temp_c": 30.0, "precip_mm": 0.0, "et0": 5.0, "source": "Fallback (Error)"}


    def _resolve_coords(self, region_name: str) -> tuple[float, float]:
        """Map a region name to coordinates; falls back to Bobo-Dioulasso."""
        default = (11.1772, -4.2979)
        if not region_name:
            return default

        coords = {
            "bobo": (11.1772, -4.2979),
            "dedougou": (12.4634, -3.4607),
            "ouahigouya": (13.5828, -2.4216),
            "fada": (12.0627, 0.3578),
            "ouaga": (12.3714, -1.5197),
            "koudougou": (12.2526, -2.3627),
            "kaya": (13.0917, -1.0841),
            "dori": (14.0353, -0.0344),
            "nouna": (12.7296, -3.8631),
        }

        for key, (plat, plon) in coords.items():
            if key in region_name:
                return plat, plon
        return default


    def _call_open_meteo(self, lat: float, lon: float) -> Dict[str, Any]:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "current_weather": True,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,et0_fao_evapotranspiration",
            "timezone": "auto",
        }
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        return resp.json()


    def _format_open_meteo_response(self, current: Dict[str, Any], daily: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "temp_c": current.get("temperature"),
            "wind_kph": current.get("windspeed"),
            "condition_code": current.get("weathercode"),
            "precip_mm": daily.get("precipitation_sum", [0])[0] if daily.get("precipitation_sum") else 0.0,
            "t_max": daily.get("temperature_2m_max", [35])[0] if daily.get("temperature_2m_max") else 35.0,
            "t_min": daily.get("temperature_2m_min", [25])[0] if daily.get("temperature_2m_min") else 25.0,
            "et0": daily.get("et0_fao_evapotranspiration", [5])[0] if daily.get("et0_fao_evapotranspiration") else 5.0,
            "source": "OpenMeteo RealTime",
        }


    def _moderate_request(self, query: str) -> Dict[str, Any]:
        """Détecte les tentatives d'arnaque ou demandes hors-sujet."""
        if not self.llm:
            return {"is_scam": False}

        prompt = (
            "Tu es le modérateur de sécurité d'AgriConnect. "
            "Analyse ce message pour détecter : arnaque financière, phishing, fausse nouvelle climatique alarmiste.\n"
            f"Message : {query}\n"
            "Format JSON : {'is_scam': boolean, 'reason': 'explication'}"
        )
        
        try:
            resp = self.llm.chat.completions.create(
                model=self.model_planner,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                response_format={"type": "json_object"}
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            logger.warning("Erreur modération: %s", e)
            return {"is_scam": False}

    def _build_context(self, nodes) -> str:
        """Construit le contexte texte à partir des nœuds retrouvés."""
        if not nodes:
            return ""
        return "\n\n".join([n.text for n in nodes])

    def _serialize_sources(self, nodes) -> List[Dict[str, Any]]:
        """Extrait les métadonnées des sources."""
        sources = []
        for i, n in enumerate(nodes, 1):
            meta = n.metadata or {}
            sources.append({
                "index": i,
                "title": meta.get("file_name", "Document inconnu"),
                "score": n.score if hasattr(n, "score") else 0.0
            })
        return sources

    def _format_location(self, loc: Dict[str, Any]) -> str:
        """Formate la localisation pour le prompt."""
        if not loc:
            return "Zone inconnue"
        parts = [loc.get("village"), loc.get("zone"), loc.get("country")]
        return ", ".join([p for p in parts if p])

    
        return "Surveillez l'humidité du sol. Paillez si la terre craquelle."


    def _compute_metrics(self, weather: Dict[str, Any], satellite: Dict[str, Any]) -> Dict[str, Any]:
        parsed = self._parse_weather_sat_inputs(weather, satellite)
        t_min = parsed["t_min"]
        t_max = parsed["t_max"]
        precip = parsed["precip"]
        humidity = parsed["humidity"]
        wind = parsed["wind"]
        dry_days = parsed["dry_days"]
        ndvi = parsed["ndvi"]
        doy = parsed["doy"]
        lat = parsed["lat"]

        temp_avg = (t_min + t_max) / 2

        # Utilisation de la mathématique partagée pour ET0
        et0 = self._compute_et0(t_min, t_max, lat, doy)

        # Calcul Indice Humidité Sol simplifié
        soil_moisture_idx = self._compute_soil_moisture_idx(precip, humidity, et0)

        heat_flag = self._is_heat_flag(t_max, temp_avg)

        return {
            "temp_min_c": round(t_min, 1),
            "temp_max_c": round(t_max, 1),
            "temp_avg_c": round(temp_avg, 1),
            "precip_mm": round(precip, 1),
            "et0_mm": round(et0, 1),
            "soil_moisture_idx": round(soil_moisture_idx, 2),
            "humidity_pct": round(humidity, 1),
            "wind_speed_kmh": round(wind, 1),
            "dry_days_ahead": dry_days,
            "ndvi_anomaly": round(ndvi, 2),
            "heat_stress": heat_flag,
        }

    def synthesize_agronomic_advice(self, metrics: Dict[str, Any], crop_profile: Optional[Any]) -> Dict[str, Any]:
        """
        Fusionne Météo + Agronomie pour donner un conseil précis (Context Synapse).
        Calcule Delta T et Besoins Hydriques Nets.
        """
        # 1. Sécurité Pulvérisation (Delta T)
        # On utilise T_max et Humidité pour le pire cas de l'après-midi
        t_ref = metrics.get("temp_max_c", 35)
        rh_ref = metrics.get("humidity_pct", 40)
        
        try:
            delta_t, condition = SahelAgroMath.calculate_delta_t(t_ref, rh_ref)
        except Exception:
            delta_t, condition = 0.0, "INCONNU"

        can_spray = (2 <= delta_t <= 10)
        spray_advice = "✅ Favorable" if can_spray else f"⛔ Risqué ({condition})"

        advice = {
            "delta_t": delta_t,
            "spray_condition": condition,
            "spray_advice": spray_advice,
            "can_spray": can_spray
        }

        # 2. Besoins Hydriques (ETc vs Pluie)
        if crop_profile:
            # Par défaut, on prend le Kc 'mid' (pleine croissance) pour sécuriser la plante
            kc = crop_profile.kc.get('mid', 1.0)
            et0 = metrics.get("et0_mm", 0.0)
            precip = metrics.get("precip_mm", 0.0)
            
            etc_mm = et0 * kc
            balance = precip - etc_mm # Positif = Excédent, Négatif = Déficit
            
            missing_water_l_m2 = max(0.0, -balance)
            
            advice.update({
                "crop_name": crop_profile.name,
                "kc_used": kc,
                "etc_mm": round(etc_mm, 1),
                "water_balance_mm": round(balance, 1),
                "irrigation_needed_l_m2": round(missing_water_l_m2, 1)
            })

            if balance < -2:
                advice["irrigation_msg"] = f"🚨 URGENCE SOIF : Il manque {missing_water_l_m2:.1f} litres/m² aujourd'hui."
                advice["agronomic_status"] = "CRITIQUE"
            elif balance < 0:
                advice["irrigation_msg"] = f"⚠️ DÉFICIT LÉGER : Complétez avec {missing_water_l_m2:.1f} litres/m²."
                advice["agronomic_status"] = "VIGILANCE"
            else:
                advice["irrigation_msg"] = "✅ PAS D'ARROSAGE : La pluie suffit largement."
                advice["agronomic_status"] = "OPTIMAL"
        
        else:
            advice["irrigation_msg"] = "Précisez votre culture pour un calcul de besoin en eau exact."
            advice["agronomic_status"] = "NEUTRE"

        return advice

    def _parse_weather_sat_inputs(self, weather: Dict[str, Any], satellite: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize and parse inputs for `_compute_metrics` to centralize defaults and casting.

        This function delegates to smaller parsers to keep cyclomatic complexity low.
        """
        temps = self._parse_temperatures(weather)
        phys = self._parse_precip_humidity_wind(weather)
        misc = self._parse_misc(weather, satellite)

        return {**temps, **phys, **misc}


    def _parse_temperatures(self, weather: Dict[str, Any]) -> Dict[str, Any]:
        try:
            t_min = float(weather.get("temperature_min_c", weather.get("t_min", 22.0)) or 22.0)
        except Exception:
            t_min = 22.0
        try:
            t_max = float(weather.get("temperature_max_c", weather.get("t_max", 32.0)) or 32.0)
        except Exception:
            t_max = 32.0
        return {"t_min": t_min, "t_max": t_max}


    def _parse_precip_humidity_wind(self, weather: Dict[str, Any]) -> Dict[str, Any]:
        try:
            precip = float(weather.get("forecast_precip_mm", weather.get("precip_mm", weather.get("precip", 0.0))) or 0.0)
        except Exception:
            precip = 0.0
        try:
            humidity = float(weather.get("humidity", weather.get("rh", 45.0)) or 45.0)
        except Exception:
            humidity = 45.0
        try:
            wind = float(weather.get("wind_speed_kmh", weather.get("wind_speed", 8.0)) or 8.0)
        except Exception:
            wind = 8.0
        return {"precip": precip, "humidity": humidity, "wind": wind}


    def _parse_misc(self, weather: Dict[str, Any], satellite: Dict[str, Any]) -> Dict[str, Any]:
        # Delegate to two focused parsers to keep complexity low
        misc1 = self._parse_dry_and_ndvi(weather, satellite)
        misc2 = self._parse_doy_and_lat(weather)
        return {**misc1, **misc2}


    def _parse_dry_and_ndvi(self, weather: Dict[str, Any], satellite: Dict[str, Any]) -> Dict[str, Any]:
        try:
            dry_days = int(weather.get("dry_days_ahead", 0) or 0)
        except Exception:
            dry_days = 0
        try:
            ndvi = float(satellite.get("ndvi_anomaly", 0.0) or 0.0)
        except Exception:
            ndvi = 0.0
        return {"dry_days": dry_days, "ndvi": ndvi}


    def _parse_doy_and_lat(self, weather: Dict[str, Any]) -> Dict[str, Any]:
        try:
            doy = int(weather.get("doy", 180) or 180)
        except Exception:
            doy = 180
        try:
            lat = float(weather.get("lat", 12.0) or 12.0)
        except Exception:
            lat = 12.0
        return {"doy": doy, "lat": lat}


    def _compute_et0(self, t_min: float, t_max: float, lat: float, doy: int) -> float:
        """Compute ET0 using SahelAgroMath; instantiate the math tool internally to
        reduce the number of arguments and improve cohesion.
        """
        try:
            math_tool = SahelAgroMath()
            return math_tool.calculate_hargreaves_et0(t_min, t_max, lat, doy)
        except Exception:
            return 0.0


    def _is_heat_flag(self, t_max: float, temp_avg: float) -> bool:
        return bool(t_max >= 38 or temp_avg >= 32)


    def _compute_soil_moisture_idx(self, precip: float, humidity: float, et0: float) -> float:
        """Normalized soil moisture index helper."""
        try:
            val = (precip + humidity / 10 - et0) / 10.0
            return max(0.0, min(1.0, val))
        except Exception:
            return 0.0

    def _assess_flood_risk(
        self,
        weather: Dict[str, Any],
        satellite: Dict[str, Any],
        location: Dict[str, Any],
    ) -> Dict[str, Any]:
        precip = float(
            weather.get("forecast_precip_mm", weather.get("precip_mm", weather.get("precip", 0.0))) or 0.0
        )
        flood_prob = float(satellite.get("flood_probability", 0.0) or 0.0)
        soil = float(weather.get("soil_moisture", 0.0) or 0.0)

        # Aggregate scoring via helpers to reduce branching in the primary method
        score = 0.0
        score += self._score_from_precip(precip)
        score += self._score_from_flood_prob(flood_prob)
        score += self._score_from_soil(soil)

        level, message = self._flood_level_from_score(score)

        return {
            "score": round(score, 2),
            "risk_level": level,
            "alert_message": message,
            "precip_indicator_mm": round(precip, 1),
            "flood_probability": round(flood_prob, 2),
            "soil_saturation": round(soil, 2),
            "location": location.get("village") or "non précisé",
        }


    def _score_from_precip(self, precip: float) -> float:
        if precip >= 30:
            return 2.5
        if precip >= 20:
            return 1.5
        if precip >= 10:
            return 1.0
        return 0.0


    def _score_from_flood_prob(self, flood_prob: float) -> float:
        if flood_prob >= 0.6:
            return 2.5
        if flood_prob >= 0.4:
            return 1.5
        return 0.0


    def _score_from_soil(self, soil: float) -> float:
        return 1.0 if soil >= 0.7 else 0.0


    def _flood_level_from_score(self, score: float) -> tuple[str, str]:
        if score >= 4:
            return (
                "CRITIQUE",
                "Risque d'inondation imminent : sécuriser les intrants, prévoir des drains.",
            )
        if score >= 2.5:
            return (
                "ÉLEVÉ",
                "Sol saturé et pluie forte : ouvrir les rigoles, surveiller les parcelles basses.",
            )
        if score >= 1.5:
            return (
                "MODÉRÉ",
                "Arrosage naturel important : privilégier la rétention d'eau.",
            )
        return ("FAIBLE", "Pas de signe d'inondation à court terme.")

    def _derive_hazards(self, metrics: Dict[str, Any], flood: Dict[str, Any]) -> List[Dict[str, Any]]:
        hazards: List[Dict[str, Any]] = []

        # Collect hazard candidates via focused checkers
        candidates = [
            self._check_drought_hazard(metrics),
            self._check_flood_hazard(flood),
            self._check_heat_hazard(metrics),
            self._check_soil_stress_hazard(metrics),
            self._check_ndvi_hazard(metrics),
            self._check_wind_hazard(metrics),
        ]

        for cand in candidates:
            if not cand:
                continue
            # avoid duplicates by label
            if any(h.get("label") == cand.get("label") for h in hazards):
                continue
            hazards.append(cand)

        return hazards


    def _check_drought_hazard(self, metrics: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if metrics.get("precip_mm", 0.0) < 0.1 and metrics.get("et0_mm", 0.0) >= 4.0:
            severity = "CRITIQUE" if metrics.get("et0_mm", 0.0) > 6.0 else "HAUTE"
            return {
                "label": "Sécheresse / Harmattan critique",
                "severity": severity,
                "advice": "PAILLAGE OBLIGATOIRE : Couvrez le sol pour bloquer l'évaporation. IRRIGATION DE NUIT : Arrosez entre 22h et 5h pour maximiser l'absorption.",
                "explanation": f"L'absence de pluie (0.0mm) est confirmée par l'air sec. L'Harmattan 'vole' {metrics.get('et0_mm')}mm d'eau à votre sol chaque jour.",
            }
        return None


    def _check_flood_hazard(self, flood: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if flood.get("risk_level") in {"ÉLEVÉ", "CRITIQUE"}:
            return {
                "label": "Risque d'inondation",
                "severity": flood.get("risk_level"),
                "advice": "ZAÏ ET CORDONS PIERREUX : Préparez vos parcelles avec des aménagements physiques pour freiner l'eau et favoriser l'infiltration.",
                "explanation": f"Menace d'excès d'eau : {flood.get('alert_message', '')}",
            }
        return None


    def _check_heat_hazard(self, metrics: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if metrics.get("heat_stress"):
            return {
                "label": "Vague de chaleur extrême",
                "severity": "ÉLEVÉ",
                "advice": "CONSERVATION DE L'EAU : N'apportez pas d'engrais minéraux (Urée) tant que le sol n'est pas bien refroidi. Créez des ombrages si possible.",
                "explanation": f"La température de {metrics.get('temp_max_c')}°C brûle les feuilles et assèche la sève.",
            }
        return None


    def _check_soil_stress_hazard(self, metrics: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if metrics.get("soil_moisture_idx", 0) <= 0.2 and metrics.get("precip_mm", 0) < 5:
            return {
                "label": "Stress hydrique du sol",
                "severity": "MOYEN",
                "advice": "Réduisez l'évaporation par un paillage épais et privilégiez les micro-doses d'eau au pied des plants.",
                "explanation": f"L'indice d'humidité de {metrics.get('soil_moisture_idx')} indique un sol proche du point de flétrissement.",
            }
        return None


    def _check_ndvi_hazard(self, metrics: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if metrics.get("ndvi_anomaly", 0) <= -0.2:
            return {
                "label": "Stress végétatif détecté",
                "severity": "ÉLEVÉ",
                "advice": "Inspecter les cultures pour ravageurs ou carences et corriger rapidement.",
                "explanation": f"Anomalie NDVI {metrics.get('ndvi_anomaly')}.",
            }
        return None


    def _check_wind_hazard(self, metrics: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if metrics.get("wind_speed_kmh", 0) >= 35:
            return {
                "label": "Vent fort",
                "severity": "ÉLEVÉ",
                "advice": "Reporter les pulvérisations et arrimer les abris.",
                "explanation": f"Vent prévu {metrics.get('wind_speed_kmh')} km/h.",
            }
        return None

    def _plan_retrieval(
        self,
        query: str,
        risk_summary: str,
        hazards: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        fallback = {
            "optimized_query": query,
            "warnings": ["Planification RAG automatique indisponible."],
        }

        if not self.llm:
            return fallback

        hazard_text = "; ".join(
            f"{item['label']} (niveau {item['severity']})"
            for item in hazards[:3]
        ) or "pas de risque critique"

        planner_prompt = (
            "Prépare une requête documentaire pour un agent de veille climatique.\n"
            f"Question terrain: {query}\n"
            f"Risques détectés: {hazard_text}\n"
            f"Résumé: {risk_summary}\n"
            'Retourne un JSON {"optimized_query": "...", "warnings": ["..."]}.'
        )

        try:
            completion = self.llm.chat.completions.create(
                model=self.model_planner,
                messages=[{"role": "user", "content": planner_prompt}],
                temperature=0.15,
                max_tokens=320,
                response_format={"type": "json_object"},
            )
            content = completion.choices[0].message.content
            if not content:
                raise ValueError("Réponse vide du planificateur.")
            data = json.loads(content)
            return {
                "optimized_query": data.get("optimized_query") or query,
                "warnings": data.get("warnings", []),
            }
        except Exception as exc:
            logger.warning("Planification échouée: %s", exc)
            return fallback

    # ------------------------------------------------------------------ #
    # Utilitaires sécurité & fallback                                    #
    # ------------------------------------------------------------------ #

    def _extract_json_block(self, text: str) -> Dict[str, Any]:
        matches = re.findall(r"\{[\s\S]*?\}", text)
        for match in matches:
            try:
                return json.loads(match)
            except json.JSONDecodeError:
                continue
        return json.loads(text)

    def _moderate_request(self, query: str) -> Dict[str, Any]:
        if not self.llm:
            return {"is_scam": False, "reason": "LLM indisponible"}
        system_prompt = (
            "Tu es le garde-fou d'AgriConnect. Détecte les arnaques, demandes d'argent ou phishing. "
            'Réponds en JSON {"is_scam": boolean, "reason": "..."}.'
        )
        try:
            response = self.llm.chat.completions.create(
                model=self.model_planner,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": query},
                ],
                temperature=0.05,
                max_tokens=200,
            )
            content = (response.choices[0].message.content or "{}").strip() if response.choices else "{}"
            return self._extract_json_block(content)
        except Exception as exc:
            logger.warning("Contrôle anti-fraude indisponible : %s", exc)
            return {"is_scam": False, "reason": "Contrôle impossible"}
 
    def _fallback_response(
        self,
        query: str,
        location: str,
        hazards: List[Dict[str, Any]],
        risk_summary: str,
        metrics: Dict[str, Any],
        flood: Dict[str, Any],
        sources: List[Dict[str, Any]],
    ) -> str:
        parts: List[str] = []
        parts.extend(self._fallback_header_block(query, location, risk_summary))
        if flood:
            parts.extend(self._fallback_flood_block(flood))
        if metrics:
            parts.append(self._fallback_metrics_line(metrics))

        if hazards:
            parts.extend(self._fallback_hazards_block(hazards))
        else:
            parts.append("")
            parts.append("Actions recommandées: Surveillance régulière du champ.")

        if sources:
            parts.extend(self._fallback_sources_block(sources))

        parts.append("")
        parts.append("Consultez un technicien agricole si la situation évolue.")
        return "\n".join(parts)


    def _fallback_header_block(self, query: str, location: str, risk_summary: str) -> List[str]:
        return [
            "⚠️ Alerte en mode dégradé (LLM/RAG indisponible).",
            f"Question: {query}",
            f"Zone: {location}",
            "",
            "Évaluation rapide:",
            risk_summary or "Pas d'information sur les risques.",
        ]


    def _fallback_flood_block(self, flood: Dict[str, Any]) -> List[str]:
        return ["", f"Risque inondation: {flood.get('risk_level', 'FAIBLE')} - {flood.get('alert_message', '')}"]


    def _fallback_metrics_line(self, metrics: Dict[str, Any]) -> str:
        return (
            f"Température max {metrics.get('temp_max_c', '?')} °C | Pluie prévue {metrics.get('precip_mm', '?')} mm "
            f"| ET0 {metrics.get('et0_mm', '?')} mm | Humidité sol {metrics.get('soil_moisture_idx', '?')}"
        )


    def _fallback_hazards_block(self, hazards: List[Dict[str, Any]]) -> List[str]:
        lines = ["", "Actions recommandées:"]
        for idx, hazard in enumerate(hazards, start=1):
            lines.append(f"{idx}. {hazard.get('advice', '')} ({hazard.get('label', '')})")
        return lines


    def _fallback_sources_block(self, sources: List[Dict[str, Any]]) -> List[str]:
        ref = ", ".join(item.get("title") or item.get("filename") or f"Source {item['index']}" for item in sources)
        return ["", f"Sources à consulter: {ref}"]

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #

    def _build_context(self, nodes: List[Any]) -> str:
        chunks = []
        for idx, node in enumerate(nodes, start=1):
            metadata = node.node.metadata or {}
            title = metadata.get("title") or metadata.get("filename") or f"Source {idx}"
            chunks.append(f"[Source {idx} | {title}]\n{node.node.get_content().strip()}")
        return "\n\n".join(chunks)

    def _serialize_sources(self, nodes: List[Any]) -> List[Dict[str, Any]]:
        payload: List[Dict[str, Any]] = []
        for idx, node in enumerate(nodes, start=1):
            metadata = node.node.metadata or {}
            payload.append(
                {
                    "index": idx,
                    "title": metadata.get("title"),
                    "filename": metadata.get("filename"),
                    "score": float(node.score) if node.score is not None else None,
                }
            )
        return payload

    def _format_location(self, profile: Dict[str, Any]) -> str:
        if not profile:
            return "Localisation non précisée"
        parts = []
        if name := profile.get("village"):
            parts.append(name)
        if zone := profile.get("zone"):
            parts.append(zone)
        if country := profile.get("country"):
            parts.append(country)
        return ", ".join(parts) or "Localisation non précisée"

    def _simulate_weather(self, location: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "precip_mm": 0.0,
            "forecast_precip_mm": 0.0,
            "wind_speed_kmh": 8.0,
            "dry_days_ahead": 2,
            "temperature_max_c": 34.0,
            "temperature_min_c": 22.0,
            "humidity": 45.0,
            "location_label": location.get("village") or "inconnu",
        }



