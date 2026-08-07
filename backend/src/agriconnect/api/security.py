import hashlib
import hmac
import logging
import os
from typing import Dict
from fastapi import HTTPException, Request
from twilio.request_validator import RequestValidator

logger = logging.getLogger("AgriConnect.API.TwilioSecurity")


def verify_whatsapp_cloud_signature(app_secret: str, raw_body: bytes, signature_header: str) -> bool:
    """Vérifie la signature HMAC-SHA256 d'un webhook WhatsApp Cloud API (Meta).

    Le corps brut (AVANT tout parsing JSON) est signé avec le secret de l'app
    Meta ; l'en-tête ``X-Hub-Signature-256`` porte ``sha256=<hex>``. Sans
    cette vérification, n'importe qui peut poster un faux message sur
    l'endpoint webhook — c'est l'équivalent Meta de ``verify_twilio_signature``
    ci-dessus, mais HMAC sur le corps plutôt que sur l'URL+paramètres.
    """
    if not app_secret or not signature_header:
        return False
    prefix = "sha256="
    if not signature_header.startswith(prefix):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    received = signature_header[len(prefix):]
    return hmac.compare_digest(expected, received)

async def verify_twilio_signature(request: Request) -> None:
    """
    Dépendance FastAPI ultra-robuste pour valider les signatures Twilio.
    """
    # 1. Bypass uniquement si en dev ET sans token configuré
    if os.getenv("ENV") == "development" and not os.getenv("TWILIO_AUTH_TOKEN"):
        logger.warning("Twilio validation skipped (DEV mode)")
        return

    # 2. Récupération de la signature
    signature = request.headers.get("X-Twilio-Signature")
    if not signature:
        raise HTTPException(status_code=403, detail="Signature Twilio manquante.")

    # 3. Extraction des paramètres (POST form data)
    # L'ordre ici est crucial : on récupère le corps avant de valider.
    form_data = await request.form()
    params = {key: value for key, value in form_data.items()}

    # 4. Reconstruction forcée de l'URL
    # On utilise la variable d'environnement pour garantir l'identité de l'URL signée
    base_url = os.getenv("TWILIO_PUBLIC_BASE_URL", "").rstrip("/")
    if not base_url:
        # Fallback si pas de variable, mais c'est le point de risque principal
        base_url = "https://shortness-expensive-fidgety.ngrok-free.dev"
    
    url = f"{base_url}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"

    # 5. Validation
    validator = RequestValidator(os.getenv("TWILIO_AUTH_TOKEN", ""))
    
    if not validator.validate(url, params, signature):
        logger.error(
            "Twilio signature validation FAILED.",
            extra={
                "received_url": url,
                "received_params": params,
                "received_signature": signature
            }
        )
        raise HTTPException(status_code=403, detail="Signature Twilio invalide.")

    logger.info(f"Twilio signature validated for URL: {url}")