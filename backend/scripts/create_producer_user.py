#!/usr/bin/env python3
"""
Create a test user with producer profile in the configured DATABASE_URL.
Run this inside a container that has access to the DB (or locally with backend/.env present).
Example (inside scheduler container):
  docker exec backend-airflow-scheduler-1 python /opt/airflow/agriconnect_root/backend/scripts/create_producer_user.py
"""
import os
import uuid

from sqlalchemy import text
from agriconnect.core.db import get_engine

# Load DATABASE_URL from backend/.env if present
ENV_PATH = os.path.join(os.path.dirname(__file__), '..', '.env')
if os.path.exists(ENV_PATH):
    # simple .env loader (key=val)
    with open(ENV_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip().strip('"')
                os.environ.setdefault(k, v)

DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    print('DATABASE_URL not set in env or backend/.env. Aborting.')
    raise SystemExit(1)

engine = get_engine(DATABASE_URL)

user_id = str(uuid.uuid4())
producer_id = str(uuid.uuid4())
phone = os.getenv('TEST_PRODUCER_PHONE', '+223700000000')
name = os.getenv('TEST_PRODUCER_NAME', 'Producer Test')
business = os.getenv('TEST_PRODUCER_BUSINESS', 'ProducerCo')

print('Creating user', user_id)
print('Creating producer', producer_id)

insert_user_sql = text(
    "INSERT INTO users (id, name, phone, role, \"createdAt\", \"updatedAt\")"
    " VALUES (:id, :name, :phone, :role, now(), now())"
    " ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, phone=EXCLUDED.phone, role=EXCLUDED.role;"
)

insert_producer_sql = text(
    "INSERT INTO producers (id, \"userId\", \"businessName\", status, \"createdAt\", \"updatedAt\")"
    " VALUES (:pid, :uid, :business, :status, now(), now())"
    " ON CONFLICT (id) DO UPDATE SET \"businessName\"=EXCLUDED.\"businessName\", status=EXCLUDED.status;"
)

with engine.begin() as conn:
    print('Inserting/ensuring user row...')
    try:
        conn.execute(insert_user_sql, {"id": user_id, "name": name, "phone": phone, "role": "PRODUCER"})
    except Exception as e:
        print('Failed to insert user:', e)
        raise

    # Inspect producers table columns to choose correct column identifiers
    # Find which schema contains the producers table and list its columns.
    schemas = [r[0] for r in conn.execute(text(
        "SELECT DISTINCT table_schema FROM information_schema.tables WHERE table_name = 'producers';"
    )).fetchall()]
    print('Producers table found in schemas:', schemas)

    col_names = []
    chosen_schema = None
    for schema in schemas:
        cs = [r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = :schema AND table_name = 'producers';"
        ), {"schema": schema}).fetchall()]
        print(f'Columns in {schema}.producers: {cs}')
        if cs:
            # prefer schema that contains business-like column
            lower = {c.lower(): c for c in cs}
            if 'business_name' in lower or 'businessname' in lower or 'business' in lower:
                chosen_schema = schema
                col_names = cs
                break
            # fallback to first schema with columns
            if not chosen_schema:
                chosen_schema = schema
                col_names = cs

    if not col_names:
        raise RuntimeError('No producers table columns found in any schema.')

    print('Detected producers columns (chosen schema):', col_names)

    def find_col(candidates):
        lower_map = {cn.lower(): cn for cn in col_names}
        for c in candidates:
            if c in col_names:
                return c
            if c.lower() in lower_map:
                return lower_map[c.lower()]
        return None

    user_col_found = find_col(['userId', 'user_id', 'userid'])
    business_col_found = find_col(['businessName', 'business_name', 'businessname', 'business'])
    created_col_found = find_col(['createdAt', 'created_at', 'createdat'])
    updated_col_found = find_col(['updatedAt', 'updated_at', 'updatedat'])

    if not user_col_found:
        raise RuntimeError(f'Could not determine user id column in producers table. Columns: {col_names}')

    user_col = user_col_found
    business_col = business_col_found or 'business'
    created_col = created_col_found or 'created_at'
    updated_col = updated_col_found or 'updated_at'

    # Build insert SQL dynamically with detected column names
    def col_identifier(name):
        # name is actual column name from DB (preserves case)
        if any(c.isupper() for c in name):
            return f'"{name}"'
        return name

    user_col_sql = col_identifier(user_col)
    business_col_sql = col_identifier(business_col)

    insert_cols = f"id, {user_col_sql}, {business_col_sql}, status, {col_identifier(created_col)}, {col_identifier(updated_col)}"
    update_set = f"{business_col_sql}=EXCLUDED.{business_col_sql}, status=EXCLUDED.status"

    qualified_table = f"{chosen_schema}.producers" if chosen_schema else 'producers'
    insert_producer_dynamic_sql = text(
        f"INSERT INTO {qualified_table} ({insert_cols}) VALUES (:pid, :uid, :business, :status, now(), now()) "
        f"ON CONFLICT (id) DO UPDATE SET {update_set};"
    )

    try:
        print('Inserting producer using columns:', insert_cols)
        conn.execute(insert_producer_dynamic_sql, {"pid": producer_id, "uid": user_id, "business": business, "status": "ACTIVE"})
    except Exception as e:
        print('Producer insert failed with dynamic SQL:', e)
        raise

print('Done. User and producer created/updated:')
print('  user_id=', user_id)
print('  producer_id=', producer_id)
print('\nVerify via psql or your app DB queries:')
print(f"  SELECT id,name,phone,role FROM users WHERE id='{user_id}';")
print(f"  SELECT id,\"userId\",\"businessName\" FROM producers WHERE \"userId\"='{user_id}';")
