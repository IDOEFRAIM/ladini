from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime
from sqlalchemy import text
from agriconnect.core.db import get_engine, resolve_database_url
from agriconnect.core.settings import settings

def fix_schema():
    print("Connecting to DB via settings...")
    db_url = resolve_database_url(required=True)
    engine = get_engine(db_url)

    with engine.begin() as conn:
        # 1. Drop legacy column
        print("Dropping doc_ref...")
        try:
            conn.execute(text("ALTER TABLE agri_vector.document_chunks DROP COLUMN IF EXISTS doc_ref CASCADE;"))
            print("Dropped doc_ref.")
        except Exception as e:
            print(f"Error dropping doc_ref: {e}")

        # 2. Add constraint
        print("Adding unique constraint on content_hash...")
        try:
            conn.execute(text("ALTER TABLE agri_vector.document_chunks ADD CONSTRAINT document_chunks_content_hash_key UNIQUE (content_hash);"))
            print("Constraint added.")
        except Exception as e:
            print(f"Constraint error: {e}")

        # 3. Check dimensions
        row = conn.execute(text("SELECT atttypmod FROM pg_attribute WHERE attrelid = 'agri_vector.document_chunks'::regclass AND attname = 'embedding';")).first()
        print(f"Dimensions: {row}")

with DAG(
    dag_id="Fix_Schema_DAG",
    start_date=datetime(2024, 1, 1),
    schedule=None,
    catchup=False
) as dag:
    fix_task = PythonOperator(
        task_id="fix_schema_task",
        python_callable=fix_schema
    )
