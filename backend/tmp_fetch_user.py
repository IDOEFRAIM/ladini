import asyncio
import json
from sqlalchemy import text

from agriconnect.core.database import init_db, get_db
from agriconnect.protocols.mcp.servers.agri_db_server import AgriDBMCPServer

PRODUCER_ID = "fa987f63-fafa-4147-9676-52c9af0edc75"

init_db()

async def main():
    try:
        async with get_db() as session:
            r = await session.execute(text("SELECT user_id FROM marketplace.producers WHERE id = :id LIMIT 1"), {"id": PRODUCER_ID})
            row = r.fetchone()
            if not row:
                print(json.dumps({"status": "producer_not_found", "producer_id": PRODUCER_ID}, ensure_ascii=False))
                return
            user_id = row[0]
            print(json.dumps({"status": "found", "producer_id": PRODUCER_ID, "user_id": user_id}, ensure_ascii=False))

        server = AgriDBMCPServer()
        res = await server._get_user_profile({"user_id": user_id})
        print(json.dumps({"profile_result": res.__dict__}, ensure_ascii=False, default=str))
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))

if __name__ == '__main__':
    asyncio.run(main())
