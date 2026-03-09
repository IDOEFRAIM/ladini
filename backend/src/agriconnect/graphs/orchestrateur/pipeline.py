"""Pipeline helpers (audio + persistence) exported for `flow.py`.

These functions are thin wrappers that call the corresponding
instance methods on the `MessageResponseFlow` object. They provide a
clean import surface for `flow.py` and make it easier to test
post-response logic independently.
"""
from typing import Dict, Any


def generate_audio(flow, state: Dict[str, Any]) -> Dict[str, Any]:
    return flow.generate_audio(state)


def persist(flow, state: Dict[str, Any]) -> Dict[str, Any]:
    return flow.persist(state)


__all__ = ["generate_audio", "persist"]
