"""Fabriques SQL brutes pour construire des graphes métier valides sur une base réelle."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from psycopg2.extras import Json


def insert(cur, table: str, **cols):
    """INSERT ... RETURNING (1re colonne = id). Les dict deviennent du JSONB."""
    names = list(cols)
    vals = [Json(v) if isinstance(v, dict) else v for v in cols.values()]
    cur.execute(
        f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))}) RETURNING *",
        vals,
    )
    return cur.fetchone()[0]


def uniq(prefix: str = "x") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class Graph:
    """Un mini-marché valide : zone, catégorie, producteur, acheteur, produit — plus enchères/offres à la demande."""

    def __init__(self, cur):
        self.cur = cur
        region = insert(cur, "governance.climatic_regions", name=uniq("region"))
        self.zone = insert(cur, "governance.zones", name=uniq("zone"), code=uniq("Z"), climatic_region_id=region)
        cat = insert(cur, "governance.categories", name=uniq("cat"))
        self.sub_category = insert(cur, "governance.sub_categories", category_id=cat, name=uniq("sub"))
        self.producer_user = insert(cur, "auth.users", phone=uniq("+226"), zone_id=self.zone)
        self.producer = insert(cur, "marketplace.producers", user_id=self.producer_user, zone_id=self.zone)
        self.buyer_user = insert(cur, "auth.users", phone=uniq("+226"))
        self.buyer = insert(cur, "marketplace.buyer_profiles", user_id=self.buyer_user)
        self.product = insert(
            cur, "marketplace.products", category_label="Céréales", price=100,
            producer_id=self.producer, sub_category_id=self.sub_category,
        )

    def auction(self, **over):
        now = datetime.utcnow()
        cols = dict(
            buyer_id=self.buyer, sub_category_id=self.sub_category, quantity=100, max_price_per_unit=500,
            delivery_location="Ouagadougou", delivery_deadline=now + timedelta(days=10), deadline=now + timedelta(days=3),
        )
        cols.update(over)
        return insert(self.cur, "marketplace.auctions", **cols)

    def bid(self, auction, producer=None, **over):
        return insert(self.cur, "marketplace.bids", auction_id=auction, producer_id=producer or self.producer,
                      offered_price=450, **over)

    def order(self, **over):
        cols = dict(total_amount=1000, buyer_id=self.buyer)
        cols.update(over)
        return insert(self.cur, "marketplace.orders", **cols)

    def extra_producer(self):
        u = insert(self.cur, "auth.users", phone=uniq("+226"))
        return insert(self.cur, "marketplace.producers", user_id=u)
