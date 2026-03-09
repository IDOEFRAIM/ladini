from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

try:
    import agriconnect.core.database as db
    print('OK import agriconnect.core.database')
    print('has init_db=', hasattr(db, 'init_db'))
    print('has close_db=', hasattr(db, 'close_db'))
    print('has _AsyncSessionLocal=', getattr(db, '_AsyncSessionLocal', None) is not None)
except Exception:
    traceback.print_exc()
