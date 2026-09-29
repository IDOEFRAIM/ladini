"""Verrous d'architecture + goldens — certification du prix de correction d'une production future
(Phase B2c.7, `PRODUCTION_UPDATE_FUTURE`).

Le bug fermé : `_resolve_cycle_for_update` extrayait le prix avec la regex nue `_PRICE_RE`, puis
`update_production_fields` écrivait `MarketOffer.price_per_unit = <float brut>` — « 4 millions » sur un
lot de 10 TONNE pouvait devenir 4 000 000 FCFA/TONNE (×10). ET `pricing_snapshot` (B2c.3) n'était
jamais touché : après la « correction », la recherche acheteur lisait encore l'ANCIEN prix certifié.
Voir `domain/production_update_offer.py` et `flows/producer/flow.py::_stage_lot_correction`."""
from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from tests.conftest import StubRuntime, run

BACKEND = Path(__file__).resolve().parents[2]
SRC = BACKEND / "src" / "ladini"
FLOW = SRC / "graphs" / "agents" / "market_coach" / "flows" / "producer" / "flow.py"
PRODUCER_DB = SRC / "services" / "database" / "producer.py"
OFFER = SRC / "domain" / "production_update_offer.py"


def _function_source(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name} introuvable dans {path.name}")


# =====================================================================
# ARCHITECTURE LOCKS
# =====================================================================


class TestNoRawPriceReachesTheWriter:
    def test_lot_flow_never_uses_the_bare_price_regex(self):
        for fn in ("_resolve_cycle_for_update", "_stage_lot_correction"):
            body = _function_source(FLOW, fn)
            assert "_extract_price_correction" not in body, fn
            assert "_PRICE_RE" not in body, fn

    def test_lot_flow_certifies_via_the_shared_bid_engine(self):
        body = _function_source(FLOW, "_stage_lot_correction")
        assert "parse_bid_price(" in body
        assert "_certify_lot_price(" in body
        # le prix brut de `_parse_update_correction` est ECARTÉ, jamais persisté
        assert 'correction.pop("price"' in body and 'pending.pop("price"' in body

    def test_execution_sends_only_the_frozen_certified_offer(self):
        body = _function_source(FLOW, "_resolve_cycle_for_update")
        assert "CertifiedProductionPriceCorrection.from_state(offer_state)" in body
        assert 'gw_fields["pricing"] = parse_state' in body
        assert 'gw_fields["expected_fingerprint"] = offer.fingerprint' in body
        # un prix brut résiduel dans l'état est refusé, jamais écrit
        assert 'gw_fields.pop("price", None)' in body

    def test_recap_projects_the_certified_offer_not_a_per_unit_claim(self):
        body = _function_source(FLOW, "_format_pending_recap")
        assert "price_offer.recap_line()" in body


class TestSnapshotCanNeverGoStale:
    def test_writer_certifies_or_invalidates_never_leaves_the_snapshot(self):
        body = _function_source(PRODUCER_DB, "update_production_fields")
        assert "certify_market_offer_price_correction(" in body
        assert "invalidate_market_offer_pricing_on_edit(" in body
        assert "cycle.pricing_snapshot = snapshot_dict" in body

    def test_certified_price_per_unit_is_derived_in_decimal_not_float_math(self):
        src = OFFER.read_text(encoding="utf-8")
        body = src.split("def build_production_price_correction")[1]
        assert "quantize_normalized(total / qty)" in body
        assert "float(" not in body


# =====================================================================
# GOLDENS — flux conversationnel
# =====================================================================

import ladini.graphs.agents.market_coach.flows.producer.flow as flow  # noqa: E402
from ladini.domain.bid_pricing_flow import parse_bid_price  # noqa: E402
from ladini.domain.commercial_offer import PriceBasis  # noqa: E402
from ladini.domain.production_update_offer import (  # noqa: E402
    CertifiedProductionPriceCorrection,
    build_production_price_correction,
)

PHONE = "+22670000001"


def _listing(quantity=10, unit="TONNE"):
    return {"cycle_id": "c1", "label": "maïs", "quantity": quantity, "unit": unit}


def _rt(quantity=10, unit="TONNE"):
    return StubRuntime(
        responses={
            "list_producer_productions": {
                "status": "success",
                "data": [
                    {"cycle_id": "c1", "product_label": "maïs", "quantity": quantity, "unit": unit, "price": 100}
                ],
            }
        }
    )


