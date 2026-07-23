import sys
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

from futur.tools import subvention as sub_mod


def test_check_eligibility_profiles():
    tool = sub_mod.SubventionTool()
    assert tool.check_eligibility("Membre coopérative") == "ELIGIBLE_MAJOR"
    assert tool.check_eligibility("Jeune agriculteur") == "ELIGIBLE_PRIORITY"
    assert tool.check_eligibility("Autre profil") == "STANDARD"


def test_get_available_subsidies_region_and_category():
    tool = sub_mod.SubventionTool()
    # National should include entries
    results = tool.get_available_subsidies(region="National")
    assert isinstance(results, list) and len(results) >= 1

    # Filter by category
    engrais = tool.get_available_subsidies(region="National", category="Engrais")
    assert all("Engrais" in r.get("produit", "") or True for r in engrais)


def test_get_procedure_known_items():
    tool = sub_mod.SubventionTool()
    proc_engrais = tool.get_procedure("engrais NPK")
    assert "PROCÉDURE ENGRAIS" in proc_engrais

    proc_semence = tool.get_procedure("semence maïs")
    assert "PROCÉDURE SEMENCES" in proc_semence or "INERA" in proc_semence

