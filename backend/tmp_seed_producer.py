import asyncio
from sqlalchemy import text
from agriconnect.core.database import init_db, get_db

PRODUCER = {
  "id": "3cadb350-59e5-4ad8-ae95-5ff7cc1350bd",
  "userId": "fa987f63-fafa-4147-9676-52c9af0edc75",
  "businessName": "prod-gaoua",
  "status": "ACTIVE",
  "user": {
    "id": "fa987f63-fafa-4147-9676-52c9af0edc75",
    "name": "prod-gaoua",
    "email": "prod-gaoua@gmail.com"
  }
}

init_db()

async def main():
    async with get_db() as s:
        # Insert user if not exists
        await s.execute(text(
            "INSERT INTO auth.users (id, name, email, phone, role) VALUES (:id, :name, :email, :phone, :role) ON CONFLICT (id) DO NOTHING"
        ), {"id": PRODUCER['user']['id'], "name": PRODUCER['user']['name'], "email": PRODUCER['user']['email'], "phone": None, "role": 'PRODUCER'})

        # Insert producer if not exists
        await s.execute(text(
            "INSERT INTO marketplace.producers (id, user_id, business_name, status) VALUES (:id, :user_id, :business_name, :status) ON CONFLICT (id) DO NOTHING"
        ), {"id": PRODUCER['id'], "user_id": PRODUCER['userId'], "business_name": PRODUCER['businessName'], "status": PRODUCER['status']})

        await s.commit()

    print('Seed completed')

if __name__ == '__main__':
    asyncio.run(main())