def _collect(text, *, quantity=10, unit="TONNE", working=None):
    return run(
        flow._resolve_cycle_for_update(
            _rt(quantity, unit), PHONE, {"cycle_id": "c1"}, dict(working or {}), text, ""
        )
    )


def _offer_of(result):
    return CertifiedProductionPriceCorrection.from_state(
        result["working_memory"]["update_pending"]["price_offer"]
    )


class _SpyGateway:
    calls: list = []

    def __init__(self, rt):
        pass

    async def update_production(self, phone, cycle_id, **fields):
        _SpyGateway.calls.append(fields)
        return {"status": "success", "message": "OK"}


class TestGoldenA_PerUnitExplicit:
    def test_400_le_kg_is_per_base_unit_and_unchanged(self):
        result = _collect("prix 400 le kg", quantity=100, unit="KG")
        offer = _offer_of(result)
        assert offer.pricing.price_basis == PriceBasis.PER_BASE_UNIT
        assert offer.price_per_unit == Decimal("400.0000")
        assert offer.total == Decimal("40000.00")
        assert "price" not in result["working_memory"]["update_pending"]


class TestGoldenB_TotalLotNoTenXMistake:
    def test_4_millions_pour_tout_is_a_total_not_4m_per_tonne(self):
        result = _collect("prix 4 millions pour tout")
        offer = _offer_of(result)
        assert offer.pricing.price_basis == PriceBasis.TOTAL_LOT
        assert offer.total == Decimal("4000000.00")
        # jamais 4 000 000 FCFA/TONNE (la ×10) : le prix legacy divise le budget par la quantité
        assert offer.price_per_unit == Decimal("400000.0000")

    def test_recap_never_claims_a_per_unit_price_for_a_total_lot(self):
        text = _collect("prix 4 millions pour tout")["final_response"]
        assert "pour l'ensemble" in text
        assert "FCFA/TONNE" not in text
        assert "Nouveau prix : 4 000 000 FCFA/" not in text


class TestGoldenC_BareAmountIsAmbiguousNeverWrites:
    def test_prix_4_millions_alone_asks_the_basis_and_confirms_nothing(self, monkeypatch):
        _SpyGateway.calls = []
        monkeypatch.setattr(flow, "StockGateway", _SpyGatewayWithListing)
        result = _collect("prix 4 millions")
        wm = result["working_memory"]
        assert wm["update_phase"] == "COLLECT"
        assert wm["update_price_pending"]
        assert "price_offer" not in wm["update_pending"]
        assert "pour tout" in result["final_response"].lower() or "ensemble" in result["final_response"].lower()
        assert _SpyGateway.calls == []


class _SpyGatewayWithListing(_SpyGateway):
    async def list_productions(self, phone):
        return {"status": "success", "data": [{"cycle_id": "c1", "product_label": "maïs", "quantity": 10, "unit": "TONNE"}]}


class TestGoldenD_BasisReplyThenExactlyTheCertifiedTermsAreWritten:
    def test_pour_tout_reply_certifies_then_oui_writes_frozen_offer(self, monkeypatch):
        asked = _collect("prix 4 millions")
        reply = run(
            flow._resolve_cycle_for_update(
                _rt(), PHONE, {}, asked["working_memory"], "pour tout le lot", ""
            )
        )
        assert reply["working_memory"]["update_phase"] == "CONFIRM"
        offer = _offer_of(reply)
        assert offer.pricing.price_basis == PriceBasis.TOTAL_LOT and offer.total == Decimal("4000000.00")

        _SpyGateway.calls = []
        monkeypatch.setattr(flow, "StockGateway", _SpyGatewayWithListing)
        done = run(
            flow._resolve_cycle_for_update(
                _rt(), PHONE, {}, reply["working_memory"], "oui", "CONFIRM"
            )
        )
        assert done["status"] == "COMPLETED"
        (sent,) = _SpyGateway.calls
        assert "price" not in sent  # jamais de float brut
        assert sent["expected_fingerprint"] == offer.fingerprint
        assert sent["pricing"]["basis"] == "TOTAL_LOT" and sent["pricing"]["amount"] == "4000000"

    def test_reject_while_the_basis_is_pending_cancels_without_writing(self):
        asked = _collect("prix 4 millions")
        done = run(
            flow._resolve_cycle_for_update(_rt(), PHONE, {}, asked["working_memory"], "non", "REJECT")
        )
        assert "annulée" in done["final_response"]
        assert done["working_memory"]["update_price_pending"] is None


