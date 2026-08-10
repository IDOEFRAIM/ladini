from __future__ import annotations


def normalize_role(role: str | None) -> str:
    role_up = str(role or "").upper().strip()
    if role_up in {"BUYER", "ACHETEUR", "ACHETEUSE"}:
        return "BUYER"
    return "PRODUCER"


__all__ = [
    "normalize_role",
]
