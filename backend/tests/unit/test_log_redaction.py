"""core/log_redaction.py — PII/secret redaction, unit + integration (via the
real `logging` module record factory, not just the pure `redact()` helper).
"""

from __future__ import annotations

import io
import logging

import pytest

from ladini.core.log_redaction import install_log_redaction, redact


class TestRedactPure:
    def test_phone_number_is_partially_masked(self):
        out = redact("phone=+22670009999")
        assert "+22670009999" not in out
        assert out == "phone=+226******99"

    def test_multiple_phone_numbers_in_one_line(self):
        out = redact("from=+22670000000 to=+22679998888")
        assert "+22670000000" not in out
        assert "+22679998888" not in out
        assert out.count("*") > 0

    def test_phone_inside_json_like_args(self):
        out = redact('args={"phone": "+22670000002", "_idempotency_key": "x"}')
        assert "+22670000002" not in out
        assert "+226" in out  # prefix still present (operational correlation)

    def test_bearer_token_sentinel(self):
        out = redact("Authorization: Bearer secret-test")
        assert "secret-test" not in out
        assert "[REDACTED]" in out

    def test_whatsapp_token_kv_sentinel(self):
        out = redact("whatsapp_token=secret-test")
        assert "secret-test" not in out

    def test_paydunya_private_key_sentinel(self):
        out = redact("PAYDUNYA_PRIVATE_KEY=secret-test")
        assert "secret-test" not in out

    def test_redis_url_credentials_masked_but_host_kept(self):
        out = redact("REDIS_URL=redis://:secret-test@redis-host.example.com:6379/0")
        assert "secret-test" not in out
        assert "redis-host.example.com" in out  # host kept — diagnostic utile

    def test_rediss_and_postgres_urls_masked(self):
        out = redact("rediss://default:hunter2@cache.example.com:6379/0")
        assert "hunter2" not in out
        out2 = redact("postgresql+asyncpg://ladini_app:hunter2@db.example.com/ladini")
        assert "hunter2" not in out2
        assert "db.example.com" in out2

    def test_non_sensitive_text_is_left_alone(self):
        assert redact("AGENT_COMPLETED | duration_ms=881.2 | status=WAITING_INPUT") == (
            "AGENT_COMPLETED | duration_ms=881.2 | status=WAITING_INPUT"
        )

    def test_empty_and_none_safe(self):
        assert redact("") == ""

    def test_short_number_does_not_crash_and_is_still_masked(self):
        # 6 chiffres après le + : trop court pour garder préfixe+suffixe
        # sans tout exposer — entièrement masqué plutôt que de planter.
        out = redact("+123456")
        assert "123456" not in out


class TestInstallLogRedaction:
    """Bout en bout via le vrai module `logging` — prouve que la rédaction
    s'applique au message FINAL vu par n'importe quel handler/formatter en
    aval, pas seulement à la fonction pure `redact()`."""

    def _capture(self, log_fn) -> str:
        install_log_redaction()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("ladini.test.log_redaction")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False
        try:
            log_fn(logger)
        finally:
            logger.removeHandler(handler)
        return stream.getvalue()

    def test_percent_style_call_is_redacted(self):
        out = self._capture(
            lambda lg: lg.info("phone=%s | action=%s", "+22670000000", "onboard")
        )
        assert "+22670000000" not in out
        assert "+226" in out

    def test_fstring_style_call_is_redacted(self):
        phone = "+22670000000"
        out = self._capture(lambda lg: lg.info(f"Orchestrator | phone={phone}"))
        assert "+22670000000" not in out

    def test_secret_sentinel_via_real_logger(self):
        out = self._capture(
            lambda lg: lg.warning("WHATSAPP_TOKEN=secret-test rejected by upstream")
        )
        assert "secret-test" not in out

    def test_malformed_percent_args_do_not_crash_logging(self):
        # Nombre d'arguments incohérent avec le format — record.getMessage()
        # lève TypeError en interne (dans la factory ET dans le formatter
        # en aval, qui l'appelle aussi) ; Python logging avale déjà cette
        # erreur (Handler.handleError, la ligne part en stderr plutôt que
        # d'être perdue) — le seul contrat que la factory doit tenir est de
        # ne JAMAIS laisser cette exception remonter et tuer le process.
        install_log_redaction()
        logger = logging.getLogger("ladini.test.log_redaction.malformed")
        logger.info("phone=%s and %s", "+22670000000")  # ne doit pas lever

    def test_install_is_idempotent(self):
        install_log_redaction()
        install_log_redaction()  # ne doit pas empiler N factories
        out = self._capture(lambda lg: lg.info("phone=+22670000000"))
        # Une seule application de la rédaction : pas de double-masquage
        # bizarre, juste le résultat attendu une fois.
        assert out.strip() == "phone=+226******00"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
