"""Launch script for the Formation API (development).

Usage (from repo root):
    set PYTHONPATH=backend/src
    python backend/serve_agro.py

This starts a uvicorn server exposing the FastAPI app at
`agriconnect.api.agro_api:app` on port 8000.
"""
import uvicorn


if __name__ == "__main__":
    uvicorn.run("agriconnect.api.agro_api:app", host="0.0.0.0", port=8000, log_level="info")
