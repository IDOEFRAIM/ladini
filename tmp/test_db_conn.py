import os
import sys


def main():
    host = os.getenv("DB_HOST", "127.0.0.1")
    port = int(os.getenv("DB_PORT", "5433"))
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASS")
    dbname = os.getenv("DB_NAME")

    if not all([user, password, dbname]):
        print("Warning: DB_USER, DB_PASS or DB_NAME not set. Set them in the environment and re-run.")

    # Try asyncpg first (with and without SSL)
    try:
        import asyncio
        import asyncpg
        import ssl

        async def check_asyncpg(sslctx=None):
            conn = await asyncpg.connect(user=user, password=password, database=dbname, host=host, port=port, ssl=sslctx)
            ver = await conn.fetchval('select version()')
            await conn.close()
            return ver

        print("Trying asyncpg without SSL...")
        try:
            ver = asyncio.run(check_asyncpg(None))
            print("OK asyncpg (no ssl):", ver)
            return 0
        except Exception as e:
            print("asyncpg (no ssl) error:", repr(e))

        print("Trying asyncpg with SSL...")
        try:
            sslctx = ssl.create_default_context()
            ver = asyncio.run(check_asyncpg(sslctx))
            print("OK asyncpg (ssl):", ver)
            return 0
        except Exception as e:
            print("asyncpg (ssl) error:", repr(e))
    except Exception as e:
        print("asyncpg not available or import failed:", repr(e))

    # Fallback to psycopg2
    try:
        import psycopg2
        print("Trying psycopg2 without SSL...")
        try:
            conn = psycopg2.connect(host=host, port=port, user=user, password=password, dbname=dbname, connect_timeout=5)
            cur = conn.cursor()
            cur.execute('select version()')
            print("OK psycopg2 (no ssl):", cur.fetchone())
            cur.close()
            conn.close()
            return 0
        except Exception as e:
            print("psycopg2 (no ssl) error:", repr(e))

        print("Trying psycopg2 with SSL (sslmode=require)...")
        try:
            conn = psycopg2.connect(host=host, port=port, user=user, password=password, dbname=dbname, connect_timeout=5, sslmode='require')
            cur = conn.cursor()
            cur.execute('select version()')
            print("OK psycopg2 (ssl):", cur.fetchone())
            cur.close()
            conn.close()
            return 0
        except Exception as e:
            print("psycopg2 (ssl) error:", repr(e))
    except Exception as e:
        print("psycopg2 not available or import failed:", repr(e))

    print("All connection attempts failed.")
    return 2


if __name__ == '__main__':
    sys.exit(main())
