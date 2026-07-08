"""Domain layer for MarketCoach business logic.

This package hosts domain-specific services and immutable context/result
objects used by action handlers. It is intentionally decoupled from MCP,
registry, and transport concerns so that business rules remain testable
and stable over time.
"""
from __future__ import annotations

from .model import DomainContext, DomainEvent, ActionStarted, ActionCompleted, ActionFailed, DomainResult

__all__ = [
    "DomainContext",
    "DomainEvent",
    "ActionStarted",
    "ActionCompleted",
    "ActionFailed",
    "DomainResult",
]