class TestGoldenE_CorrectionSwitchesBasisNotJustTheAmount:
    def test_finalement_pour_tout_switches_basis_on_the_recap(self):
        first = _collect("prix 450000 la tonne")
        assert _offer_of(first).pricing.price_basis == PriceBasis.PER_BASE_UNIT
        second = run(
            flow._resolve_cycle_for_update(
                _rt(), PHONE, {}, first["working_memory"], "finalement prix 4 millions pour tout", ""
            )
        )
        new_offer = _offer_of(second)
        assert new_offer.pricing.price_basis == PriceBasis.TOTAL_LOT
        assert new_offer.price_per_unit == Decimal("400000.0000")


class TestGoldenF_TamperedStateNeverLeaksIntoExecution:
    def test_altered_offer_or_raw_price_is_refused_not_written(self, monkeypatch):
        confirmed = _collect("prix 4 millions pour tout")["working_memory"]
        _SpyGateway.calls = []
        monkeypatch.setattr(flow, "StockGateway", _SpyGatewayWithListing)

        tampered = {**confirmed, "update_pending": {**confirmed["update_pending"]}}
        tampered["update_pending"]["price_offer"] = {**tampered["update_pending"]["price_offer"], "total": "1"}
        res = run(flow._resolve_cycle_for_update(_rt(), PHONE, {}, tampered, "oui", "CONFIRM"))
        assert res["working_memory"]["update_phase"] == "COLLECT"
        assert _SpyGateway.calls == []

        legacy = {**confirmed, "update_pending": {"price": 4000000.0}}
        res = run(flow._resolve_cycle_for_update(_rt(), PHONE, {}, legacy, "oui", "CONFIRM"))
        assert res["working_memory"]["update_phase"] == "COLLECT"
        assert _SpyGateway.calls == []


class TestGoldenG_QuantityChangeRecertifiesThePrice:
    def test_total_lot_price_follows_a_quantity_change(self):
        first = _collect("prix 4 millions pour tout")
        second = run(
            flow._resolve_cycle_for_update(_rt(), PHONE, {}, first["working_memory"], "quantité 5 tonnes", "")
        )
        offer = _offer_of(second)
        assert offer.quantity == Decimal("5") and offer.total == Decimal("4000000.00")
        assert offer.price_per_unit == Decimal("800000.0000")

    def test_package_price_is_refused_fail_closed(self):
        result = _collect("prix 12000 la caisse de 25 kg", quantity=100, unit="KG")
        assert result["working_memory"]["update_phase"] == "COLLECT"
        assert "conditionnement" in result["final_response"]
        assert "price_offer" not in result["working_memory"]["update_pending"]


# =====================================================================
# GOLDENS — couche DB (`update_production_fields` : jamais de snapshot périmé)
# =====================================================================

from ladini.domain.commercial_pricing_snapshot import market_offer_pricing_view  # noqa: E402,I001
from ladini.services.database.producer import ProducerMgmtMixin  # noqa: E402


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Session:
    def __init__(self, row):
        self._row = row

    async def execute(self, stmt):
        return _Result(self._row)

    async def flush(self):
        return None


class _Svc(ProducerMgmtMixin):
    def __init__(self, session):
        self._s = session

    @property
    def session(self):
        return self._s

    async def _resolve_producer_phone(self, phone=None, producer_id=None):
        return phone

    async def _fetch_user_entities(self, phone):
        return (None, SimpleNamespace(id="prod-1"))


def _cycle(text="300 le kg", quantity=100, unit="KG"):
    snap = parse_bid_price(text, auction_unit=unit, auction_quantity=quantity).snapshot(quantity, unit)
    import uuid

    return SimpleNamespace(
        id=uuid.uuid4(), farm_id=uuid.uuid4(), product_label="maïs", production_type="CROP", species=None,
        breed=None, status="DRAFT", available_quantity=quantity, reserved_quantity=0.0, current_stock=quantity,
        unit=unit, price_per_unit=300.0, preorder_enabled=True, is_public=True, estimated_available_at=None,
        expected_harvest_date=None, pricing_snapshot=snap.to_dict(),
    )


