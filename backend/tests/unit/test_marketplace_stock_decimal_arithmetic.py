"""`MarketplaceMixin.add_stock` / `remove_stock` — incrément/décrément sur
une vraie colonne `Numeric` (incident production 2026-09-15).

## Le gap réel fermé par ce fichier

`Stock.quantity` est une colonne SQLAlchemy `Numeric`, donc chargée en
`decimal.Decimal` — jamais un `float`. `stock.quantity += quantity` (un
`float` venu de `positive_float()`) lève `TypeError: unsupported operand
type(s) for +=: 'decimal.Decimal' and 'float'`, masqué en production
derrière le message générique `SafeDatabaseError` (même classe de bug que
`services/database/producer.py::cancel_confirmed_order`, trouvée sur le
même incident). Aucun test existant sur `add_stock`/`remove_stock` ne
faisait tourner ce chemin avec un vrai `Decimal` : les tests IDOR de
`test_farm_ownership_idor.py` s'arrêtent avant d'atteindre l'arithmétique."""
from __future__ import annotations

import types
import uuid
from decimal import Decimal

from ladini.services.database.marketplace import MarketplaceMixin
from tests.conftest import run

PRODUCER_PHONE = "+22670000001"
FARM_ID = str(uuid.uuid4())


async def _async_return(value):
    return value


def _stock(qty):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        farm_id=FARM_ID,
        item_name="Riz",
        quantity=qty,
        unit="KG",
    )


class _StockSession:
    def __init__(self, stock):
        self._stock = stock

    async def execute(self, _stmt):
        return types.SimpleNamespace(scalar_one_or_none=lambda: self._stock)

    def add(self, _obj):
        pass

    async def flush(self):
        pass


def _service(session):
    class _Svc(MarketplaceMixin):
        @property
        def session(self):
            return session

    svc = _Svc()
    svc._assert_farm_owned_by = lambda farm_id, phone: _async_return(None)
    return svc


class TestAddStockSurvivesRealDecimalColumn:
    def test_increment_on_existing_stock_with_decimal_quantity(self):
        stock = _stock(Decimal("100.0"))
        svc = _service(_StockSession(stock))

        result = run(
            svc.add_stock(
                farm_id=FARM_ID,
                item_name="Riz",
                quantity=25.0,
                producer_phone=PRODUCER_PHONE,
            )
        )

        assert result["status"] == "success"
        assert stock.quantity == 125.0


class TestRemoveStockSurvivesRealDecimalColumn:
    def test_decrement_on_existing_stock_with_decimal_quantity(self):
        stock = _stock(Decimal("100.0"))
        svc = _service(_StockSession(stock))

        result = run(
            svc.remove_stock(
                farm_id=FARM_ID,
                item_name="Riz",
                quantity=30.0,
                producer_phone=PRODUCER_PHONE,
            )
        )

        assert result["status"] == "success"
        assert stock.quantity == 70.0
