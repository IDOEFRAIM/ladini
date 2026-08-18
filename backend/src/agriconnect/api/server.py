"""
AgriConnect AI Production Server.

Utilisation recommandée en production (voir infra/docker/Dockerfile.api,
source de vérité pour cette commande) :
    gunicorn agriconnect.api.main:app -k uvicorn.workers.UvicornWorker -w 4 --bind 0.0.0.0:8000

L'instance FastAPI `app` vit dans `agriconnect.api.main` (routers, middleware,
lifespan) — CE module (`server.py`) n'est qu'un lanceur de dev local, il n'en
définit pas de copie.
"""

import uvicorn

if __name__ == "__main__":
    # Utilisé uniquement pour le développement local
    # En production, utilisez Gunicorn comme indiqué dans le docstring ci-dessus
    uvicorn.run(
        "agriconnect.api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,  # À mettre à False en production
        log_level="info",
    )
