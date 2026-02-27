import asyncio
import os
import asyncpg

async def main():
    d = os.getenv("DATABASE_URL")
    if not d:
        print("DATABASE_URL not set")
        return
    conn = await asyncpg.connect(d)
    rows = await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name='agent_actions' ORDER BY ordinal_position")
    for r in rows:
        print(r['column_name'])
    await conn.close()

if __name__ == '__main__':
    asyncio.run(main())
