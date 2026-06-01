import asyncio, traceback
from agriconnect.core import database
from agriconnect.core.settings import settings

out_path = 'backend/tmp/db_conn_output.txt'
try:
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('repr(DATABASE_URL): ' + repr(getattr(settings, 'DATABASE_URL', None)) + '\n')
        f.write('DB_SSL_MODE: ' + str(getattr(settings, 'DB_SSL_MODE', None)) + '\n')
        f.write('DB_CA_PATH: ' + str(getattr(settings, 'DB_CA_PATH', None)) + '\n')

    database.init_db()

    async def run():
        try:
            ok, reason = await database.check_connection_detailed()
            with open(out_path, 'a', encoding='utf-8') as f:
                f.write('check_connection_detailed -> ' + str((ok, reason)) + '\n')
        except Exception as e:
            with open(out_path, 'a', encoding='utf-8') as f:
                f.write('check_connection_detailed exception:\n')
                traceback.print_exc(file=f)
        try:
            ok = await database.check_connection()
            with open(out_path, 'a', encoding='utf-8') as f:
                f.write('check_connection -> ' + str(ok) + '\n')
        except Exception as e:
            with open(out_path, 'a', encoding='utf-8') as f:
                f.write('check_connection exception:\n')
                traceback.print_exc(file=f)

    asyncio.run(run())
except Exception:
    with open(out_path, 'w', encoding='utf-8') as f:
        traceback.print_exc(file=f)
print('written')
