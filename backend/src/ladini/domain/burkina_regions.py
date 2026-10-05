"""Référentiel des 17 régions du Burkina Faso — SEULE source de vérité de l'onboarding.

Contrat : l'identité géographique d'onboarding est une RÉGION (`BurkinaRegion`), jamais une
sous-zone. Le chef-lieu sert uniquement à l'affichage et à la résolution naturelle
(« Ouaga » → Kadiogo) ; ce n'est pas une seconde notion de zone.

Ce module est PUR (aucun accès DB/LLM) : il ne fait que canoniser un texte en région. La
rattacher à une ligne `governance.zones` (opérationnelle : météo, livraison, matching) est le
rôle de `agents/onboarding.py::_resolve_zone`.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple


@dataclass(frozen=True)
class BurkinaRegion:
    slug: str  # identifiant stable
    name: str  # valeur canonique stockée / affichée
    capital: str  # chef-lieu (affichage + résolution)


REGIONS: Tuple[BurkinaRegion, ...] = (
    BurkinaRegion("bankui", "Bankui", "Dédougou"),
    BurkinaRegion("djoro", "Djôrô", "Gaoua"),
    BurkinaRegion("goulmou", "Goulmou", "Fada N'Gourma"),
    BurkinaRegion("guiriko", "Guiriko", "Bobo-Dioulasso"),
    BurkinaRegion("kadiogo", "Kadiogo", "Ouagadougou"),
    BurkinaRegion("kuilse", "Kuilsé", "Kaya"),
    BurkinaRegion("liptako", "Liptako", "Dori"),
    BurkinaRegion("nando", "Nando", "Koudougou"),
    BurkinaRegion("nakambe", "Nakambé", "Tenkodogo"),
    BurkinaRegion("nazinon", "Nazinon", "Manga"),
    BurkinaRegion("oubri", "Oubri", "Ziniaré"),
    BurkinaRegion("sirba", "Sirba", "Bogandé"),
    BurkinaRegion("soum", "Soum", "Djibo"),
    BurkinaRegion("tannounyan", "Tannounyan", "Banfora"),
    BurkinaRegion("tapoa", "Tapoa", "Diapaga"),
    BurkinaRegion("sourou", "Sourou", "Tougan"),
    BurkinaRegion("yaadga", "Yaadga", "Ouahigouya"),
)

REGION_BY_SLUG: Dict[str, BurkinaRegion] = {r.slug: r for r in REGIONS}
REGION_NAMES: Tuple[str, ...] = tuple(r.name for r in REGIONS)

# Variantes d'écriture/usage courantes — volontairement MINIMALES (surnoms usuels et
# orthographes alternatives) ; tout le reste vient des noms de régions et chefs-lieux.
_EXTRA_ALIASES: Dict[str, Tuple[str, ...]] = {
    "guiriko": ("gwiriko", "bobo"),
    "kadiogo": ("ouaga",),
    "goulmou": ("fada",),
}

#: Textes qui désignent le pays entier : pas une région, donc à clarifier.
_COUNTRY_ONLY: FrozenSet[str] = frozenset({"burkina", "burkina faso", "bf", "faso"})


def normalize_place(text: object) -> str:
    """minuscules, sans accents, apostrophes/tirets/ponctuation -> espace, espaces compactés."""
    clean = unicodedata.normalize("NFKD", str(text or "").strip().lower())
    clean = "".join(ch for ch in clean if not unicodedata.combining(ch))
    clean = re.sub(r"[^a-z0-9]+", " ", clean)
    return re.sub(r"\s+", " ", clean).strip()


def _build_alias_index() -> Dict[str, str]:
    index: Dict[str, str] = {}
    for region in REGIONS:
        for alias in (region.slug, region.name, region.capital, *_EXTRA_ALIASES.get(region.slug, ())):
            index[normalize_place(alias)] = region.slug
    return index


_ALIAS_TO_SLUG: Dict[str, str] = _build_alias_index()
#: alias sans espaces -> slug (comparaison floue tolérante « fada n gourma » / « fadangourma »)
_JOINED_ALIASES: Dict[str, str] = {a.replace(" ", ""): s for a, s in _ALIAS_TO_SLUG.items()}


@dataclass(frozen=True)
class RegionResolution:
    #: "RESOLVED" (exactement une région), "AMBIGUOUS" (plusieurs régions, ou pays seul),
    #: "UNKNOWN" (aucune région reconnue — l'appelant redemande la région).
    status: str
    region: Optional[BurkinaRegion] = None
    candidates: Tuple[BurkinaRegion, ...] = ()


def _contains_sequence(tokens: List[str], alias_tokens: List[str]) -> bool:
    n = len(alias_tokens)
    return any(tokens[i : i + n] == alias_tokens for i in range(len(tokens) - n + 1))


def _fuzzy_slugs(tokens: List[str]) -> List[str]:
    keys = {t for t in tokens if len(t) >= 4}
    keys.update(a + b for a, b in zip(tokens, tokens[1:]))
    found: List[str] = []
    for key in keys:
        match = difflib.get_close_matches(key, list(_JOINED_ALIASES), n=1, cutoff=0.84)
        if match and _JOINED_ALIASES[match[0]] not in found:
            found.append(_JOINED_ALIASES[match[0]])
    return found


def resolve_region(text: object) -> RegionResolution:
    """Résout un texte libre (« Bobo », « je suis à Ouaga », « dans le Kadiogo », « djoro »)
    vers UNE région canonique. Jamais de choix arbitraire : plusieurs régions distinctes, ou
    le pays seul, donnent `AMBIGUOUS` ; rien de reconnu donne `UNKNOWN`."""
    norm = normalize_place(text)
    if not norm:
        return RegionResolution("UNKNOWN")
    if norm in _COUNTRY_ONLY:
        return RegionResolution("AMBIGUOUS")
    tokens = norm.split()

    slugs: List[str] = []
    for alias, slug in _ALIAS_TO_SLUG.items():
        if slug not in slugs and _contains_sequence(tokens, alias.split()):
            slugs.append(slug)
    if not slugs:
        slugs = _fuzzy_slugs(tokens)

    if len(slugs) == 1:
        return RegionResolution("RESOLVED", REGION_BY_SLUG[slugs[0]], (REGION_BY_SLUG[slugs[0]],))
    if len(slugs) > 1:
        return RegionResolution("AMBIGUOUS", None, tuple(REGION_BY_SLUG[s] for s in slugs))
    if "burkina" in tokens or "faso" in tokens:
        return RegionResolution("AMBIGUOUS")  # « je suis au Burkina » : le pays, pas une région
    return RegionResolution("UNKNOWN")


def canonical_region(value: object) -> Optional[BurkinaRegion]:
    """Validation STRICTE : n'accepte que le nom canonique (ou le slug) d'une des 17 régions,
    insensible à la casse/aux accents. Ni chef-lieu, ni surnom, ni identifiant inconnu."""
    norm = normalize_place(value)
    if not norm:
        return None
    for region in REGIONS:
        if norm in (normalize_place(region.name), region.slug):
            return region
    return None


def region_list_text() -> str:
    return ", ".join(r.name for r in REGIONS)


__all__ = [
    "BurkinaRegion",
    "REGIONS",
    "REGION_BY_SLUG",
    "REGION_NAMES",
    "RegionResolution",
    "canonical_region",
    "normalize_place",
    "region_list_text",
    "resolve_region",
]
