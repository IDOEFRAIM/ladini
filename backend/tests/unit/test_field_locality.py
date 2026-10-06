"""Localités du terrain : villes -> les 17 régions canoniques (fautes tolérées) ; un quartier inconnu n'est JAMAIS rattaché arbitrairement."""
from __future__ import annotations

import pytest

from ladini.domain.burkina_regions import resolve_region


@pytest.mark.parametrize("text,region", [
    ("Ouagadougou", "Kadiogo"), ("Ouaga", "Kadiogo"), ("sur Ouaga", "Kadiogo"), ("ouagadogou", "Kadiogo"),
    ("je suis a ouagadogou", "Kadiogo"), ("Bobo", "Guiriko"), ("bobo-dioulasso", "Guiriko"), ("Kadiogo", "Kadiogo"),
])
def test_cities_and_typos_resolve_to_the_canonical_region(text, region):
    hit = resolve_region(text)
    assert hit.status == "RESOLVED" and hit.region is not None and hit.region.name == region


@pytest.mark.parametrize("text", ["Tampouy", "vers Tampouy", "à côté de Patte d'Oie", "Karpala"])
def test_an_unknown_fine_locality_is_never_mapped_arbitrarily(text):
    hit = resolve_region(text)
    assert hit.status != "RESOLVED" and hit.region is None
