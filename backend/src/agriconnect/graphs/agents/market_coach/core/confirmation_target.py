"""`ConfirmationTarget` — snapshot minimal "quel draft, quelle version" que
référence `PendingInteraction(kind=CONFIRM_ACTION).target` (2026-09-03,
hardening transverse avant migration SALES).

## Pourquoi cette extraction, maintenant

`ConfirmationTarget` existait déjà, DUPLIQUÉ à l'identique, dans
`domain/procurement_draft.py` et `domain/preorder_draft.py` — décision
délibérée à l'époque ("le dupliquer coûte moins que le coupler", un type à
2 champs). Cette justification tenait pour DEUX copies. Une TROISIÈME copie
pour `SalesPublishDraft` (mandat SALES, section 13) ferait basculer
l'équilibre : au-delà de 2 occurrences, la duplication devient elle-même le
risque (une correction faite dans une copie, oubliée dans les 2 autres).
Extraction MINIMALE — aucun champ ajouté, aucun comportement changé, le
seul coût est un `Protocol` structurel (`HasDraftIdentity`) au lieu d'un
type nominal par domaine pour `matches()`.

`ProcurementDraft`/`PreorderDraft`/`SalesPublishDraft` restent des types
séparés (RÈGLE ABSOLUE du mandat hardening) — ce module ne les couple PAS
entre eux, il ne dépend d'AUCUN d'eux : `matches()` accepte n'importe quel
objet exposant `draft_id`/`version`, vérifié structurellement (duck typing
via `Protocol`, pas un import croisé)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class HasDraftIdentity(Protocol):
    draft_id: str
    version: int


@dataclass(frozen=True)
class ConfirmationTarget:
    """Ce que référence `PendingInteraction(kind=CONFIRM_ACTION).target` —
    remplace le vague "on attend probablement un oui" par un objet précis :
    "le prochain message est une décision sur CE draft, CETTE version"."""

    draft_id: str
    draft_version: int

    def matches(self, draft: Optional[HasDraftIdentity]) -> bool:
        return (
            draft is not None
            and self.draft_id == draft.draft_id
            and self.draft_version == draft.version
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"draft_id": self.draft_id, "draft_version": self.draft_version}

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["ConfirmationTarget"]:
        if not isinstance(d, dict) or not d.get("draft_id"):
            return None
        try:
            version = int(d.get("draft_version"))
        except (TypeError, ValueError):
            return None
        return cls(draft_id=str(d["draft_id"]), draft_version=version)


__all__ = ["ConfirmationTarget", "HasDraftIdentity"]
