import os
import sys
import traceback
from sqlalchemy import create_engine, text


def main():
    url = os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL not set", file=sys.stderr)
        sys.exit(2)
    print("Testing DB URL:", url)
    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            res = conn.execute(text("select 1"))
            print("select 1 ->", res.scalar())

            
        print("DB connection OK")
    except Exception:
        print("DB connection failed:")
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
