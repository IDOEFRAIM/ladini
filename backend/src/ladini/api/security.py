"""Authentification des points d'entrée HTTP (webhooks + routes internes).

RÈGLE CENTRALE (audit sécurité 2026-09-10) : tout endpoint capable de faire
AGIR l'agent au nom d'un numéro de téléphone est une usurpation d'identité
complète s'il n'est pas authentifié — l'agent passe commande, confirme des
enchères, débloque du séquestre et modifie le catalogue en dérivant l'identité
de l'appelant du seul `phone_number` reçu. Les trois portes couvertes ici :

  - `verify_twilio_signature`         -> POST /api/webhook/twilio
  - `verify_whatsapp_cloud_signature` -> POST /api/webhook/whatsapp
  - `require_internal_token`          -> /api/market/* (appels serveur-à-serveur)

Toutes sont FAIL-CLOSED : une configuration absente refuse le trafic au lieu
de l'accepter en clair. Le seul assouplissement est un mode développement
EXPLICITE (`ENV=development` **et** secret non configuré), qui journalise
bruyamment à chaque requête.
"""

import hashlib
import hmac
import logging
import os

from fastapi import Header, HTTPException, Request
from twilio.request_validator import RequestValidator

from ladini.core.settings import settings

logger = logging.getLogger("Ladini.API.Security")


def is_explicit_dev_mode() -> bool:
    """Vrai uniquement en développement déclaré. Jamais déduit d'une absence
    de configuration seule — c'est exactement ce qui transforme un oubli de
    variable d'environnement en endpoint public en production."""
    return os.getenv("ENV", "").strip().lower() in ("development", "dev", "local")


# =====================================================================
# WhatsApp Cloud API (Meta) — HMAC-SHA256 sur le corps brut
# =====================================================================


def verify_whatsapp_cloud_signature(
    app_secret: str, raw_body: bytes, signature_header: str
) -> bool:
    """Vérifie la signature HMAC-SHA256 d'un webhook WhatsApp Cloud API (Meta).

    Le corps brut (AVANT tout parsing JSON) est signé avec le secret de l'app
    Meta ; l'en-tête ``X-Hub-Signature-256`` porte ``sha256=<hex>``. Sans
    cette vérification, n'importe qui peut poster un faux message sur
    l'endpoint webhook — c'est l'équivalent Meta de ``verify_twilio_signature``
    ci-dessous, mais HMAC sur le corps plutôt que sur l'URL+paramètres.
    """
    if not app_secret or not signature_header:
        return False
    prefix = "sha256="
    if not signature_header.startswith(prefix):
        return False
    expected = hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    received = signature_header[len(prefix) :]
    return hmac.compare_digest(expected, received)


# =====================================================================
# Twilio — signature sur URL + paramètres du formulaire
# =====================================================================


def _twilio_auth_token() -> str:
    # `settings` d'abord (source unique de configuration du projet), `os.getenv`
    # en repli pour les process lancés sans .env chargé.
    return (
        str(getattr(settings, "TWILIO_AUTH_TOKEN", "") or "").strip()
        or os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    )


def _candidate_signed_urls(request: Request) -> list[str]:
    """URLs candidates contre lesquelles valider la signature Twilio.

    Twilio signe l'URL EXACTE qu'il a appelée. Derrière un reverse proxy ou un
    load balancer, `request.url` peut porter un schéma/hôte interne
    (`http://10.0.0.4:8000/...`) différent de l'URL publique signée — d'où
    l'ordre suivant :

      1. `TWILIO_PUBLIC_BASE_URL` si configurée (source de vérité explicite) ;
      2. l'URL reconstruite depuis les en-têtes `X-Forwarded-Proto`/`Host`
         (ce que voit réellement le proxy) ;
      3. l'URL brute de la requête.

    Aucune valeur codée en dur : la version précédente retombait sur un
    domaine ngrok de développement inscrit dans le source, ce qui rendait la
    validation dépendante d'un hôte tiers arbitraire.
    """
    path = request.url.path
    query = f"?{request.url.query}" if request.url.query else ""
    suffix = f"{path}{query}"

    candidates: list[str] = []

    configured = (
        str(getattr(settings, "TWILIO_PUBLIC_BASE_URL", "") or "").strip()
        or os.getenv("TWILIO_PUBLIC_BASE_URL", "").strip()
    ).rstrip("/")
    if configured:
        candidates.append(f"{configured}{suffix}")

    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[
        0
    ].strip()
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",")[
        0
    ].strip() or (request.headers.get("host") or "").strip()
    if forwarded_host:
        scheme = forwarded_proto or request.url.scheme or "https"
        candidates.append(f"{scheme}://{forwarded_host}{suffix}")

    candidates.append(str(request.url))

    # Dédoublonnage en préservant l'ordre de préférence.
    seen: set[str] = set()
    return [u for u in candidates if not (u in seen or seen.add(u))]


