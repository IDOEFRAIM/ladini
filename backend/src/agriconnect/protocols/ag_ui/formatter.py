from typing import Any, Dict, List, Optional
from .components import AgriResponse, Card, ActionButton, ListPicker, TextBlock


class UIFormatter:
    """Helper to build `AgriResponse` objects from raw agent outputs.

    Agents should use this central formatter to avoid embedding UI logic.
    """

    @staticmethod
    def as_card(title: str, body: str, agent: str = "", fields: Optional[List[Dict[str, str]]] = None, actions: Optional[List[Dict[str, Any]]] = None) -> AgriResponse:
        resp = AgriResponse(agent_name=agent)
        resp.add(Card(title=title, body=body, fields=fields or []))
        if actions:
            for a in actions:
                btn = ActionButton(label=a.get("label", ""), action_type=a.get("action_type"), payload=a.get("payload", {}))
                resp.add(btn)
        return resp

    @staticmethod
    def as_text(text: str, agent: str = "") -> AgriResponse:
        resp = AgriResponse(agent_name=agent)
        resp.add(TextBlock(content=text))
        return resp

    @staticmethod
    def as_list(title: str, items: List[Dict[str, str]], agent: str = "", multi_select: bool = False) -> AgriResponse:
        resp = AgriResponse(agent_name=agent)
        resp.add(ListPicker(title=title, items=items, multi_select=multi_select))
        return resp
