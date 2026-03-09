import sys
import asyncio
sys.path.insert(0, r'c:\Users\LENOVO T14s\Documents\projet\AgriConnect\backend\src')
from agriconnect.core import database as db

try:
    db.init_db()
    print('init_db() returned')
    ok = asyncio.run(db.check_connection())
    print('check_connection ->', ok)
except Exception as e:
    print('EXC:', type(e).__name__, e)
    raise
