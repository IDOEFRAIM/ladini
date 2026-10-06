"""`ProducerMgmtMixin.add_stock_movement` — incrément/décrément sur une
vraie colonne `Numeric` (incident production 2026-09-15).

## Le gap réel fermé par ce fichier

Même classe de bug que `cancel_confirmed_order` sur le même incident :
`Stock.quantity` est chargée en `decimal.Decimal`, et `quantity` (le
paramètre appelant) est un `float` — `Decimal += float` / `Decimal -=
float` lève `TypeError`, masqué en production derrière le message
générique `SafeDatabaseError`. Aucun test existant n'exerçait
`add_stock_movement`."""
from __future__ import annotations

import types
import uuid
from decimal import Decimal

from ladini.services.database.producer import ProducerMgmtMixin
from tests.conftest import run

PHONE = "+22670000001"


async def _async_return(value):
    return value


def _stock(qty, phone=PHONE):
    user = types.SimpleNamespace(phone=phone)
    producer = types.SimpleNamespace(user=user)
    farm = types.SimpleNamespace(producer=producer)
    return types.SimpleNamespace(
        id=uuid.uuid4(), farm=farm, item_name="Riz", quantity=qty, unit="KG",
    )


class _StockMovementSession:
    def __init__(self, stock):
        self._stock = stock

    async def execute(self, _stmt):
        return types.SimpleNamespace(
            unique=lambda: types.SimpleNamespace(
                scalar_one_or_none=lambda: self._stock
            )
        )

    def add(self, _obj):
        pass

    async def flush(self):
        pass


def _service(session):
    class _Svc(ProducerMgmtMixin):
        @property
        def session(self):
            return session

    return _Svc()


class TestAddStockMovementSurvivesRealDecimalColumn:
    def test_in_movement_increments_a_decimal_quantity(self):
        stock = _stock(Decimal("100.0"))
        svc = _service(_StockMovementSession(stock))

        result = run(
            svc.add_stock_movement(phone=PHONE, stock_id=str(stock.id), mtype="IN", quantity=25.0)
        )

        assert result["status"] == "success"
        assert stock.quantity == 125.0

    def test_out_movement_decrements_a_decimal_quantity(self):
        stock = _stock(Decimal("100.0"))
        svc = _service(_StockMovementSession(stock))

        result = run(
            svc.add_stock_movement(phone=PHONE, stock_id=str(stock.id), mtype="OUT", quantity=30.0)
        )

        assert result["status"] == "success"
        assert stock.quantity == 70.0
