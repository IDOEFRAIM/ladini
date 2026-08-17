"""
AgriConnect AI Production Server.

Utilisation recommandée en production :
    gunicorn -w 4 -k uvicorn.workers.UvicornWorker server:app --bind 0.0.0.0:8000

g
"""

import uvicorn

if __name__ == "__main__":
    # Utilisé uniquement pour le développement local
    # En production, utilisez Gunicorn comme indiqué dans le docstring ci-dessus
    uvicorn.run(
        "agriconnect.api.server:app",
        host="0.0.0.0",
        port=8000,
        reload=True,  # À mettre à False en production
        log_level="info",
    )
