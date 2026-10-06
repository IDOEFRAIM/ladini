"""`llm_gateway/alerting.py` — §28/§29/§30 du brief.

Une panne = UN incident, pas une notification par requête en échec ; une
récupération = UNE seule notification. Le `WebhookNotifier` ne dépend
JAMAIS d'un LLM (§29 : déterministe, fonctionne même si tout est down)."""

from __future__ import annotations

from ladini.graphs.agents.market_coach.llm_gateway.alerting import (
    IncidentDeduplicator,
    LLMIncidentAlert,
    LogOnlyNotifier,
    WebhookNotifier,
)
from tests.unit.llm_gateway.conftest import make_fake_redis


def _alert(**overrides) -> LLMIncidentAlert:
    base = dict(
        incident_type="PRIMARY_DEGRADED",
        severity="CRITICAL",
        environment="test",
        profile="REASONING",
        provider="bedrock_gateway",
        model="deepseek.v3.2",
        reason="TIMEOUT",
    )
    base.update(overrides)
    return LLMIncidentAlert(**base)


class TestIncidentDeduplication:
    def test_the_first_failure_opens_an_incident(self):
        dedup = IncidentDeduplicator(redis_client=make_fake_redis())
        assert dedup.should_notify_open("bedrock_gateway:deepseek.v3.2") is True

    def test_repeated_failures_do_not_reopen_the_same_incident(self):
        """§28 : pas 1000 alertes pour 1000 timeouts."""
        dedup = IncidentDeduplicator(redis_client=make_fake_redis())
        candidate_key = "bedrock_gateway:deepseek.v3.2"

        first = dedup.should_notify_open(candidate_key)
        subsequent = [dedup.should_notify_open(candidate_key) for _ in range(10)]

        assert first is True
        assert all(result is False for result in subsequent)

    def test_recovery_is_notified_once_and_only_if_an_incident_was_open(self):
        dedup = IncidentDeduplicator(redis_client=make_fake_redis())
        candidate_key = "bedrock_gateway:deepseek.v3.2"
        dedup.should_notify_open(candidate_key)

        first_recovery = dedup.should_notify_recovery(candidate_key)
        second_recovery = dedup.should_notify_recovery(candidate_key)

        assert first_recovery is True
        assert second_recovery is False  # aucun incident ouvert à ce stade

    def test_a_recovery_with_no_prior_incident_is_never_notified(self):
        dedup = IncidentDeduplicator(redis_client=make_fake_redis())
        assert dedup.should_notify_recovery("nobody:home") is False

    def test_a_new_incident_can_be_opened_again_after_recovery(self):
        """Cycle complet : ouverture -> récupération -> nouvelle panne ->
        nouvelle ouverture doit de nouveau être notifiée."""
        dedup = IncidentDeduplicator(redis_client=make_fake_redis())
        candidate_key = "bedrock_gateway:deepseek.v3.2"
        dedup.should_notify_open(candidate_key)
        dedup.should_notify_recovery(candidate_key)

        assert dedup.should_notify_open(candidate_key) is True


class TestNotifiersNeverRaise:
    def test_log_only_notifier_never_raises(self):
        LogOnlyNotifier().send_incident(_alert())  # ne doit pas lever

    def test_webhook_notifier_with_an_unreachable_url_never_raises(self):
        # §29 : une alerte qui échoue à partir ne doit JAMAIS remonter et
        # dégrader le tour utilisateur.
        notifier = WebhookNotifier("http://127.0.0.1:1/unreachable", timeout_seconds=0.2)
        notifier.send_incident(_alert())  # ne doit pas lever

    def test_webhook_payload_never_contains_a_credential_field(self):
        payload = WebhookNotifier._build_payload(_alert())
        serialized = str(payload).lower()
        for forbidden in ("api_key", "authorization", "password", "secret", "token"):
            assert forbidden not in serialized
