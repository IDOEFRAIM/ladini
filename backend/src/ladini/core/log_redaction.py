"""Rédaction centralisée des logs — PII et secrets, un seul point d'entrée.

(2026-09-17, follow-up pre-Hetzner) : la validation E2E précédente a confirmé
que des numéros de téléphone en clair (`phone=+226...`, `workspace=+226...`,
`args={"phone": "..."}`) apparaissent dans de nombreux logs `INFO`, tous
récupérés par Alloy (`loki.source.docker`) et expédiés vers Grafana Cloud.
Patcher les ~50 sites d'appel individuellement serait fragile (nouveau site
= nouvel oubli garanti). Ce module intercepte plutôt CHAQUE `LogRecord` créé
dans le process, quel que soit le logger, le handler ou le formatter en
aval — y compris ceux posés par gunicorn/uvicorn/Celery (jamais configurés
par `core/logger.py::setup_logging()`, volontairement non appelée côté
API/worker, voir sa docstring) et par le breadcrumb-capture de Sentry
(`sentry_sdk.integrations.logging`, qui lit lui aussi `record.getMessage()`).

Mécanisme : `logging.setLogRecordFactory()` — la factory tourne AVANT tout
filtre/handler, pour TOUT logger du process (contrairement à un `Filter`
attaché à un logger précis, qui n'est jamais invoqué pour les ancêtres lors
de la propagation — piège classique du module `logging`). On y rend le
message final (`record.getMessage()`), on le rédige, puis on le réinjecte
dans `record.msg` avec `record.args = ()` — tout formatter en aval qui
rappelle `record.getMessage()` obtient déjà la version rédigée.
"""

from __future__ import annotations

import logging
import re
from typing import Any

_installed = False

# ── Téléphones internationaux (format WhatsApp/Twilio : "+226XXXXXXXX", pas
# d'espaces dans ce codebase) — garde le préfixe pays (jusqu'à 4 chars après
# le "+") + les 2 derniers chiffres, masque le reste. Corrélation minimale
# opérationnelle (repérer "même utilisateur" dans un fil de logs) SANS
# exposer le numéro complet — même convention que
# `services/twilio_sender.py::_mask_phone` (derniers chiffres visibles),
# étendue ici à un masquage partiel réversible nulle part.
_PHONE_RE = re.compile(r"\+\d{6,15}\b")


def _mask_phone(match: "re.Match[str]") -> str:
    s = match.group(0)
    if len(s) <= 7:  # trop court pour découper sans tout exposer
        return "+" + "*" * (len(s) - 1)
    prefix = s[:4]  # "+" + jusqu'à 3 chiffres (indicatif pays le plus courant)
    suffix = s[-2:]
    masked_len = len(s) - len(prefix) - len(suffix)
    return f"{prefix}{'*' * masked_len}{suffix}"


# ── Secrets structurés "clé=valeur" / "clé: valeur" — noms de clés
# sensibles connus, valeur entière redigée. Couvre tokens WhatsApp/Twilio/
# Paydunya, credentials Redis/DB/Grafana, DSN Sentry, et tout champ générique
# `*token*`/`*secret*`/`*password*`/`*api_key*`.
_SENSITIVE_KEY = (
    r"(?:authorization|bearer|[a-z_]*token|[a-z_]*secret|[a-z_]*password"
    r"|[a-z_]*api[_-]?key|[a-z_]*apikey|[a-z_]*credential|dsn"
    r"|paydunya[a-z_]*key|master[_-]?key|private[_-]?key|public[_-]?key)"
)
_KV_RE = re.compile(
    rf"(?i)\b({_SENSITIVE_KEY})\b\s*[=:]\s*(['\"]?)([^\s'\",;}}]+)\2"
)


def _mask_kv(match: "re.Match[str]") -> str:
    key, quote = match.group(1), match.group(2)
    return f"{key}={quote}[REDACTED]{quote}"


# ── "Bearer <token>" en texte libre (headers loggés bruts, pas en k=v). ──
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-_.=]{8,}")

# ── Credentials dans une URL de connexion (redis://, rediss://,
# postgres(ql)(+driver)://) — remplace user:pass par des étoiles, garde le
# host/port/db (utile pour diagnostiquer SANS exposer le secret).
_URL_CRED_RE = re.compile(
    r"(?i)\b(rediss?|postgres(?:ql)?(?:\+\w+)?)://[^:/@\s]*:[^@\s]+@"
)


def _mask_url_cred(match: "re.Match[str]") -> str:
    return f"{match.group(1)}://***:***@"


def redact(text: str) -> str:
    """Rédige `text` — PII (téléphones) + secrets structurés/en clair.

    Pure, sans effet de bord, testable indépendamment du logging.
    """
    if not text:
        return text
    text = _URL_CRED_RE.sub(_mask_url_cred, text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _KV_RE.sub(_mask_kv, text)
    text = _PHONE_RE.sub(_mask_phone, text)
    return text


def install_log_redaction() -> None:
    """Installe la rédaction pour TOUT le process — idempotent, à appeler UNE
    fois au tout début du démarrage (avant que gunicorn/uvicorn/Celery ne
    créent leurs propres handlers, pour que la factory soit déjà active
    quand les premiers records circulent)."""
    global _installed
    if _installed:
        return

    previous_factory = logging.getLogRecordFactory()

    def _redacting_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous_factory(*args, **kwargs)
        try:
            rendered = record.getMessage()
        except Exception:
            # Message malformé (ex: %s manquant) — ne jamais faire planter
            # le logging pour une raison de rédaction ; laisser le record
            # tel quel, au pire non-rédigé plutôt que perdu.
            return record
        redacted = redact(rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        return record

    logging.setLogRecordFactory(_redacting_factory)
    _installed = True


__all__ = ["redact", "install_log_redaction"]
