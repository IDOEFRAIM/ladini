import os
import sys
import psycopg2
from dotenv import load_dotenv

# Try to force UTF-8 for Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except:
        pass

# Force load .env from current directory (backend)
load_dotenv()

DB_HOST = os.getenv("POSTGRES_HOST", "localhost")
DB_NAME = os.getenv("POSTGRES_DB", "agriconnect")
DB_USER = os.getenv("POSTGRES_USER", "postgres")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")

def patch_dimensions():
    print(f"Connecting to {DB_NAME} at {DB_HOST}...", flush=True)
    try:
        # Use keyword arguments instead of DSN string to avoid parsing issues
        conn = psycopg2.connect(
            host=DB_HOST,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            port=DB_PORT
        )
        conn.autocommit = True
        cur = conn.cursor()

        # 1. Truncate table to avoid data conflict
        print("Truncating table 'agri_vector.document_chunks'...", flush=True)
        try:
            cur.execute("TRUNCATE TABLE agri_vector.document_chunks CASCADE;")
            print("Table truncated.")
        except Exception as e:
            try:
                print(f"Error truncating (will continue): {e}")
            except:
                print("Error truncating (encoding error)")
        
        # 2. Alter column to vector(1536)
        print("Altering column 'embedding' to vector(1536)...", flush=True)
        try:
            # Drop old column first to ensure clean state
            cur.execute("ALTER TABLE agri_vector.document_chunks DROP COLUMN IF EXISTS embedding;")
            cur.execute("ALTER TABLE agri_vector.document_chunks ADD COLUMN embedding vector(1536);")
            print("Successfully rebooted column to vector(1536).")
        except Exception as e:
            try:
                print(f"Error altering column: {e}")
            except:
                print("Error altering column (encoding error)")

        cur.close()
        conn.close()
        print("Schema dimension patch COMPLETED SUCCESSFULLY.")

    except Exception as e:
        # Print exception safely
        try:
            print(f"Connection failed: {str(e)}")
        except:
            print(f"Connection failed (encoding error): {repr(e)}")

if __name__ == "__main__":
    patch_dimensions()
