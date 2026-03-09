"""ASGI loader that imports `agriconnect/api/agro_api.py` by file path
to avoid importing package-level side-effects in `agriconnect.api.__init__`.

This allows running the isolated `app` defined in that file without requiring
the rest of the `agriconnect.api` package to be importable (e.g., Celery).
"""
from pathlib import Path
import importlib.util
import sys

HERE = Path(__file__).resolve().parent
TARGET = HERE / "src" / "agriconnect" / "api" / "agro_api.py"

if not TARGET.exists():
    raise RuntimeError(f"agro_api.py not found at {TARGET}")

spec = importlib.util.spec_from_file_location("agro_api_module", str(TARGET))
module = importlib.util.module_from_spec(spec)
# Execute the module isolated (it will populate module.app)
spec.loader.exec_module(module)

# Expose the FastAPI app for ASGI servers
try:
    app = getattr(module, "app")
except Exception:
    raise RuntimeError("No `app` found in agro_api.py")
