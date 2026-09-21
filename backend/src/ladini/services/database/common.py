"""Shared helpers for the database service package.

Provides validation utilities, limit clamping, UUID generation,
and DDL statements for performance indexes.
"""

from __future__ import annotations

import math
import re
from logging import getLogger
from typing import Any, Optional

from ladini.domain.orm_base import _uuid4

logger = getLogger("ladini.services.database")


def _uuid() -> str:
    """Return a new UUID4 string."""
    return _uuid4()


# Caractères de contrôle (hors tab/newline) : neutralisés silencieusement pour
# éviter qu'un payload agent bruité ne pollue les logs ou les colonnes texte.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(
    value: Any, field: str = "champ", *, required: bool = False, max_length: int = 255
) -> Optional[str]:
    """Nettoyage générique pour les chaînes de caractères.

    Note sécurité : ne protège PAS contre l'injection SQL (inutile — toutes les
    requêtes passent par l'ORM paramétré). Sert à borner la longueur, retirer
    les caractères de contrôle et normaliser les espaces avant persistance/log.
    """
    if value is None:
        if required:
            raise ValueError(f"{field} est obligatoire")
        return None

    # Conversion en string, retrait des caractères de contrôle, trim.
    text = _CONTROL_CHARS_RE.sub("", str(value)).strip()

    if not text:
        if required:
            raise ValueError(f"{field} ne peut pas être vide")
        return None

    return text[:max_length]


def escape_like(term: str) -> str:
    """Échappe les métacaractères LIKE/ILIKE (`%`, `_`, `\\`) d'un terme LITTÉRAL.

    Sans ça, `Product.name.ilike(f"%{term}%")` avec `term="%"` matche TOUTE la
    table (sur-exposition + full scan = DoS applicatif), et `term="a_b"` traite
    `_` comme un joker. À utiliser sur tout terme de recherche libre injecté
    dans un pattern LIKE, combiné à `.ilike(pattern, escape="\\")`.

    NB : ce n'est PAS une protection anti-injection SQL (l'ORM paramètre déjà la
    valeur) mais une protection contre l'injection de MÉTACARACTÈRES LIKE.
    """
    if term is None:
        return ""
    return str(term).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def normalize_phone(phone: Any, required: bool = True) -> str:
    """
    Normalisation stricte des numéros de téléphone.
    Supprime les espaces, tirets, parenthèses et points.
    Conserve le '+' initial si présent.
    """
    if phone is None:
        if required:
            raise ValueError("Le numéro de téléphone est obligatoire")
        return ""

    phone_str = str(phone).strip()

    # On ne garde que les chiffres et le symbole '+'
    # Utile pour contrer les saisies type "+226 70 00 00 00" -> "+22670000000"
    normalized = re.sub(r"[^\d+]", "", phone_str)

    if required and not normalized:
        raise ValueError("Numéro de téléphone invalide")

    return normalized


def normalize_uuid(uuid_val: Any) -> Optional[str]:
    """Assure qu'un UUID est bien au format string propre pour SQL."""
    if not uuid_val:
        return None
    return str(uuid_val).strip().lower()


def positive_float(value: Any, field: str, *, allow_zero: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if number < 0 or (number == 0 and not allow_zero):
        raise ValueError(f"{field} must be positive")
    return number


def clamp_limit(value: Any, default: int = 20, maximum: int = 100) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, maximum))


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance orthodromique (grand cercle) entre deux points GPS, en kilomètres.

    Utilitaire partagé pour tout filtre de proximité (produits, appels
    d'offres, mise en relation producteur/acheteur). Retourne 0.0 si une
    coordonnée est manquante/nulle plutôt que de lever une erreur — cohérent
    avec le principe "l'absence de GPS ne bloque jamais" du reste du module.
    """
    if not all([lat1, lon1, lat2, lon2]):
        return 0.0
    R = 6371.0  # Rayon moyen de la Terre en km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(R * c, 2)


__all__ = [
    "_uuid",
    "logger",
    "clean_text",
    "escape_like",
    "normalize_phone",
    "normalize_uuid",
    "positive_float",
    "clamp_limit",
    "haversine_distance_km",
]
