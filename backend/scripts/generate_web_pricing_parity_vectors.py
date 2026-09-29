"""Génère les VECTEURS DE PARITÉ du contrat de prix d'un bid (Python = référence, web = miroir).

    python scripts/generate_web_pricing_parity_vectors.py --out ../../frontag/__tests__/auction/fixtures/pricing-parity-vectors.json

Le moteur Python (`domain/commercial_pricing_snapshot.py` + `domain/bid_award.py`) calcule les valeurs attendues ;
le web (`features/auction/pricing/*`) et le backend (`tests/unit/test_web_pricing_parity.py`) les rejouent à
l'identique. Le fichier est ensuite synchronisé dans `schema_contract/parity/` par `tests/schema/sync_contract.py`.
Ne JAMAIS éditer le JSON à la main : modifier ce générateur et relancer.
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from ladini.domain.bid_award import AwardNotPossible, build_award_decision
from ladini.domain.bid_pricing_flow import comparable_total, render_pricing_label
from ladini.domain.commercial_pricing_snapshot import (
    CommercialPricingSnapshot,
    PricingSnapshotError,
    bid_pricing_view,
    build_bid_pricing_snapshot,
)

AUCTION_ID = "11111111-1111-4111-8111-111111111111"
BUYER_ID = "22222222-2222-4222-8222-222222222222"
BID_ID = "33333333-3333-4333-8333-333333333333"
PRODUCER_ID = "44444444-4444-4444-8444-444444444444"


def plain(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else format(value.normalize(), "f")


def _cases() -> List[Dict[str, Any]]:
    def c(name, qty, unit, amount, basis, price_unit=None, **pkg):
        return {
            "name": name,
            "auction": {"id": AUCTION_ID, "buyer_id": BUYER_ID, "quantity": qty, "unit": unit},
            "bid": {
                "id": BID_ID, "producer_id": PRODUCER_ID, "amount": amount, "basis": basis,
                "price_unit": price_unit, "package_type": pkg.get("package_type"),
                "package_content_amount": pkg.get("package_content_amount"),
                "package_content_unit": pkg.get("package_content_unit"),
            },
        }

    return [
        c("A_450000_per_tonne_on_10_tonne", "10", "TONNE", "450000", "PER_BASE_UNIT", "TONNE"),
        c("B_4200000_total_lot_on_10_tonne", "10", "TONNE", "4200000", "TOTAL_LOT"),
        c("E_450000_per_tonne_on_1000_kg", "1000", "KG", "450000", "PER_BASE_UNIT", "TONNE"),
        c("450_per_kg_on_1000_kg", "1000", "KG", "450", "PER_BASE_UNIT", "KG"),
        c("450_per_kg_on_10_tonne", "10", "TONNE", "450", "PER_BASE_UNIT", "KG"),
        c("total_lot_1000000_on_3_tonne_inexact_normalized", "3", "TONNE", "1000000", "TOTAL_LOT"),
        c("500_per_litre_on_50_litre", "50", "LITRE", "500", "PER_BASE_UNIT", "LITRE"),
        c("12000_per_bag_on_25_bag", "25", "BAG", "12000", "PER_BASE_UNIT", "BAG"),
        c("450000_per_tonne_on_2_5_tonne", "2.5", "TONNE", "450000", "PER_BASE_UNIT", "TONNE"),
        c("half_up_12345_67_per_kg_on_7_5_kg", "7.5", "KG", "12345.67", "PER_BASE_UNIT", "KG"),
        c("total_lot_with_decimals_on_kg_auction", "7.5", "KG", "1234.5", "TOTAL_LOT"),
        c("package_8_crates_of_25kg", "200", "KG", "12000", "PER_PACKAGE", None,
          package_type="CAISSE", package_content_amount="25", package_content_unit="KG"),
        c("package_not_divisible_210kg_by_25kg", "210", "KG", "12000", "PER_PACKAGE", None,
          package_type="CAISSE", package_content_amount="25", package_content_unit="KG"),
        c("package_in_tonne_auction", "1", "TONNE", "12000", "PER_PACKAGE", None,
          package_type="SAC", package_content_amount="50", package_content_unit="KG"),
        # ── refus (les deux moteurs rejettent) ──
        c("refused_per_base_unit_without_unit", "10", "TONNE", "450000", "PER_BASE_UNIT", None),
        c("refused_zero_amount", "10", "TONNE", "0", "PER_BASE_UNIT", "TONNE"),
        c("refused_litre_price_on_kg_auction", "10", "KG", "450", "PER_BASE_UNIT", "LITRE"),
        c("refused_unknown_basis", "10", "TONNE", "450000", "PER_WHATEVER", "TONNE"),
    ]


def _evaluate(case: Dict[str, Any]) -> Dict[str, Any]:
    a, b = case["auction"], case["bid"]
    try:
        snap = build_bid_pricing_snapshot(
            amount=b["amount"], basis=b["basis"], price_unit=b["price_unit"],
            auction_quantity=a["quantity"], auction_unit=a["unit"],
            package_type=b["package_type"], package_content_amount=b["package_content_amount"],
            package_content_unit=b["package_content_unit"],
        )
    except PricingSnapshotError:
        return {"build_ok": False}

    cols = snap.to_bid_columns()
    columns = {
        "offeredPrice": plain(cols["offered_price"]),
        "offeredPriceBasis": cols["offered_price_basis"],
        "offeredPriceUnit": cols["offered_price_unit"],
        "offeredPriceCurrency": cols["offered_price_currency"],
        "packageType": cols["package_type"],
        "packageContentAmount": plain(cols["package_content_amount"]),
        "packageContentUnit": cols["package_content_unit"],
        "normalizedUnitPrice": plain(cols["normalized_unit_price"]),
        "normalizedUnit": cols["normalized_unit"],
        "pricingSnapshotVersion": cols["pricing_snapshot_version"],
    }
    # ce que le serveur RELIT : le snapshot reconstruit depuis les colonnes persistées
    row = SimpleNamespace(
        id=BID_ID, producer_id=PRODUCER_ID, offered_price=cols["offered_price"],
        offered_price_basis=cols["offered_price_basis"], offered_price_unit=cols["offered_price_unit"],
        offered_price_currency=cols["offered_price_currency"], package_type=cols["package_type"],
        package_content_amount=cols["package_content_amount"], package_content_unit=cols["package_content_unit"],
        normalized_unit_price=cols["normalized_unit_price"], normalized_unit=cols["normalized_unit"],
        pricing_snapshot_version=cols["pricing_snapshot_version"],
    )
    reread = CommercialPricingSnapshot.from_bid(row)
    assert reread is not None
    total = comparable_total(reread, a["quantity"], a["unit"])
    out: Dict[str, Any] = {
        "build_ok": True,
        "columns": columns,
        "label": render_pricing_label(reread),
        "comparable_total": plain(total),
        "award_ok": False,
    }
    try:
        decision = build_award_decision(
            auction_id=AUCTION_ID, bid_id=BID_ID, producer_id=PRODUCER_ID, buyer_id=BUYER_ID,
            producer_name="P", pricing=bid_pricing_view(row).snapshot,
            auction_quantity=a["quantity"], auction_unit=a["unit"],
        )
    except AwardNotPossible as exc:
        out["award_error"] = exc.reason
        return out
    out.update(
        award_ok=True,
        award_total=plain(decision.award_total),
        fingerprint=decision.fingerprint,
        idempotency_key=decision.idempotency_key,
        frozen_snapshot=decision.frozen_snapshot(),
    )
    return out


def build() -> Dict[str, Any]:
    cases = []
    for case in _cases():
        cases.append({**case, "expected": _evaluate(case)})
    legacy = {
        "name": "legacy_bid_without_certified_basis",
        "auction": {"id": AUCTION_ID, "buyer_id": BUYER_ID, "quantity": "10", "unit": "TONNE"},
        "legacy_bid": {"id": BID_ID, "producer_id": PRODUCER_ID, "offeredPrice": "400000"},
        "expected": {"certified": False, "comparable": False, "award_error": "bid_basis_unknown"},
    }
    return {"schema": "web-pricing-parity", "version": 1, "engine": "python", "cases": cases, "legacy_cases": [legacy]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    data = build()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(data['cases'])} cas + {len(data['legacy_cases'])} legacy -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
