"""Invariant anti-dérive : AUCUNE route HTTP capable de faire agir l'agent
ne doit être joignable sans authentification.

Contexte (audit sécurité 2026-09-10) — trois portes étaient ouvertes en même
temps, chacune suffisante à elle seule pour une usurpation d'identité complète,
parce que TOUT ce qui est en aval dérive l'identité de l'utilisateur d'un
simple numéro de téléphone reçu sur le réseau :

  1. `POST /api/webhook/twilio` n'avait aucune vérification de signature.
     `verify_twilio_signature` existait bien dans `api/security.py` — et
     `api/README.md` affirmait même qu'elle « intercepte la requête » — mais
     elle n'était câblée sur AUCUNE route. Un `From=whatsapp:+…` arbitraire
     suffisait donc à passer/confirmer des commandes, désigner le gagnant
     d'une enchère ou vider un stock au nom de n'importe qui.
  2. `POST /api/market/producer` et `/buyer` lançaient l'agent pour un
     `phone_number` arbitraire avec `force_role=True`, sans aucune auth ;
     `GET /api/market/status/{task_id}` renvoyait le résultat de n'importe
     quelle tâche (donc la réponse conversationnelle d'un autre utilisateur).
  3. `POST /api/webhook/whatsapp` était FAIL-OPEN : `WHATSAPP_APP_SECRET`
     absent -> warning puis traitement du message quand même. Comme
     `whatsapp_cloud` est le provider par DÉFAUT, un secret oublié en
     production laissait le webhook principal grand ouvert.

Ces tests échouent si l'une des trois protections est retirée, ou si une
NOUVELLE route sensible est ajoutée sans authentification (liste blanche
explicite ci-dessous plutôt qu'énumération des routes protégées : ajouter une
route non listée fait échouer le test, ce qui force la décision).
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from ladini.api.main import app
from ladini.api.security import require_internal_token, verify_twilio_signature

# Routes délibérément publiques, chacune avec sa raison. Toute AUTRE route doit
# porter une dépendance d'authentification (ou vérifier un secret dans son
# corps, cf. `_BODY_CHECKED_ROUTES`).
_INTENTIONALLY_PUBLIC = {
    # Sondes d'infrastructure — aucune donnée métier, aucun effet de bord.
    ("GET", "/health"),
    ("GET", "/health/live"),
    ("GET", "/health/ready"),
    # Métadonnées de release (release / git_sha / built_at) — aucune valeur
    # secrète, injectées par l'environnement du conteneur. Consommé par les
    # smoke tests de déploiement (hardening DevOps 2026-09-10).
    ("GET", "/version"),
    # Scrape Prometheus : métriques agrégées, aucun contenu utilisateur.
    ("GET", "/metrics"),
    # Documentation OpenAPI (désactivable en prod via FastAPI(docs_url=None)).
    ("GET", "/openapi.json"),
    ("HEAD", "/openapi.json"),
    ("GET", "/docs"),
    ("HEAD", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("HEAD", "/docs/oauth2-redirect"),
    ("GET", "/redoc"),
    ("HEAD", "/redoc"),
    # Challenge de vérification Meta : compare `hub.verify_token` au secret
    # configuré dans son propre corps, ne déclenche aucune action.
    ("GET", "/api/webhook/whatsapp"),
    # IPN Paydunya : signé par personne côté Paydunya, MAIS n'extrait qu'un
    # `invoice_token` (un identifiant) et re-confirme statut ET montant
    # serveur-à-serveur auprès de Paydunya avec nos clés avant toute écriture.
    # Le corps de la requête n'est jamais une source de vérité — voir la
    # docstring de `routes/paydunya_webhook.py`.
    ("POST", "/api/webhooks/paydunya-ipn"),
}

# Routes qui vérifient un secret DANS leur corps (pas via `Depends`), parce que
# la vérification a besoin du corps brut ou d'un code de réponse spécifique.
_BODY_CHECKED_ROUTES = {
    # Signature HMAC-SHA256 sur le corps BRUT : impossible en dépendance sans
    # relire le corps deux fois.
    ("POST", "/api/webhook/whatsapp"),
    # `_check_admin_token` (header X-Admin-Token), fail-closed.
    ("GET", "/admin/llm/health"),
}


def _routes() -> list[tuple[str, str, APIRoute]]:
    out: list[tuple[str, str, APIRoute]] = []
    for route in app.routes:
        for method in sorted(getattr(route, "methods", None) or []):
            if method == "OPTIONS":
                continue
            out.append((method, route.path, route))
    return out


def _dependency_names(route) -> list[str]:
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return []
    return [
        getattr(dep.call, "__name__", str(dep.call)) for dep in dependant.dependencies
    ]


@pytest.mark.architecture
def test_every_sensitive_route_is_authenticated():
    """Aucune route hors liste blanche ne doit être sans authentification."""
    unprotected: list[str] = []
    for method, path, route in _routes():
        key = (method, path)
        if key in _INTENTIONALLY_PUBLIC or key in _BODY_CHECKED_ROUTES:
            continue
        if not _dependency_names(route):
            unprotected.append(f"{method} {path}")

    assert not unprotected, (
        "Route(s) HTTP sensible(s) sans authentification : "
        f"{sorted(unprotected)}. Ajoutez une dépendance d'auth (voir "
        "api/security.py) — ou, si la route est réellement publique, "
        "déclarez-la dans _INTENTIONALLY_PUBLIC avec sa justification."
    )


@pytest.mark.architecture
def test_twilio_webhook_validates_its_signature():
    """`POST /api/webhook/twilio` doit porter `verify_twilio_signature`.

    C'était LE trou principal : sans elle, un `From` arbitraire fait agir
    l'agent au nom de n'importe quel numéro.
    """
    matches = [
        route
        for method, path, route in _routes()
        if (method, path) == ("POST", "/api/webhook/twilio")
    ]
    assert matches, "La route POST /api/webhook/twilio a disparu."
    assert verify_twilio_signature.__name__ in _dependency_names(matches[0])


@pytest.mark.architecture
@pytest.mark.parametrize(
    "path",
    [
        "/api/market/producer",
        "/api/market/buyer",
        "/api/market/status/{task_id}",
        # (2026-09-19) Canal chat web — même raisonnement exact que
        # /api/market/* : phone_number arbitraire + force_role=True, appelé
        # depuis le BACKEND du site (jamais son navigateur), voir
        # routes/webchat.py pour le détail complet.
        "/api/webchat/producer",
        "/api/webchat/buyer",
    ],
)
def test_market_routes_require_the_internal_token(path):
    """Les routes `/api/market/*` et `/api/webchat/*` lancent l'agent pour un
    numéro arbitraire avec `force_role=True` : elles doivent exiger le secret
    interne."""
    matches = [route for _m, p, route in _routes() if p == path]
    assert matches, f"La route {path} a disparu."
    for route in matches:
        assert require_internal_token.__name__ in _dependency_names(route), (
            f"{path} n'exige plus INTERNAL_API_TOKEN — elle redevient une "
            "usurpation d'identité ouverte."
        )


@pytest.mark.architecture
def test_internal_token_is_fail_closed_when_unset(monkeypatch):
    """Secret non configuré -> 503 pour tout le monde, jamais 'ouvert par
    défaut' (même posture que ADMIN_API_TOKEN)."""
    from fastapi import HTTPException

    from ladini.core.settings import settings

    monkeypatch.setattr(settings, "INTERNAL_API_TOKEN", "", raising=False)
    with pytest.raises(HTTPException) as exc:
        require_internal_token(x_internal_token="n-importe-quoi")
    assert exc.value.status_code == 503


