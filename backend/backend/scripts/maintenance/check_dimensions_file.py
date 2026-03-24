import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
DB_NAME = os.getenv("POSTGRES_DB", "agriconnect")
DB_USER = os.getenv("POSTGRES_USER", "postgres")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")

DSN = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

with open("schema_check_result.txt", "w", encoding="utf-8") as f:
    try:
        conn = psycopg2.connect(DSN)
        cur = conn.cursor()
        cur.execute("""
            SELECT column_name, data_type, udt_name, character_maximum_length, numeric_precision 
            FROM information_schema.columns 
            WHERE table_schema = 'agri_vector' 
            AND table_name = 'document_chunks' 
            AND column_name = 'embedding';
        """)
        row = cur.fetchone()
        if row:
            f.write(f"FOUND: {row}\n") # udt_name should be 'vector'
            
            # Check dimensions specifically if possible
            cur.execute("""
                SELECT atttypmod 
                FROM pg_attribute 
                WHERE attrelid = 'agri_vector.document_chunks'::regclass 
                AND attname = 'embedding';
            """)
            mod = cur.fetchone()
            if mod:
                f.write(f"MODIFIER: {mod[0]}\n")
        else:
            f.write("NOT FOUND\n")
        
        conn.close()
    except Exception as e:
        f.write(f"ERROR: {e}\n")
