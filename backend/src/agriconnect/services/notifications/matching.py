import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, text

from agriconnect.core.settings import settings

logger = logging.getLogger(__name__)


class NotificationMatcher:
    """
    Compare latest weather/market signals against user profile
    (zone + crops) and generate contextual push messages.
    """

    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url or settings.DATABASE_URL
        self.engine = create_engine(self.db_url, pool_pre_ping=True)

    def ensure_tables(self) -> None:
        ddl = [
            "CREATE SCHEMA IF NOT EXISTS agri_notify",
            """
            CREATE TABLE IF NOT EXISTS agri_notify.matches (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                zone_name TEXT NOT NULL,
                crop_name TEXT,
                weather_alert_type TEXT,
                market_signal_type TEXT,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                dispatched_at TIMESTAMPTZ
            )
            """,
        ]
        with self.engine.begin() as conn:
            for statement in ddl:
                conn.execute(text(statement))

    def _read_users_and_signals(self) -> List[Dict[str, Any]]:
        # Keep SQL defensive to support mixed schemas in existing environments.
        query = text(
            """
            WITH latest_weather AS (
                SELECT DISTINCT ON (zone_name)
                    zone_name,
                    alert_type,
                    severity,
                    details,
                    forecast_date
                FROM agri_weather.alerts
                ORDER BY zone_name, created_at DESC
            ), latest_market AS (
                SELECT DISTINCT ON (zone_name, commodity)
                    zone_name,
                    commodity,
                    signal_type,
                    current_price,
                    detected_at
                FROM agri_market.put_call_signals
                ORDER BY zone_name, commodity, detected_at DESC
            )
            SELECT
                u.id::text AS user_id,
                COALESCE(z.name, 'Unknown') AS zone_name,
                uc.crop_name,
                lw.alert_type,
                lw.severity,
                lw.details,
                lm.signal_type,
                lm.commodity,
                lm.current_price
            FROM users u
            LEFT JOIN zones z ON z.id = u."zoneId"
            LEFT JOIN user_crops uc ON uc.user_id = u.id::text
            LEFT JOIN latest_weather lw ON lw.zone_name = COALESCE(z.name, 'Unknown')
            LEFT JOIN latest_market lm ON lm.zone_name = COALESCE(z.name, 'Unknown')
            WHERE u.id IS NOT NULL
            """
        )
        with self.engine.begin() as conn:
            rows = conn.execute(query).mappings().all()
        return [dict(r) for r in rows]

    def build_messages(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        for row in rows:
            weather = row.get("alert_type")
            market = row.get("signal_type")
            if not weather and not market:
                continue

            zone = row.get("zone_name") or "votre zone"
            crop = row.get("crop_name") or row.get("commodity") or "votre culture"
            user_id = row.get("user_id")

            if weather == "drought_risk" and market in {"PUT_ACTIVABLE", "CALL_ACTIVABLE"}:
                message = (
                    f"Risque de secheresse a {zone}, vos options Put sur {crop} sont activables. "
                    "Ajustez irrigation et couverture de prix aujourd'hui."
                )
            elif weather:
                message = f"Alerte meteo ({weather}) dans {zone}. Surveillez {crop} et adaptez vos actions terrain."
            else:
                message = f"Signal marche ({market}) detecte pour {crop} a {zone}. Verifiez vos options de couverture."

            messages.append(
                {
                    "user_id": user_id,
                    "zone_name": zone,
                    "crop_name": crop,
                    "weather_alert_type": weather,
                    "market_signal_type": market,
                    "message": message,
                }
            )
        return messages

    def persist_matches(self, messages: List[Dict[str, Any]]) -> int:
        if not messages:
            return 0
        insert_sql = text(
            """
            INSERT INTO agri_notify.matches (
                user_id, zone_name, crop_name, weather_alert_type,
                market_signal_type, message, status
            ) VALUES (
                :user_id, :zone_name, :crop_name, :weather_alert_type,
                :market_signal_type, :message, 'PENDING'
            )
            """
        )
        with self.engine.begin() as conn:
            for msg in messages:
                conn.execute(insert_sql, msg)
        return len(messages)

    def dispatch_pending(self, limit: int = 200) -> int:
        # In production, this task should handoff to WhatsApp/SMS push workers.
        fetch_sql = text(
            """
            SELECT id, user_id, message
            FROM agri_notify.matches
            WHERE status = 'PENDING'
            ORDER BY created_at ASC
            LIMIT :limit
            """
        )
        done_sql = text(
            """
            UPDATE agri_notify.matches
            SET status = 'SENT', dispatched_at = :now
            WHERE id = :id
            """
        )

        sent = 0
        with self.engine.begin() as conn:
            rows = conn.execute(fetch_sql, {"limit": limit}).mappings().all()
            now = datetime.now(timezone.utc)
            for row in rows:
                logger.info("Dispatching notification user=%s id=%s", row["user_id"], row["id"])
                conn.execute(done_sql, {"id": row["id"], "now": now})
                sent += 1

        return sent

    def run_matching(self) -> Dict[str, int]:
        self.ensure_tables()
        rows = self._read_users_and_signals()
        messages = self.build_messages(rows)
        persisted = self.persist_matches(messages)
        return {"candidates": len(rows), "messages": len(messages), "persisted": persisted}
