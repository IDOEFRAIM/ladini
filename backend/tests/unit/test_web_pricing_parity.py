"""Parité Python <-> web du contrat de prix d'un bid (Phase B2c.1).

Les vecteurs sont GÉNÉRÉS par `scripts/generate_web_pricing_parity_vectors.py` (le moteur Python fait autorité) et
rejoués tels quels côté web par `frontag/__tests__/auction/pricing-parity.test.ts`. Ce test vérifie que le fichier
committé côté frontend est EXACTEMENT ce que regénère le moteur Python actuel — un écart signifierait que l'un des
deux moteurs (ou le générateur) a divergé sans que l'autre suite ne puisse le détecter.

Le dépôt frontend (`frontag`, sibling de ce dépôt) n'est pas garanti présent dans tout environnement CI qui ne
checkoute que le backend : ce test se saute proprement (pas d'échec faux positif) s'il est absent, exactement comme
`tests/schema/sync_contract.py --check` le fait déjà pour le reste du contrat de schéma."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
GENERATOR = BACKEND / "scripts" / "generate_web_pricing_parity_vectors.py"
FRONTEND_VECTORS = BACKEND.parents[1] / "frontag" / "__tests__" / "auction" / "fixtures" / "pricing-parity-vectors.json"

pytestmark = pytest.mark.skipif(
    not FRONTEND_VECTORS.exists(),
    reason="frontag absent de cet environnement (pas de checkout cross-repo) — voir tests/schema/sync_contract.py",
)


def _load_generator():
    spec = importlib.util.spec_from_file_location("_web_pricing_vectors_gen", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_committed_vectors_match_what_the_python_engine_generates_now():
    """Le JSON committé côté frontend == `build()` du générateur, à l'octet près (après tri des clés)."""
    gen = _load_generator()
    expected = json.loads(json.dumps(gen.build(), sort_keys=True, ensure_ascii=False))
    committed = json.loads(FRONTEND_VECTORS.read_text(encoding="utf-8"))
    assert committed == expected, (
        "frontag/__tests__/auction/fixtures/pricing-parity-vectors.json est PÉRIMÉ — relancer : "
        "python scripts/generate_web_pricing_parity_vectors.py --out ../../frontag/__tests__/auction/fixtures/pricing-parity-vectors.json"
    )


def test_no_case_silently_multiplies_total_lot_by_the_auction_quantity():
    """Verrou de non-régression direct sur B7/B20 : un cas TOTAL_LOT ne doit JAMAIS avoir
    `award_total == commercial_price_amount × auction_quantity` sauf coïncidence explicite d'un vecteur dédié."""
    committed = json.loads(FRONTEND_VECTORS.read_text(encoding="utf-8"))
    for case in committed["cases"]:
        bid = case["bid"]
        if bid["basis"] != "TOTAL_LOT":
            continue
        expected = case["expected"]
        if not expected.get("award_ok"):
            continue
        amount = float(bid["amount"])
        qty = float(case["auction"]["quantity"])
        total = float(expected["award_total"])
        if qty != 1:
            assert total != pytest.approx(amount * qty), case["name"]
        assert total == pytest.approx(amount)