async def verify_twilio_signature(request: Request) -> None:
    """Dépendance FastAPI : rejette toute requête non signée par Twilio.

    ⚠️ Sans cette dépendance, `POST /api/webhook/twilio` accepte n'importe
    quel `From`/`Body` : c'est une usurpation d'identité totale (passer et
    confirmer des commandes, désigner un gagnant d'enchère, faire télécharger
    une URL arbitraire via `MediaUrl0`) accessible à quiconque connaît l'URL
    du webhook. Elle EXISTAIT dans ce fichier mais n'était câblée sur aucune
    route — corrigé le 2026-09-10, voir `routes/twilio_webhook.py`.
    """
    auth_token = _twilio_auth_token()

    # Seul assouplissement : développement DÉCLARÉ et token réellement absent.
    if not auth_token:
        if is_explicit_dev_mode():
            logger.warning(
                "TWILIO_SIGNATURE_CHECK_SKIPPED | ENV=development et "
                "TWILIO_AUTH_TOKEN absent — endpoint NON authentifié."
            )
            return
        logger.error(
            "TWILIO_SIGNATURE_UNVERIFIABLE | TWILIO_AUTH_TOKEN non configuré — "
            "webhook refusé (fail-closed)."
        )
        raise HTTPException(status_code=503, detail="Webhook non configuré.")

    signature = request.headers.get("X-Twilio-Signature")
    if not signature:
        logger.warning("TWILIO_SIGNATURE_MISSING | path=%s", request.url.path)
        raise HTTPException(status_code=403, detail="Signature Twilio manquante.")

    # `request.form()` est mis en cache par Starlette : le handler pourra le
    # relire (et ses paramètres `Form(...)`) sans re-consommer le corps.
    form_data = await request.form()
    params = {key: str(value) for key, value in form_data.items()}

    validator = RequestValidator(auth_token)
    candidates = _candidate_signed_urls(request)
    for url in candidates:
        if validator.validate(url, params, signature):
            return

    # Ne JAMAIS journaliser les paramètres (corps du message, numéro de
    # téléphone, coordonnées GPS) ni la signature reçue : cet enregistrement
    # partait auparavant en ERROR avec `received_params` complet, déversant du
    # contenu utilisateur et des données personnelles dans les logs à chaque
    # tentative — y compris celles d'un attaquant qui choisit ce contenu.
    #
    # `candidates` (URLs, PAS des données utilisateur — schéma/hôte/chemin
    # seulement) journalisé TEMPORAIREMENT (2026-09-11) pour diagnostiquer un
    # incident réel de 403 intermittents malgré un `TWILIO_PUBLIC_BASE_URL`
    # déjà correctement configuré — sans ça, aucun moyen de voir LEQUEL des
    # 3 candidats a été tenté ni pourquoi aucun ne matche.
    logger.error(
        "TWILIO_SIGNATURE_INVALID | path=%s | client=%s | param_keys=%s | "
        "candidates=%s",
        request.url.path,
        getattr(request.client, "host", "?"),
        sorted(params.keys()),
        candidates,
    )
    raise HTTPException(status_code=403, detail="Signature Twilio invalide.")


# =====================================================================
# Routes internes (/api/market/*) — secret partagé
# =====================================================================


def require_internal_token(x_internal_token: str | None = Header(default=None)) -> None:
    """Authentifie un appel serveur-à-serveur sur les routes `/api/market/*`.

    Ces routes font tourner l'agent pour un `phone_number` ARBITRAIRE avec
    `force_role=True` : sans authentification, elles offrent à tout le monde
    exactement les pouvoirs de n'importe quel utilisateur de la plateforme
    (audit 2026-09-10 — elles étaient entièrement ouvertes). Elles ne sont
    jamais appelées par WhatsApp (qui passe par les webhooks signés) : ce sont
    des points d'entrée d'outillage/intégration.

    Fail-closed, même posture que `routes/admin.py::_check_admin_token` :
    secret non configuré -> 503, endpoint désactivé pour TOUT le monde.
    Comparaison en temps constant.
    """
    expected = str(getattr(settings, "INTERNAL_API_TOKEN", "") or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=(
                "INTERNAL_API_TOKEN non configuré — routes internes désactivées "
                "par défaut."
            ),
        )
    provided = (x_internal_token or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        logger.warning("INTERNAL_API_AUTH_DENIED")
        raise HTTPException(status_code=401, detail="Token interne invalide ou absent.")


__all__ = [
    "require_internal_token",
    "verify_twilio_signature",
    "verify_whatsapp_cloud_signature",
]
