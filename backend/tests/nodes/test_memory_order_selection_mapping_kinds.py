"""`nodes/memory.py::_ORDER_MAPPING_KINDS` — les menus numérotés posés par
`flows/producer/flow.py::_resolve_order_for_confirmation`/
`_resolve_order_for_cancellation`/`_resolve_order_for_delivery_payment`
(2026-09-13, incident WhatsApp #6).

## Le bug fermé

Ces trois resolvers posent un `MenuRequest`/`available_mapping` avec
`kind="order_confirmation"`/`"order_cancellation"`/`"order_delivery_payment"`
respectivement. Aucun de ces trois `kind` n'était dans `_ORDER_MAPPING_KINDS`
(qui ne connaissait que `"order"`/`"order_list"`/`"buyer_orders"`) : une
réponse numérique ("1") à leur menu était bien classée `SELECTION` avec
`selection_index=1` par le fast-path déterministe, mais `memory_update`
tentait quand même de la résoudre via le mapping générique — échouait à
faire correspondre le `kind` à une branche connue, tombait dans le `else`
(`payload["resolved_id"]`, jamais lu par ces resolvers), PUIS popait
`selection_index` sans condition (seuls `"product_vendor"`/`"pricing_tier"`
en étaient protégés). Le tour suivant, le resolver ne trouvait ni
`selection_index` ni `order_id` dans le payload et réaffichait le même menu
— boucle infinie réelle observée en prod ("1" répété sans effet sur la
confirmation d'une commande producteur)."""
from __future__ import annotations

import pytest

from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from tests.conftest import StubRuntime, make_state, run


@pytest.mark.parametrize(
    "mapping_kind",
    ["order_confirmation", "order_cancellation", "order_delivery_payment"],
)
class TestOrderResolverMenuKindsResolveOrderId:
    def test_a_numeric_reply_resolves_order_id_from_the_active_mapping(
        self, mapping_kind
    ):
        st = make_state(
            interpreted_event="SELECTION",
            expected_input="SELECTION",
            current_goal="PRODUCER_CONFIRM_ORDER",
            detected_intent="UNKNOWN",
            extracted_entities={"selection_index": 1},
            transaction_payload={},
            available_mapping={"1": "order-uuid-1", "2": "order-uuid-2"},
            working_memory={"available_mapping_kind": mapping_kind},
        )
        result = run(memory_update(st, StubRuntime()))

        payload = result["transaction_payload"]
        assert payload.get("order_id") == "order-uuid-1"
        # Consommé une fois résolu — sinon le resolver le relirait à tort au
        # tour suivant comme un NOUVEAU choix.
        assert "selection_index" not in payload