def _update(cycle, **fields):
    farm = SimpleNamespace(id=cycle.farm_id, name="Ferme", producer_id="prod-1")
    svc = _Svc(_Session((cycle, farm)))
    return run(svc.update_production_fields(str(cycle.id), phone=PHONE, **fields))


def _certified(text, *, quantity, unit):
    parsed = parse_bid_price(text, auction_unit=unit, auction_quantity=quantity)
    return parsed, build_production_price_correction(
        cycle_id="x", product_label="maïs", pricing=parsed.snapshot(quantity, unit), quantity=quantity, unit=unit
    )


class TestDbGolden_BuyerNeverSeesTheStalePrice:
    def test_certified_correction_replaces_snapshot_and_legacy_column_together(self):
        cycle = _cycle("300 le kg", quantity=10, unit="TONNE")
        parsed, offer = _certified("4 millions pour tout", quantity=10, unit="TONNE")
        offer = build_production_price_correction(
            cycle_id=cycle.id, product_label="maïs", pricing=parsed.snapshot(10, "TONNE"), quantity=10, unit="TONNE"
        )
        res = _update(cycle, pricing=parsed.to_state(), expected_fingerprint=offer.fingerprint)
        assert res["status"] == "success", res
        assert cycle.price_per_unit == 400000.0
        assert cycle.pricing_snapshot["price_basis"] == "TOTAL_LOT"
        view = market_offer_pricing_view(cycle)  # ce que la recherche acheteur lit
        assert view.amount == Decimal("4000000") and view.basis == PriceBasis.TOTAL_LOT

    def test_raw_price_write_invalidates_the_certified_snapshot(self):
        cycle = _cycle("300 le kg")
        res = _update(cycle, price=999.0)
        assert res["status"] == "success"
        assert cycle.price_per_unit == 999.0
        assert cycle.pricing_snapshot is None  # jamais un snapshot qui affirme encore 300/kg

    def test_unit_change_invalidates_but_a_rename_does_not(self):
        cycle = _cycle("300 le kg")
        assert _update(cycle, product_label="mil")["status"] == "success"
        assert cycle.pricing_snapshot is not None
        assert _update(cycle, unit="TONNE")["status"] == "success"
        assert cycle.pricing_snapshot is None

    def test_quantity_change_invalidates_a_total_lot_but_not_a_per_unit_snapshot(self):
        per_unit = _cycle("300 le kg")
        _update(per_unit, quantity=50)
        assert per_unit.pricing_snapshot is not None
        total = _cycle("30000 pour tout")
        _update(total, quantity=50)
        assert total.pricing_snapshot is None

    def test_changed_terms_since_confirmation_are_rejected_untouched(self):
        cycle = _cycle("300 le kg", quantity=10, unit="TONNE")
        parsed, offer = _certified("4 millions pour tout", quantity=10, unit="TONNE")
        before = dict(cycle.pricing_snapshot)
        res = _update(cycle, pricing=parsed.to_state(), expected_fingerprint=offer.fingerprint, quantity=99)
        assert res["status"] == "error" and "changé" in res["message"]
        assert cycle.pricing_snapshot == before and cycle.price_per_unit == 300.0

    def test_package_price_and_uncertified_provenance_are_refused(self):
        cycle = _cycle("300 le kg")
        pkg = {"status": "RESOLVED", "amount": "12000", "basis": "PER_PACKAGE", "package_type": "CAISSE",
               "package_content_amount": "25", "package_content_unit": "KG", "source": "USER_EXPLICIT"}
        assert _update(cycle, pricing=pkg)["status"] == "error"
        unsafe = {"status": "RESOLVED", "amount": "400", "basis": "PER_BASE_UNIT", "price_unit": "KG", "source": "UNKNOWN"}
        assert _update(cycle, pricing=unsafe)["status"] == "error"
        assert cycle.price_per_unit == 300.0 and cycle.pricing_snapshot is not None

    def test_both_certified_and_raw_price_is_refused(self):
        cycle = _cycle("300 le kg")
        parsed, _ = _certified("400 le kg", quantity=100, unit="KG")
        assert _update(cycle, pricing=parsed.to_state(), price=400.0)["status"] == "error"
