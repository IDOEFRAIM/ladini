"""`workers/crons/*` + `workers/payments/paydunya_ipn_task.py` + automation
services — la boucle de vie complète d'un tick planifié (Celery Beat) et du
webhook de paiement.

Chaque cron suit le même contrat : `_run()` (pur, testable) ouvre une session
DB puis délègue à un service ; la tâche Celery (`bind=True`) wrappe `_run()`
et relance via `self.retry(...)` sur exception. Appeler `<task>.run()`
directement (au lieu de `.delay()`) invoque la fonction avec l'instance Task
réelle déjà liée comme `self` — testé empiriquement : hors contexte
d'exécution Celery, `self.retry(exc=exc, ...)` relève l'exception ORIGINALE
telle quelle (pas d'enveloppe `Retry`), ce qui est exactement le signal
recherché ici (« l'échec remonte, il n'est pas avalé »).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import run


class _FakeWorkerSessionCM:
    async def __aenter__(self):
        return SimpleNamespace()

    async def __aexit__(self, *exc):
        return False


def _patch_worker_session(monkeypatch, module) -> None:
    monkeypatch.setattr(module, "worker_session", lambda: _FakeWorkerSessionCM())


# =====================================================================
# crons/order_expiry.py
# =====================================================================

class TestOrderExpiryCron:
    def test_run_returns_the_service_result(self, monkeypatch):
        import agriconnect.workers.crons.order_expiry as mod
        import agriconnect.services.database.d as db_mod

        _patch_worker_session(monkeypatch, mod)
        fake_service = SimpleNamespace(expire_pending_payments=AsyncMock(
            return_value={"expired_count": 2, "expired_order_ids": ["o1", "o2"]}
        ))
        monkeypatch.setattr(db_mod, "AgriDatabaseService", lambda: fake_service)

        result = run(mod._run())
        assert result == {"expired_count": 2, "expired_order_ids": ["o1", "o2"]}

    def test_run_with_zero_expired_does_not_crash(self, monkeypatch):
        import agriconnect.workers.crons.order_expiry as mod
        import agriconnect.services.database.d as db_mod

        _patch_worker_session(monkeypatch, mod)
        fake_service = SimpleNamespace(expire_pending_payments=AsyncMock(return_value={"expired_count": 0}))
        monkeypatch.setattr(db_mod, "AgriDatabaseService", lambda: fake_service)

        assert run(mod._run()) == {"expired_count": 0}

    def test_celery_task_reraises_on_failure(self, monkeypatch):
        import agriconnect.workers.crons.order_expiry as mod

        async def _boom():
            raise ValueError("db unreachable")

        monkeypatch.setattr(mod, "_run", _boom)
        with pytest.raises(ValueError, match="db unreachable"):
            mod.run_order_expiry_cron.run()


# =====================================================================
# crons/auction_solicitation.py
# =====================================================================

class TestAuctionSolicitationCron:
    def test_run_delegates_to_the_automation_service_and_returns_its_report(self, monkeypatch):
        import agriconnect.workers.crons.auction_solicitation as mod

        _patch_worker_session(monkeypatch, mod)
        fake_report = SimpleNamespace(as_dict=lambda: {"auctions": 3})
        fake_service = SimpleNamespace(run=AsyncMock(return_value=fake_report))
        monkeypatch.setattr(mod, "AuctionAutomationService", lambda session: fake_service)

        result = run(mod._run(batch_size=50))
        assert result == {"auctions": 3}
        fake_service.run.assert_awaited_once_with(batch_size=50)

    def test_celery_task_reraises_on_failure(self, monkeypatch):
        import agriconnect.workers.crons.auction_solicitation as mod

        async def _boom(batch_size):
            raise RuntimeError("automation crashed")

        monkeypatch.setattr(mod, "_run", _boom)
        with pytest.raises(RuntimeError, match="automation crashed"):
            mod.run_auction_solicitation_cron.run()


# =====================================================================
# crons/proximity_matching.py
# =====================================================================

class TestProximityMatchingCron:
    def test_run_delegates_and_returns_its_report(self, monkeypatch):
        import agriconnect.workers.crons.proximity_matching as mod

        _patch_worker_session(monkeypatch, mod)
        fake_report = SimpleNamespace(as_dict=lambda: {"offers": 5})
        fake_service = SimpleNamespace(run=AsyncMock(return_value=fake_report))
        monkeypatch.setattr(mod, "ProximityMatchingService", lambda session: fake_service)

        result = run(mod._run(batch_size=50, recent_days=3))
        assert result == {"offers": 5}
        fake_service.run.assert_awaited_once_with(batch_size=50, recent_days=3)

    def test_celery_task_reraises_on_failure(self, monkeypatch):
        import agriconnect.workers.crons.proximity_matching as mod

        async def _boom(batch_size, recent_days):
            raise RuntimeError("matching crashed")

        monkeypatch.setattr(mod, "_run", _boom)
        with pytest.raises(RuntimeError, match="matching crashed"):
            mod.run_proximity_matching_cron.run()


# =====================================================================
# crons/outbox_dispatch.py
# =====================================================================

class TestOutboxDispatchCron:
    def test_run_delegates_to_the_dispatcher_and_returns_its_report(self, monkeypatch):
        import agriconnect.workers.crons.outbox_dispatch as mod

        fake_report = SimpleNamespace(as_dict=lambda: {"sent": 4})
        fake_dispatcher = SimpleNamespace(run=AsyncMock(return_value=fake_report))
        monkeypatch.setattr(mod, "OutboxDispatcher", lambda: fake_dispatcher)

        result = run(mod._run(batch_size=25))
        assert result == {"sent": 4}
        fake_dispatcher.run.assert_awaited_once_with(batch_size=25)

    def test_celery_task_reraises_on_failure(self, monkeypatch):
        import agriconnect.workers.crons.outbox_dispatch as mod

        async def _boom(batch_size):
            raise RuntimeError("dispatch crashed")

        monkeypatch.setattr(mod, "_run", _boom)
        with pytest.raises(RuntimeError, match="dispatch crashed"):
            mod.run_outbox_dispatch_cron.run()


# =====================================================================
# payments/paydunya_ipn_task.py — le chemin le plus sensible (argent réel)
# =====================================================================

class TestPaydunyaIpnTask:
    def test_confirm_failure_returns_an_error_status_without_touching_the_db(self, monkeypatch):
        import agriconnect.workers.payments.paydunya_ipn_task as mod
        import agriconnect.services.payments.paydunya_client as client_mod
        import agriconnect.services.database.d as db_mod

        class _FakeError(Exception):
            pass

        monkeypatch.setattr(client_mod, "PaydunyaError", _FakeError)
        fake_client = SimpleNamespace(confirm_invoice=AsyncMock(side_effect=_FakeError("timeout")))
        monkeypatch.setattr(client_mod, "PaydunyaClient", lambda: fake_client)
        fake_service = SimpleNamespace(mark_escrow_paid=AsyncMock())
        monkeypatch.setattr(db_mod, "AgriDatabaseService", lambda: fake_service)

        result = run(mod._run("token-1"))
        assert result == {"status": "error", "reason": "confirm_failed"}
        fake_service.mark_escrow_paid.assert_not_awaited()

    def test_non_completed_status_is_ignored_without_touching_the_db(self, monkeypatch):
        import agriconnect.workers.payments.paydunya_ipn_task as mod
        import agriconnect.services.payments.paydunya_client as client_mod
        import agriconnect.services.database.d as db_mod

        fake_client = SimpleNamespace(confirm_invoice=AsyncMock(return_value={"status": "pending"}))
        monkeypatch.setattr(client_mod, "PaydunyaClient", lambda: fake_client)
        fake_service = SimpleNamespace(mark_escrow_paid=AsyncMock())
        monkeypatch.setattr(db_mod, "AgriDatabaseService", lambda: fake_service)

        result = run(mod._run("token-2"))
        assert result == {"status": "ignored", "paydunya_status": "pending"}
        fake_service.mark_escrow_paid.assert_not_awaited()

    def test_completed_status_marks_escrow_paid_inside_a_worker_session(self, monkeypatch):
        """LE chemin argent : confirmation Paydunya réussie doit débiter le
        stock/générer l'OTP via `mark_escrow_paid`, à l'intérieur d'une
        session worker explicitement ouverte (sans quoi la méthode lève
        `BusinessRuleException` — bug déjà vécu en prod, voir le commentaire
        du fichier source)."""
        import agriconnect.workers.payments.paydunya_ipn_task as mod
        import agriconnect.services.payments.paydunya_client as client_mod
        import agriconnect.services.database.d as db_mod

        _patch_worker_session(monkeypatch, mod)
        fake_client = SimpleNamespace(confirm_invoice=AsyncMock(return_value={"status": "completed"}))
        monkeypatch.setattr(client_mod, "PaydunyaClient", lambda: fake_client)
        fake_service = SimpleNamespace(mark_escrow_paid=AsyncMock(
            return_value={"order_id": "o1", "already_processed": False}
        ))
        monkeypatch.setattr(db_mod, "AgriDatabaseService", lambda: fake_service)

        result = run(mod._run("token-3"))
        assert result == {"order_id": "o1", "already_processed": False}
        fake_service.mark_escrow_paid.assert_awaited_once_with("token-3")

    def test_celery_task_retries_on_unexpected_exception(self, monkeypatch):
        import agriconnect.workers.payments.paydunya_ipn_task as mod

        async def _boom(invoice_token):
            raise RuntimeError("unexpected crash")

        monkeypatch.setattr(mod, "_run", _boom)
        with pytest.raises(RuntimeError, match="unexpected crash"):
            mod.process_paydunya_ipn.run("token-x")


# =====================================================================
# automation/auction_automation_service.py
# =====================================================================

class TestAuctionAutomationService:
    def _service(self, monkeypatch, *, auctions, producers, created, skipped=None, session=None):
        import agriconnect.workers.automation.auction_automation_service as mod

        session = session or SimpleNamespace()
        monkeypatch.setattr(mod.solicitation_repo, "fetch_auctions_to_solicit", AsyncMock(return_value=auctions))
        monkeypatch.setattr(mod, "producers_for_auction", AsyncMock(return_value=producers))
        monkeypatch.setattr(
            mod.solicitation_repo,
            "upsert_auction_solicitations",
            AsyncMock(return_value={"created": created, "skipped": skipped or []}),
        )
        monkeypatch.setattr(mod.outbox_repo, "enqueue", AsyncMock(return_value=len(created)))
        monkeypatch.setattr(mod.solicitation_repo, "mark_notified", AsyncMock())
        return mod.AuctionAutomationService(session), mod

    def test_no_auctions_yields_an_empty_report(self, monkeypatch):
        service, _ = self._service(monkeypatch, auctions=[], producers=[], created=[])
        report = run(service.run())
        assert report.as_dict() == {
            "auctions": 0, "producers_targeted": 0, "solicitations_created": 0,
            "outbox_enqueued": 0, "errors": [],
        }

    def test_full_pipeline_enqueues_and_marks_notified(self, monkeypatch):
        auction = SimpleNamespace(id="a1", sub_category_id="sc1", target_zone_id="z1", quantity=100, unit="kg", max_price_per_unit=250)
        producers = [{"producer_id": "p1", "user_id": "u1", "phone": "+2260"}]
        created = [{"solicitation_id": "sol-1", "producer_id": "p1"}]
        service, mod = self._service(monkeypatch, auctions=[auction], producers=producers, created=created)
        monkeypatch.setattr(service, "_sub_category_name", AsyncMock(return_value="Tomates"))

        report = run(service.run())
        assert report.auctions == 1
        assert report.producers_targeted == 1
        assert report.solicitations_created == 1
        assert report.outbox_enqueued == 1
        mod.solicitation_repo.mark_notified.assert_awaited_once_with(service.session, ["sol-1"])

    def test_producer_without_phone_is_skipped_from_the_outbox(self, monkeypatch):
        auction = SimpleNamespace(id="a1", sub_category_id="sc1", target_zone_id="z1", quantity=1, unit="kg", max_price_per_unit=1)
        producers = [{"producer_id": "p1", "user_id": "u1", "phone": None}]
        created = [{"solicitation_id": "sol-1", "producer_id": "p1"}]
        service, mod = self._service(monkeypatch, auctions=[auction], producers=producers, created=created)
        monkeypatch.setattr(service, "_sub_category_name", AsyncMock(return_value="Tomates"))

        report = run(service.run())
        assert report.outbox_enqueued == 0
        mod.outbox_repo.enqueue.assert_not_awaited()
        mod.solicitation_repo.mark_notified.assert_not_awaited()

    def test_no_newly_created_solicitations_skips_outbox_entirely(self, monkeypatch):
        auction = SimpleNamespace(id="a1", sub_category_id="sc1", target_zone_id="z1", quantity=1, unit="kg", max_price_per_unit=1)
        producers = [{"producer_id": "p1", "user_id": "u1", "phone": "+2260"}]
        service, mod = self._service(monkeypatch, auctions=[auction], producers=producers, created=[])

        report = run(service.run())
        assert report.solicitations_created == 0
        mod.outbox_repo.enqueue.assert_not_awaited()

    def test_one_failing_auction_does_not_abort_the_whole_batch(self, monkeypatch):
        """Une enchère qui casse (données corrompues, etc.) ne doit jamais
        empêcher les AUTRES enchères du lot d'être traitées."""
        good = SimpleNamespace(id="a-good", sub_category_id="sc1", target_zone_id="z1", quantity=1, unit="kg", max_price_per_unit=1)
        bad = SimpleNamespace(id="a-bad", sub_category_id="sc1", target_zone_id="z1", quantity=1, unit="kg", max_price_per_unit=1)
        producers = [{"producer_id": "p1", "user_id": "u1", "phone": "+2260"}]
        created = [{"solicitation_id": "sol-1", "producer_id": "p1"}]

        service, mod = self._service(monkeypatch, auctions=[bad, good], producers=producers, created=created)
        monkeypatch.setattr(service, "_sub_category_name", AsyncMock(return_value="Tomates"))

        call_count = {"n": 0}
        original_process = service._process_auction

        async def _flaky(auction, report, targeting_limit):
            call_count["n"] += 1
            if auction.id == "a-bad":
                raise ValueError("corrupted auction row")
            return await original_process(auction, report, targeting_limit)

        monkeypatch.setattr(service, "_process_auction", _flaky)
        report = run(service.run())

        assert call_count["n"] == 2, "les DEUX enchères doivent avoir été tentées"
        assert len(report.errors) == 1
        assert "a-bad" in report.errors[0]
        assert report.solicitations_created == 1, "la bonne enchère doit quand même aboutir"

    def test_all_producers_already_solicited_logs_a_reason_per_producer(self, monkeypatch, caplog):
        """Incident 2026-08-24 : le rapport agrégé seul (`producers_targeted=2,
        solicitations_created=0`) ne dit pas SI c'est parce que les 2
        producteurs étaient déjà sollicités, avaient des données corrompues,
        etc. `upsert_auction_solicitations` doit désormais motiver chaque
        producteur écarté, et le service doit journaliser cette raison."""
        import logging

        auction = SimpleNamespace(id="a1", sub_category_id="sc1", target_zone_id="z1", quantity=1, unit="kg", max_price_per_unit=1)
        producers = [
            {"producer_id": "p1", "user_id": "u1", "phone": "+2260"},
            {"producer_id": "p2", "user_id": "u2", "phone": "+2261"},
        ]
        skipped = [
            {"producer_id": "p1", "reason": "already_solicited"},
            {"producer_id": "p2", "reason": "already_solicited"},
        ]
        service, mod = self._service(
            monkeypatch, auctions=[auction], producers=producers, created=[], skipped=skipped
        )

        with caplog.at_level(logging.INFO, logger="AgriConnect.Workers.AuctionAutomation"):
            report = run(service.run())

        assert report.producers_targeted == 2
        assert report.solicitations_created == 0
        mod.outbox_repo.enqueue.assert_not_awaited()

        decisions = [r.message for r in caplog.records if "AuctionSolicitationDecision" in r.message]
        assert len(decisions) == 2, "chaque producteur écarté doit être motivé individuellement"
        assert all("already_solicited" in d for d in decisions)
        assert all("SKIPPED" in d for d in decisions)

    def test_sub_category_name_defaults_when_missing(self, monkeypatch):
        session = SimpleNamespace(scalar=AsyncMock(return_value=None))
        service, _ = self._service(monkeypatch, auctions=[], producers=[], created=[], session=session)
        assert run(service._sub_category_name(None)) == "un produit"
        session.scalar.assert_not_awaited()

    def test_sub_category_name_falls_back_when_lookup_returns_nothing(self, monkeypatch):
        session = SimpleNamespace(scalar=AsyncMock(return_value=None))
        service, _ = self._service(monkeypatch, auctions=[], producers=[], created=[], session=session)
        assert run(service._sub_category_name("sc-ghost")) == "un produit"
        session.scalar.assert_awaited_once()

    def test_num_returns_none_for_non_numeric_values(self):
        from agriconnect.workers.automation.auction_automation_service import _num
        assert _num("not-a-number") is None
        assert _num(None) is None
        assert _num("42.5") == 42.5


