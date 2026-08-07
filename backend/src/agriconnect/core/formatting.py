"""Formatage de nombres pour affichage humain (WhatsApp) — JAMAIS de notation
scientifique.

Root cause corrigée ici : le format Python ``{:g}`` (utilisé partout dans le
code pour afficher prix/quantités) bascule automatiquement en notation
exponentielle au-delà de 6 chiffres significatifs (ex: ``f"{1500000.0:g}"``
-> ``"1.5e+06"``). Les montants FCFA dépassent couramment ce seuil (un bœuf à
486000 FCFA/TETE, un total de plusieurs millions...) — un paysan ne comprend
pas "1.5e+06 FCFA". Cette fonction est LA seule à utiliser pour formater un
nombre affiché à l'utilisateur ; ``_fmt_num`` était dupliqué (et parfois
buggé de la même façon) dans plusieurs fichiers — voir historique.
"""
from __future__ import annotations

import math
from typing import Any


def fmt_num(value: Any) -> str:
    """Formate un nombre pour l'utilisateur : jamais de notation scientifique.

    - Entier (50.0) -> "50"
    - Décimal (12.5) -> "12.5" (jamais plus de 2 décimales, zéros superflus retirés)
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
        return str(int(f))
    return f"{f:.2f}".rstrip("0").rstrip(".")


__all__ = ["fmt_num"]
