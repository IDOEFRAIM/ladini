from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime
import psycopg2
from agriconnect.core.settings import settings

def fix_schema():
    print("Connecting to DB via settings...")
    conn = psycopg2.connect(str(settings.DATABASE_URL))
    conn.autocommit = True
    cur = conn.cursor()
    
    # 1. Drop legacy column
    print("Dropping doc_ref...")
    try:
        cur.execute("ALTER TABLE agri_vector.document_chunks DROP COLUMN IF EXISTS doc_ref CASCADE;")
        print("Dropped doc_ref.")
    except Exception as e:
        print(f"Error dropping doc_ref: {e}")

    # 2. Add constraint
    print("Adding unique constraint on content_hash...")
    try:
        cur.execute("ALTER TABLE agri_vector.document_chunks ADD CONSTRAINT document_chunks_content_hash_key UNIQUE (content_hash);")
        print("Constraint added.")
    except Exception as e:
        print(f"Constraint error: {e}")

    # 3. Check dimensions
    cur.execute("SELECT atttypmod FROM pg_attribute WHERE attrelid = 'agri_vector.document_chunks'::regclass AND attname = 'embedding';")
    print(f"Dimensions: {cur.fetchone()}")

    cur.close()
    conn.close()

with DAG(
    dag_id="Fix_Schema_DAG",
    start_date=datetime(2024, 1, 1),
    schedule_interval=None,
    catchup=False
) as dag:
    fix_task = PythonOperator(
        task_id="fix_schema_task",
        python_callable=fix_schema
    )