# =====================================================================
# automation/proximity_matching_service.py
# =====================================================================

class TestProximityMatchingService:
    def _service(self, monkeypatch, *, rows, created):
        import agriconnect.workers.automation.proximity_matching_service as mod

        session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))
        monkeypatch.setattr(mod, "buyers_in_zone_for_category", AsyncMock(return_value=[{"buyer_id": "b1", "user_id": "u1", "phone": "+2260"}]))
        monkeypatch.setattr(mod.solicitation_repo, "upsert_offer_solicitations", AsyncMock(return_value=created))
        monkeypatch.setattr(mod.outbox_repo, "enqueue", AsyncMock(return_value=len(created)))
        monkeypatch.setattr(mod.solicitation_repo, "mark_notified", AsyncMock())
        return mod.ProximityMatchingService(session), mod

    def test_no_offers_yields_an_empty_report(self, monkeypatch):
        service, _ = self._service(monkeypatch, rows=[], created=[])
        report = run(service.run())
        assert report.as_dict() == {
            "offers": 0, "buyers_targeted": 0, "solicitations_created": 0,
            "outbox_enqueued": 0, "errors": [],
        }

    def test_full_pipeline_enqueues_alerts_for_local_buyers(self, monkeypatch):
        offer = SimpleNamespace(id="o1", sub_category_id="sc1", product_label="Tomates", price_per_unit=250, unit="kg")
        rows = [(offer, "z1", "Ferme Bio")]
        created = [{"solicitation_id": "sol-1", "buyer_id": "b1"}]
        service, mod = self._service(monkeypatch, rows=rows, created=created)

        report = run(service.run())
        assert report.offers == 1
        assert report.buyers_targeted == 1
        assert report.solicitations_created == 1
        assert report.outbox_enqueued == 1
        mod.solicitation_repo.mark_notified.assert_awaited_once_with(service.session, ["sol-1"])

    def test_buyer_without_phone_is_skipped(self, monkeypatch):
        import agriconnect.workers.automation.proximity_matching_service as mod
        offer = SimpleNamespace(id="o1", sub_category_id="sc1", product_label="Tomates", price_per_unit=250, unit="kg")
        session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [(offer, "z1", "Ferme Bio")])))
        monkeypatch.setattr(mod, "buyers_in_zone_for_category", AsyncMock(return_value=[{"buyer_id": "b1", "user_id": "u1", "phone": None}]))
        monkeypatch.setattr(mod.solicitation_repo, "upsert_offer_solicitations", AsyncMock(return_value=[{"solicitation_id": "sol-1", "buyer_id": "b1"}]))
        monkeypatch.setattr(mod.outbox_repo, "enqueue", AsyncMock())
        monkeypatch.setattr(mod.solicitation_repo, "mark_notified", AsyncMock())

        service = mod.ProximityMatchingService(session)
        report = run(service.run())
        assert report.outbox_enqueued == 0
        mod.outbox_repo.enqueue.assert_not_awaited()

    def test_one_failing_offer_does_not_abort_the_whole_batch(self, monkeypatch):
        import agriconnect.workers.automation.proximity_matching_service as mod
        good = SimpleNamespace(id="o-good", sub_category_id="sc1", product_label="Maïs", price_per_unit=100, unit="kg")
        bad = SimpleNamespace(id="o-bad", sub_category_id="sc1", product_label="Riz", price_per_unit=100, unit="kg")
        rows = [(bad, "z1", "P1"), (good, "z1", "P2")]
        created = [{"solicitation_id": "sol-1", "buyer_id": "b1"}]
        service, mod = self._service(monkeypatch, rows=rows, created=created)

        original_process = service._process_offer

        async def _flaky(offer, zone_id, producer_name, report, targeting_limit):
            if offer.id == "o-bad":
                raise ValueError("corrupted offer row")
            return await original_process(offer, zone_id, producer_name, report, targeting_limit)

        monkeypatch.setattr(service, "_process_offer", _flaky)
        report = run(service.run())

        assert len(report.errors) == 1
        assert "o-bad" in report.errors[0]
        assert report.solicitations_created == 1

    def test_no_newly_created_solicitations_skips_outbox_entirely(self, monkeypatch):
        offer = SimpleNamespace(id="o1", sub_category_id="sc1", product_label="Tomates", price_per_unit=250, unit="kg")
        rows = [(offer, "z1", "Ferme Bio")]
        service, mod = self._service(monkeypatch, rows=rows, created=[])
        report = run(service.run())
        assert report.solicitations_created == 0
        mod.outbox_repo.enqueue.assert_not_awaited()

    def test_num_returns_none_for_non_numeric_values(self):
        from agriconnect.workers.automation.proximity_matching_service import _num
        assert _num("not-a-number") is None
        assert _num(None) is None
        assert _num("42.5") == 42.5
