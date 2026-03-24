import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

import requests
from sqlalchemy import create_engine, text

from agriconnect.core.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class MarketPriceRecord:
    market_name: str
    zone_name: str
    commodity: str
    unit: str
    price_value: float
    currency: str
    collected_at: datetime
    source: str


class MarketCollector:
    """Collect market prices and derive put/call activation signals."""

    def __init__(self, db_url: Optional[str] = None, endpoint: Optional[str] = None):
        self.db_url = db_url or settings.DATABASE_URL
        self.engine = create_engine(self.db_url, pool_pre_ping=True)
        self.endpoint = endpoint or ""

    def ensure_tables(self) -> None:
        statements = [
            "CREATE SCHEMA IF NOT EXISTS agri_market",
            """
            CREATE TABLE IF NOT EXISTS agri_market.prices (
                id BIGSERIAL PRIMARY KEY,
                market_name TEXT NOT NULL,
                zone_name TEXT NOT NULL,
                commodity TEXT NOT NULL,
                unit TEXT NOT NULL DEFAULT 'kg',
                price_value DOUBLE PRECISION NOT NULL,
                currency TEXT NOT NULL DEFAULT 'XOF',
                collected_at TIMESTAMPTZ NOT NULL,
                source TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (market_name, zone_name, commodity, collected_at)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS agri_market.put_call_signals (
                id BIGSERIAL PRIMARY KEY,
                zone_name TEXT NOT NULL,
                commodity TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                trigger_price DOUBLE PRECISION NOT NULL,
                current_price DOUBLE PRECISION NOT NULL,
                details TEXT,
                detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
        ]
        with self.engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))

    def _fallback_seed_data(self) -> List[MarketPriceRecord]:
        now = datetime.now(timezone.utc)
        return [
            MarketPriceRecord("Marche Bobo", "Bobo-Dioulasso", "Mais", "kg", 270.0, "XOF", now, "seed"),
            MarketPriceRecord("Marche Ouaga", "Ouagadougou", "Mais", "kg", 305.0, "XOF", now, "seed"),
            MarketPriceRecord("Marche Bobo", "Bobo-Dioulasso", "Sorgho", "kg", 240.0, "XOF", now, "seed"),
        ]

    def collect_prices(self) -> List[MarketPriceRecord]:
        if not self.endpoint:
            return self._fallback_seed_data()

        try:
            response = requests.get(self.endpoint, timeout=12)
            response.raise_for_status()
            payload = response.json()
            now = datetime.now(timezone.utc)
            results: List[MarketPriceRecord] = []
            for item in payload.get("prices", []):
                results.append(
                    MarketPriceRecord(
                        market_name=item.get("market_name", "Unknown"),
                        zone_name=item.get("zone_name", "Unknown"),
                        commodity=item.get("commodity", "Unknown"),
                        unit=item.get("unit", "kg"),
                        price_value=float(item.get("price_value", 0.0)),
                        currency=item.get("currency", "XOF"),
                        collected_at=now,
                        source=item.get("source", self.endpoint),
                    )
                )
            return results
        except Exception as exc:
            logger.warning("Market endpoint unavailable, fallback data used: %s", exc)
            return self._fallback_seed_data()

    def persist_prices(self, records: Iterable[MarketPriceRecord]) -> int:
        self.ensure_tables()
        query = text(
            """
            INSERT INTO agri_market.prices (
                market_name, zone_name, commodity, unit, price_value,
                currency, collected_at, source
            ) VALUES (
                :market_name, :zone_name, :commodity, :unit, :price_value,
                :currency, :collected_at, :source
            )
            ON CONFLICT (market_name, zone_name, commodity, collected_at)
            DO UPDATE SET
                price_value = EXCLUDED.price_value,
                source = EXCLUDED.source
            """
        )
        count = 0
        with self.engine.begin() as conn:
            for record in records:
                conn.execute(
                    query,
                    {
                        "market_name": record.market_name,
                        "zone_name": record.zone_name,
                        "commodity": record.commodity,
                        "unit": record.unit,
                        "price_value": record.price_value,
                        "currency": record.currency,
                        "collected_at": record.collected_at,
                        "source": record.source,
                    },
                )
                count += 1
        return count

    def detect_put_call_signals(self) -> int:
        self.ensure_tables()
        rows_sql = text(
            """
            SELECT zone_name, commodity, AVG(price_value) AS avg_price, MAX(price_value) AS max_price, MIN(price_value) AS min_price
            FROM agri_market.prices
            WHERE collected_at >= NOW() - INTERVAL '7 days'
            GROUP BY zone_name, commodity
            """
        )
        insert_sql = text(
            """
            INSERT INTO agri_market.put_call_signals (
                zone_name, commodity, signal_type, trigger_price, current_price, details
            ) VALUES (
                :zone_name, :commodity, :signal_type, :trigger_price, :current_price, :details
            )
            """
        )

        emitted = 0
        with self.engine.begin() as conn:
            rows = conn.execute(rows_sql).mappings().all()
            for row in rows:
                avg_price = float(row["avg_price"] or 0.0)
                max_price = float(row["max_price"] or 0.0)
                min_price = float(row["min_price"] or 0.0)
                zone_name = row["zone_name"]
                commodity = row["commodity"]

                if avg_price <= 0:
                    continue

                if min_price <= avg_price * 0.9:
                    conn.execute(
                        insert_sql,
                        {
                            "zone_name": zone_name,
                            "commodity": commodity,
                            "signal_type": "PUT_ACTIVABLE",
                            "trigger_price": avg_price * 0.9,
                            "current_price": min_price,
                            "details": "Prix sous seuil de protection, couverture put recommandee",
                        },
                    )
                    emitted += 1

                if max_price >= avg_price * 1.1:
                    conn.execute(
                        insert_sql,
                        {
                            "zone_name": zone_name,
                            "commodity": commodity,
                            "signal_type": "CALL_ACTIVABLE",
                            "trigger_price": avg_price * 1.1,
                            "current_price": max_price,
                            "details": "Prix au-dessus du seuil opportunite, couverture call possible",
                        },
                    )
                    emitted += 1

        return emitted

    def run(self) -> Dict[str, int]:
        prices = self.collect_prices()
        persisted = self.persist_prices(prices)
        signals = self.detect_put_call_signals()
        return {"prices": len(prices), "persisted": persisted, "signals": signals}
