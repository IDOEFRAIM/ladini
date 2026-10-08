"""Ciblage PUR des destinataires d'une campagne (aucune I/O) : reproductible, sans doublon, déterministe.

Entrée : lignes déjà lues en base (un consentement `OPTED_IN` + l'utilisateur éventuel). Sortie : destinataires
retenus (triés par téléphone) + écartés avec raison stable. Le consentement est RECONTRÔLÉ à l'envoi
(`campaign_service` / dispatcher) : ce module ne décide que de la préparation.

Audience (`campaign.audience`, JSON) :
    regions            : liste de régions canoniques (vide = pas de filtre) ; région d'un acheteur = `declared_location`
                         résolue par `resolve_region` ; INCONNUE ou ambiguë -> écarté (`REGION_UNKNOWN`) sauf
                         `include_unknown_region: true`.
    audience_kind      : `ALL` (défaut) | `KNOWN` (compte existant) | `NEW` (consentement sans compte : découverte).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from ladini.domain.burkina_regions import canonical_region, resolve_region
from ladini.services.database.common import normalize_phone

NO_CONSENT = "NO_CONSENT"
ACCOUNT_NOT_ACTIVE = "ACCOUNT_NOT_ACTIVE"
WHATSAPP_DISABLED = "WHATSAPP_DISABLED"
INVALID_PHONE = "INVALID_PHONE"
REGION_UNKNOWN = "REGION_UNKNOWN"
REGION_MISMATCH = "REGION_MISMATCH"
KIND_MISMATCH = "KIND_MISMATCH"
DUPLICATE = "DUPLICATE"


@dataclass(frozen=True)
class Recipient:
    phone: str
    user_id: Optional[str]


def _valid_phone(phone: str) -> bool:
    digits = phone.lstrip("+")
    return digits.isdigit() and 8 <= len(digits) <= 15


def _user_region_slug(declared_location: Any) -> Optional[str]:
    res = resolve_region(declared_location)
    if res.status == "RESOLVED" and res.region is not None:
        return str(res.region.slug)
    return None


def select_recipients(
    rows: Iterable[Mapping[str, Any]], audience: Mapping[str, Any]
) -> Tuple[List[Recipient], List[Dict[str, Any]]]:
    """`rows` : dicts {phone, consent_status, user_id?, account_status?, whatsapp_enabled?, deleted_at?, declared_location?}."""
    wanted: set[str] = set()
    for r in audience.get("regions") or []:
        reg = canonical_region(r)
        if reg is not None:
            wanted.add(reg.slug)
    include_unknown = bool(audience.get("include_unknown_region"))
    kind = str(audience.get("audience_kind") or "ALL").upper()

    kept: Dict[str, Recipient] = {}
    skipped: List[Dict[str, Any]] = []

    def skip(phone: str, reason: str) -> None:
        skipped.append({"phone": phone, "reason": reason})

    for row in sorted(rows, key=lambda x: str(x.get("phone") or "")):
        phone = normalize_phone(row.get("phone"), required=False)
        if not phone or not _valid_phone(phone):
            skip(str(row.get("phone") or ""), INVALID_PHONE)
            continue
        if str(row.get("consent_status") or "") != "OPTED_IN":
            skip(phone, NO_CONSENT)
            continue
        has_user = row.get("user_id") is not None
        if kind == "KNOWN" and not has_user or kind == "NEW" and has_user:
            skip(phone, KIND_MISMATCH)
            continue
        if has_user:
            if str(row.get("account_status") or "ACTIVE").upper() != "ACTIVE" or row.get("deleted_at") is not None:
                skip(phone, ACCOUNT_NOT_ACTIVE)
                continue
            if row.get("whatsapp_enabled") is False:
                skip(phone, WHATSAPP_DISABLED)
                continue
        if wanted:
            slug = _user_region_slug(row.get("declared_location")) if has_user else None
            if slug is None:
                if not include_unknown:
                    skip(phone, REGION_UNKNOWN)
                    continue
            elif slug not in wanted:
                skip(phone, REGION_MISMATCH)
                continue
        if phone in kept:
            skip(phone, DUPLICATE)
            continue
        kept[phone] = Recipient(phone=phone, user_id=str(row["user_id"]) if has_user else None)
    return [kept[p] for p in sorted(kept)], skipped
