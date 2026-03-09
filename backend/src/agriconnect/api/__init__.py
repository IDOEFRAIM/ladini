"""API Module — Routes et Schémas HTTP AgriConnect.

Importing `routes` may require optional dependencies (Celery, workers).
To make the package import-safe for lightweight testing (local FastAPI
instances), we attempt to import `router` but fall back to `None` on
errors.
"""
try:
	from .routes import router
except Exception:
	router = None

__all__ = ["router"]
