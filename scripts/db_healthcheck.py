import json
from pathlib import Path
import sys

# Ensure backend/src on path
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "backend" / "src"
sys.path.insert(0, str(SRC))

try:
    from agriconnect.infrastructure.database import db
except Exception as exc:
    print(json.dumps({"ok": False, "error": f"import error: {exc}"}, ensure_ascii=False))
    raise

try:
    report = db.aggressive_db_healthcheck()
    print(json.dumps({"ok": True, "report": report}, ensure_ascii=False, indent=2))
except Exception as exc:
    import traceback
    traceback.print_exc()
    print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
    raise