@pytest.mark.architecture
def test_internal_token_rejects_a_wrong_value(monkeypatch):
    from fastapi import HTTPException

    from ladini.core.settings import settings

    monkeypatch.setattr(settings, "INTERNAL_API_TOKEN", "bon-secret", raising=False)
    for bad in (None, "", "mauvais-secret", "bon-secre"):
        with pytest.raises(HTTPException) as exc:
            require_internal_token(x_internal_token=bad)
        assert exc.value.status_code == 401, bad
    # Le bon secret passe.
    require_internal_token(x_internal_token="bon-secret")


@pytest.mark.architecture
def test_twilio_signature_is_fail_closed_without_auth_token(monkeypatch):
    """Hors développement DÉCLARÉ, un `TWILIO_AUTH_TOKEN` absent doit refuser
    le webhook (503) au lieu de le laisser passer."""
    import asyncio

    from fastapi import HTTPException

    from ladini.core.settings import settings

    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ENV", "production")

    class _Req:
        headers: dict = {}
        url = type("U", (), {"path": "/api/webhook/twilio"})()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(verify_twilio_signature(_Req()))
    assert exc.value.status_code == 503


@pytest.mark.architecture
def test_whatsapp_cloud_webhook_is_fail_closed_without_app_secret(monkeypatch):
    """`WHATSAPP_APP_SECRET` absent hors dev -> 503, pas un traitement silencieux.

    Vérifie le comportement RÉEL du handler (pas seulement la présence du code)
    en l'appelant avec une requête factice.
    """
    import asyncio

    from ladini.api.routes import whatsapp_webhook as mod
    from ladini.core.settings import settings

    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "", raising=False)
    monkeypatch.setenv("ENV", "production")

    called: list[str] = []
    monkeypatch.setattr(
        mod.process_agent_task,
        "delay",
        lambda **kw: called.append(kw.get("phone_number", "?")),
        raising=False,
    )

    class _Req:
        headers: dict = {}

        async def body(self) -> bytes:
            return b'{"entry":[]}'

        async def json(self):
            return {"entry": []}

    response = asyncio.run(mod._handle_whatsapp_webhook(_Req(), None))
    assert response.status_code == 503
    assert not called, "Un message non signé a été traité malgré l'absence de secret."
