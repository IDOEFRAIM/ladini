import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import boto3
import psycopg2
import requests
from psycopg2.extras import RealDictCursor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger()
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))


@dataclass
class Farmer:
    farmer_id: str
    phone: str
    lat: Optional[float]
    lon: Optional[float]
    crop: str
    zone_id: Optional[str] = None


@dataclass
class WeatherSnapshot:
    precipitation_mm: float
    humidity_pct: float
    wind_kmh: float
    etp_mm: float


def get_env(name: str, required: bool = True, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if required and (value is None or str(value).strip() == ""):
        raise ValueError(f"Missing required environment variable: {name}")
    return str(value) if value is not None else ""


def get_db_connection() -> psycopg2.extensions.connection:
    host = get_env("DB_HOST")
    dbname = get_env("DB_NAME")
    user = get_env("DB_USER")
    password = get_env("DB_PASSWORD")
    port = int(get_env("DB_PORT", required=False, default="5432"))
    connect_timeout = int(get_env("DB_CONNECT_TIMEOUT", required=False, default="10"))
    sslmode = get_env("DB_SSLMODE", required=False, default="require")
    sslrootcert = get_env("DB_SSLROOTCERT", required=False, default=None)

    logger.info("Connecting to PostgreSQL host=%s db=%s port=%s sslmode=%s", host, dbname, port, sslmode)
    connect_params = dict(
        host=host,
        dbname=dbname,
        user=user,
        password=password,
        port=port,
        connect_timeout=connect_timeout,
        sslmode=sslmode,
    )
    if sslrootcert:
        connect_params["sslrootcert"] = sslrootcert

    return psycopg2.connect(**connect_params)


def get_active_farmers(conn: psycopg2.extensions.connection) -> List[Farmer]:
    default_sql = """
        SELECT
            u.id,
            u.phone,
            u.latitude AS lat,
            u.longitude AS lon,
            uc.culture_name AS crop,
            u.zone_id
        FROM auth.users u
        JOIN auth.user_cultures uc ON uc.user_id = u.id
        WHERE u.phone IS NOT NULL
          AND uc.status = 'active'
    """

    sql = get_env("FARMERS_SQL", required=False, default=default_sql)

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    farmers: List[Farmer] = []
    for row in rows:
        lat = row.get("lat")
        lon = row.get("lon")
        farmers.append(
            Farmer(
                farmer_id=str(row.get("id")),
                phone=str(row.get("phone")),
                lat=float(lat) if lat is not None else None,
                lon=float(lon) if lon is not None else None,
                crop=str(row.get("crop") or ""),
                zone_id=str(row.get("zone_id")) if row.get("zone_id") is not None else None,
            )
        )
    return farmers


def get_zone_coords(conn: psycopg2.extensions.connection, zone_id: str) -> Tuple[Optional[float], Optional[float]]:
    """Return (latitude, longitude) for a given zone_id, or (None, None) if not found."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT latitude, longitude FROM public.zones WHERE id = %s LIMIT 1", (zone_id,))
            row = cur.fetchone()
            if not row:
                return None, None
            lat = row.get("latitude")
            lon = row.get("longitude")
            return (float(lat) if lat is not None else None, float(lon) if lon is not None else None)
    except Exception:
        return None, None


def geo_bucket(lat: float, lon: float, decimals: int) -> Tuple[float, float]:
    return (round(lat, decimals), round(lon, decimals))


def build_retry_session(total_retries: int, backoff_factor: float) -> requests.Session:
    retry = Retry(
        total=total_retries,
        connect=total_retries,
        read=total_retries,
        status=total_retries,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        backoff_factor=backoff_factor,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)

    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def fetch_weather(
    session: requests.Session,
    lat: float,
    lon: float,
    timeout_s: int,
    api_url: str,
    api_key: str,
) -> WeatherSnapshot:
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "precipitation,relative_humidity_2m,wind_speed_10m",
        "daily": "et0_fao_evapotranspiration",
        "timezone": "auto",
    }

    if api_key:
        params["apikey"] = api_key

    response = session.get(api_url, params=params, timeout=timeout_s)
    response.raise_for_status()
    payload = response.json()

    current = payload.get("current", {})
    daily = payload.get("daily", {})

    precipitation = float(current.get("precipitation", 0.0) or 0.0)
    humidity = float(current.get("relative_humidity_2m", 0.0) or 0.0)
    wind = float(current.get("wind_speed_10m", 0.0) or 0.0)

    etp_values = daily.get("et0_fao_evapotranspiration") or []
    etp = float(etp_values[0]) if etp_values else 0.0

    return WeatherSnapshot(
        precipitation_mm=precipitation,
        humidity_pct=humidity,
        wind_kmh=wind,
        etp_mm=etp,
    )


def generate_advice(weather: WeatherSnapshot, crop_type: str) -> Dict[str, str]:
    crop = crop_type.lower().strip()

    alert = "Situation normale"
    actions: List[str] = []
    learning: List[str] = []

    if weather.precipitation_mm >= 20:
        alert = "Alerte pluie forte"
        actions.append("Reporter la pulvérisation et sécuriser le drainage des parcelles.")
    elif weather.precipitation_mm <= 1 and weather.etp_mm >= 4:
        alert = "Alerte stress hydrique"
        actions.append("Prioriser un apport d'eau sur les parcelles sensibles.")

    if weather.wind_kmh >= 25:
        actions.append("Éviter les traitements foliaires pendant les rafales.")

    if weather.humidity_pct >= 85:
        actions.append("Renforcer la surveillance des maladies fongiques.")

    if crop in {"maïs", "mais"}:
        actions.append("Contrôler la levée et compléter les manques de poquets si nécessaire.")
        learning.append("Le maïs est plus vulnérable aux maladies lorsque humidité élevée et faible aération coexistent.")
    elif crop in {"riz"}:
        actions.append("Maintenir un niveau d'eau régulier et vérifier l'état des diguettes.")
        learning.append("Le pilotage de l'eau reste le levier principal pour le rendement du riz.")
    elif crop in {"niébé", "niebe"}:
        actions.append("Surveiller les attaques de ravageurs au stade floraison/gousses.")
        learning.append("Le niébé tolère mieux la sécheresse, mais reste sensible aux pics de ravageurs.")
    else:
        actions.append("Adapter les interventions au stade phénologique observé aujourd'hui.")
        learning.append("Conserver un journal parcellaire améliore la qualité des décisions quotidiennes.")

    if not learning:
        learning.append("Comparer météo observée et rendement permet d'améliorer les décisions futures.")

    return {
        "alerte": alert,
        "action": " ".join(actions),
        "apprentissage": " ".join(learning),
    }


def build_message(farmer: Farmer, weather: WeatherSnapshot, advice: Dict[str, str]) -> str:
    return (
        f"Ladini - Conseil météo-agricole\n"
        f"Culture: {farmer.crop}\n"
        f"Météo: pluie={weather.precipitation_mm:.1f}mm, humidité={weather.humidity_pct:.0f}%, "
        f"vent={weather.wind_kmh:.1f}km/h, ETP={weather.etp_mm:.1f}mm\n"
        f"Alerte: {advice['alerte']}\n"
        f"Action: {advice['action']}\n"
        f"Apprentissage: {advice['apprentissage']}"
    )


def publish_message(
    sns_client: Any,
    message: str,
    farmer_phone: str,
    topic_arn: str,
    subject: str,
) -> Dict[str, Any]:
    if topic_arn:
        return sns_client.publish(
            TopicArn=topic_arn,
            Subject=subject[:100],
            Message=message,
            MessageAttributes={
                "phone": {"DataType": "String", "StringValue": farmer_phone}
            },
        )

    return sns_client.publish(
        PhoneNumber=farmer_phone,
        Message=message,
    )


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    logger.info("Lambda started with event=%s", json.dumps(event or {}))

    weather_api_url = get_env(
        "WEATHER_API_URL",
        required=False,
        default="https://api.open-meteo.com/v1/forecast",
    )
    weather_api_key = get_env("API_KEY", required=False, default="")

    sns_topic_arn = get_env("SNS_TOPIC_ARN", required=False, default="")
    message_subject = get_env("SNS_SUBJECT", required=False, default="Ladini Daily Advice")

    timeout_s = int(get_env("WEATHER_TIMEOUT_SECONDS", required=False, default="8"))
    retries = int(get_env("WEATHER_RETRIES", required=False, default="3"))
    backoff = float(get_env("WEATHER_BACKOFF", required=False, default="0.7"))
    bucket_decimals = int(get_env("GEO_ROUND_DECIMALS", required=False, default="1"))

    sns_client = boto3.client("sns")
    weather_session = build_retry_session(total_retries=retries, backoff_factor=backoff)

    conn: Optional[psycopg2.extensions.connection] = None

    weather_cache: Dict[Tuple[float, float], WeatherSnapshot] = {}

    result = {
        "processed": 0,
        "sent": 0,
        "failed": 0,
        "zones": 0,
    }

    try:
        conn = get_db_connection()
        farmers = get_active_farmers(conn)
        result["processed"] = len(farmers)

        logger.info("Loaded %s active farmers", len(farmers))

        for farmer in farmers:
            # Resolve coordinates: prefer farmer lat/lon, otherwise use zone centroid
            lat = farmer.lat
            lon = farmer.lon
            if lat is None or lon is None:
                if farmer.zone_id:
                    zlat, zlon = get_zone_coords(conn, farmer.zone_id)
                    if zlat is not None and zlon is not None:
                        lat, lon = zlat, zlon
                    else:
                        logger.warning("No coordinates for farmer %s (zone %s)", farmer.farmer_id, farmer.zone_id)
                        result["failed"] += 1
                        continue
                else:
                    logger.warning("No coordinates and no zone for farmer %s", farmer.farmer_id)
                    result["failed"] += 1
                    continue

            bucket = geo_bucket(lat, lon, bucket_decimals)

            if bucket not in weather_cache:
                weather_cache[bucket] = fetch_weather(
                    session=weather_session,
                    lat=bucket[0],
                    lon=bucket[1],
                    timeout_s=timeout_s,
                    api_url=weather_api_url,
                    api_key=weather_api_key,
                )

            weather = weather_cache[bucket]
            advice = generate_advice(weather, farmer.crop)
            message = build_message(farmer, weather, advice)

            try:
                publish_message(
                    sns_client=sns_client,
                    message=message,
                    farmer_phone=farmer.phone,
                    topic_arn=sns_topic_arn,
                    subject=message_subject,
                )
                result["sent"] += 1
            except Exception:
                result["failed"] += 1
                logger.exception("SNS publish failed for farmer_id=%s", farmer.farmer_id)

        result["zones"] = len(weather_cache)
        logger.info("Lambda completed: %s", result)
        return {
            "statusCode": 200,
            "body": json.dumps(result),
        }

    except Exception as exc:
        logger.exception("Lambda execution failed")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(exc), **result}),
        }

    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logger.warning("Failed to close DB connection cleanly")
