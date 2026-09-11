"""Formatage de nombres pour affichage humain (WhatsApp) — JAMAIS de notation
scientifique, JAMAIS sans séparateur de milliers.

Root cause corrigée ici : le format Python ``{:g}`` (utilisé partout dans le
code pour afficher prix/quantités) bascule automatiquement en notation
exponentielle au-delà de 6 chiffres significatifs (ex: ``f"{1500000.0:g}"``
-> ``"1.5e+06"``). Les montants FCFA dépassent couramment ce seuil (un bœuf à
486000 FCFA/TETE, un total de plusieurs millions...) — un paysan ne comprend
pas "1.5e+06 FCFA". Cette fonction est LA seule à utiliser pour formater un
nombre affiché à l'utilisateur ; ``_fmt_num`` était dupliqué (et parfois
buggé de la même façon) dans plusieurs fichiers — voir historique.

(2026-09-11) Ajout des séparateurs de milliers (notation française : espace
entre les milliers, virgule pour la décimale) — un montant affiché brut sans
séparateur (ex: "1000000.00 CFA") expose une confusion réelle rapportée par
un utilisateur : lu vite, "1000000" peut se lire comme "100000" ou "10000000"
selon où l'œil s'arrête. "1 000 000" lève l'ambiguïté immédiatement.
"""

from __future__ import annotations

import math
from typing import Any

# Comma (English thousands separator, produced by Python's `,` format spec)
# -> space ; dot (English decimal point) -> comma (French decimal separator).
# `str.translate` applies both substitutions from the ORIGINAL string
# simultaneously (not sequentially), so there is no risk of the space
# introduced for thousands being re-mangled by the dot->comma step.
_FR_GROUPING = str.maketrans({",": " ", ".": ","})


def fmt_num(value: Any) -> str:
    """Formate un nombre pour l'utilisateur : jamais de notation scientifique,
    toujours avec séparateur de milliers (notation française).

    - Entier (50.0) -> "50"
    - Grand entier (1000000.0) -> "1 000 000"
    - Décimal (12.5) -> "12,5" (jamais plus de 2 décimales, zéros superflus retirés)
    - Grand décimal (1234567.5) -> "1 234 567,5"
    - Non convertible -> str(value) tel quel (ou "" si None/vide)
    """
    if value in (None, ""):
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(f) or math.isinf(f):
        return str(value)
    if f.is_integer():
        grouped = f"{int(f):,}"
    else:
        grouped = f"{f:,.2f}".rstrip("0").rstrip(".")
    return grouped.translate(_FR_GROUPING)


__all__ = ["fmt_num"]
