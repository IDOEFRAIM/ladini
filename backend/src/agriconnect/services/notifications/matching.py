from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from agriconnect.core.db import get_engine, resolve_database_url

logger = logging.getLogger(__name__)


@dataclass
class MatchCandidate:
    product_id: str
    buyer_id: str
    score: float
    zone_id: Optional[str]
    category_label: Optional[str]
    quantity_for_sale: Optional[float]
    freshness_hours: float


class NotificationMatcher:
    """Zone-first matching engine (producer <-> consumer) for daily proactive pushes."""

    def __init__(self, db_url: Optional[str] = None):
        self.db_url = (db_url or "").strip() or resolve_database_url(required=True)
        self.engine = get_engine(self.db_url)

    def ensure_tables(self) -> None:
        ddl = [
            "CREATE SCHEMA IF NOT EXISTS intelligence",
            """
            CREATE TABLE IF NOT EXISTS intelligence.matches (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                product_id UUID NOT NULL,
                buyer_id UUID,
                score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'SUGGESTED',
                meta JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_matches_status ON intelligence.matches (status)",
            "CREATE INDEX IF NOT EXISTS idx_matches_buyer_status ON intelligence.matches (buyer_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_matches_product_created ON intelligence.matches (product_id, created_at DESC)",
        ]
        with self.engine.begin() as conn:
            for statement in ddl:
                conn.execute(text(statement))

    def _fetch_candidates_for_buyer(self, buyer_id: str, limit: int = 20) -> List[MatchCandidate]:
        query = text(
            """
            WITH buyer_profile AS (
                SELECT
                    o.buyer_id,
                    o.zone_id,
                    COALESCE(AVG(oi.quantity), 0.0) AS avg_qty,
                    p.category_label
                FROM marketplace.orders o
                JOIN marketplace.order_items oi ON oi.order_id = o.id
                JOIN marketplace.products p ON p.id = oi.product_id
                WHERE o.buyer_id = :buyer_id::uuid
                  AND o.created_at >= NOW() - INTERVAL '120 days'
                GROUP BY o.buyer_id, o.zone_id, p.category_label
            ),
            active_products AS (
                SELECT
                    p.id AS product_id,
                    p.producer_id,
                    prod.user_id AS producer_user_id,
                    p.zone_id,
                    p.category_label,
                    p.quantity_for_sale,
                    p.created_at,
                    EXTRACT(EPOCH FROM (NOW() - p.created_at)) / 3600.0 AS freshness_hours
                FROM marketplace.products p
                JOIN marketplace.producers prod ON prod.id = p.producer_id
                WHERE COALESCE(p.quantity_for_sale, 0) > 0
                  AND p.created_at >= NOW() - INTERVAL '14 days'
            ),
            scored AS (
                SELECT
                    ap.product_id::text AS product_id,
                    bp.buyer_id::text AS buyer_id,
                    ap.zone_id::text AS zone_id,
                    ap.category_label,
                    ap.quantity_for_sale,
                    ap.freshness_hours,
                    (
                        0.50 * CASE WHEN ap.zone_id = bp.zone_id THEN 1.0 ELSE 0.0 END +
                        0.30 * CASE
                            WHEN bp.avg_qty <= 0 THEN 0.6
                            WHEN ap.quantity_for_sale >= bp.avg_qty THEN 1.0
                            ELSE GREATEST(0.1, ap.quantity_for_sale / NULLIF(bp.avg_qty, 0))
                        END +
                        0.20 * GREATEST(0.0, LEAST(1.0, 1.0 - (ap.freshness_hours / 120.0)))
                    ) AS affinity_score
                FROM buyer_profile bp
                JOIN active_products ap
                    ON ap.zone_id = bp.zone_id
                   AND ap.category_label = bp.category_label
                   AND ap.producer_user_id IS DISTINCT FROM bp.buyer_id
            )
            SELECT
                product_id,
                buyer_id,
                ROUND(affinity_score::numeric, 4)::double precision AS score,
                zone_id,
                category_label,
                quantity_for_sale,
                freshness_hours
            FROM scored
            ORDER BY score DESC, freshness_hours ASC
            LIMIT :limit
            """
        )
        with self.engine.connect() as conn:
            rows = conn.execute(query, {"buyer_id": buyer_id, "limit": int(limit)}).mappings().all()
        out: List[MatchCandidate] = []
        for row in rows:
            out.append(
                MatchCandidate(
                    product_id=str(row["product_id"]),
                    buyer_id=str(row["buyer_id"]),
                    score=float(row["score"]),
                    zone_id=row.get("zone_id"),
                    category_label=row.get("category_label"),
                    quantity_for_sale=(float(row["quantity_for_sale"]) if row.get("quantity_for_sale") is not None else None),
                    freshness_hours=float(row.get("freshness_hours") or 0.0),
                )
            )
        return out

    def _persist_candidates(self, candidates: List[MatchCandidate]) -> int:
        if not candidates:
            return 0

        insert_sql = text(
            """
            INSERT INTO intelligence.matches (
                product_id,
                buyer_id,
                score,
                status,
                meta,
                created_at,
                updated_at
            )
            SELECT
                :product_id::uuid,
                :buyer_id::uuid,
                :score,
                'SUGGESTED',
                jsonb_build_object(
                    'strategy', 'zone_first_v1',
                    'zone_id', :zone_id,
                    'category_label', :category_label,
                    'quantity_for_sale', :quantity_for_sale,
                    'freshness_hours', :freshness_hours
                ),
                NOW(),
                NOW()
            WHERE NOT EXISTS (
                SELECT 1
                FROM intelligence.matches m
                WHERE m.product_id = :product_id::uuid
                  AND m.buyer_id = :buyer_id::uuid
                  AND m.status IN ('SUGGESTED', 'NOTIFIED')
            )
            """
        )
        inserted = 0
        with self.engine.begin() as conn:
            for candidate in candidates:
                result = conn.execute(
                    insert_sql,
                    {
                        "product_id": candidate.product_id,
                        "buyer_id": candidate.buyer_id,
                        "score": candidate.score,
                        "zone_id": candidate.zone_id,
                        "category_label": candidate.category_label,
                        "quantity_for_sale": candidate.quantity_for_sale,
                        "freshness_hours": candidate.freshness_hours,
                    },
                )
                if int(result.rowcount or 0) > 0:
                    inserted += 1
        return inserted

    def find_matches_for_buyer(self, buyer_id: str, limit: int = 20, persist: bool = True) -> Dict[str, Any]:
        """Finds top zone-first product matches for one buyer, optionally persists them."""
        self.ensure_tables()
        candidates = self._fetch_candidates_for_buyer(buyer_id=buyer_id, limit=limit)
        persisted = self._persist_candidates(candidates) if persist else 0
        return {
            "buyer_id": buyer_id,
            "candidates": len(candidates),
            "persisted": persisted,
            "matches": [
                {
                    "product_id": c.product_id,
                    "score": c.score,
                    "zone_id": c.zone_id,
                    "category_label": c.category_label,
                }
                for c in candidates
            ],
        }

    def run_matching(self, per_buyer_limit: int = 20) -> Dict[str, int]:
        """Daily batch: compute zone-first matches for active buyers."""
        self.ensure_tables()
        buyers_sql = text(
            """
            SELECT DISTINCT o.buyer_id::text AS buyer_id
            FROM marketplace.orders o
            WHERE o.buyer_id IS NOT NULL
              AND o.zone_id IS NOT NULL
              AND o.created_at >= NOW() - INTERVAL '120 days'
            """
        )
        with self.engine.connect() as conn:
            buyers = [str(r["buyer_id"]) for r in conn.execute(buyers_sql).mappings().all()]

        total_candidates = 0
        total_persisted = 0
        for buyer_id in buyers:
            candidates = self._fetch_candidates_for_buyer(buyer_id=buyer_id, limit=per_buyer_limit)
            total_candidates += len(candidates)
            total_persisted += self._persist_candidates(candidates)

        return {
            "buyers": len(buyers),
            "candidates": total_candidates,
            "messages": total_persisted,
            "persisted": total_persisted,
        }

    def dispatch_pending(self, limit: int = 250) -> int:
        """Marks pending match suggestions as notified (handoff placeholder)."""
        update_sql = text(
            """
            WITH picked AS (
                SELECT id
                FROM intelligence.matches
                WHERE status = 'SUGGESTED'
                ORDER BY created_at ASC
                LIMIT :limit
            )
            UPDATE intelligence.matches m
            SET status = 'NOTIFIED',
                updated_at = NOW(),
                meta = COALESCE(m.meta, '{}'::jsonb) || jsonb_build_object('notified_at', NOW())
            FROM picked
            WHERE m.id = picked.id
            """
        )
        with self.engine.begin() as conn:
            result = conn.execute(update_sql, {"limit": int(limit)})
        return int(result.rowcount or 0)

    def clean_expired_matches(self, max_age_days: int = 7, hard_delete: bool = True) -> Dict[str, int]:
        """Expires matches for sold/outdated products and optionally deletes them."""
        expire_sql = text(
            """
            UPDATE intelligence.matches m
            SET status = 'EXPIRED',
                updated_at = NOW(),
                meta = COALESCE(m.meta, '{}'::jsonb) || jsonb_build_object('expired_at', NOW(), 'reason', 'sold_or_stale')
            FROM marketplace.products p
            WHERE m.product_id = p.id
              AND m.status IN ('SUGGESTED', 'NOTIFIED')
              AND (
                  COALESCE(p.quantity_for_sale, 0) <= 0
                  OR m.created_at < NOW() - make_interval(days => :max_age_days)
              )
            """
        )

        delete_sql = text("DELETE FROM intelligence.matches WHERE status = 'EXPIRED'")

        with self.engine.begin() as conn:
            expired_result = conn.execute(expire_sql, {"max_age_days": int(max_age_days)})
            deleted_count = 0
            if hard_delete:
                deleted_result = conn.execute(delete_sql)
                deleted_count = int(deleted_result.rowcount or 0)

        return {
            "expired": int(expired_result.rowcount or 0),
            "deleted": deleted_count,
        }
