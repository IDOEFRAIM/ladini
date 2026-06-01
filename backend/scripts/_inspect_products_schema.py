from __future__ import annotations

import sys

from agriconnect.core.settings import settings
from sqlalchemy import create_engine, text


def main() -> None:
    table_name = sys.argv[1] if len(sys.argv) > 1 else "products"

    url = str(settings.DATABASE_URL).split("?", 1)[0]
    if not url:
        raise SystemExit("settings.DATABASE_URL is empty")

    engine = create_engine(url)
    query = text(
        """
        SELECT column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :table_name
        ORDER BY ordinal_position
        """
    )

    print(f"{table_name} columns:")
    with engine.connect() as conn:
        rows = conn.execute(query, {"table_name": table_name}).fetchall()
    for column_name, data_type, udt_name in rows:
        print(f" - {column_name}: {data_type} ({udt_name})")


if __name__ == "__main__":
    main()
