import sys
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from futur.tools import flood_risk as flood_mod


def test_check_flood_risk_structure():
    tool = flood_mod.FloodRiskTool()
    res = tool.check_flood_risk("Ouagadougou", 12.37, -1.53)
    assert res["location"] == "Ouagadougou"
    assert "risk_level" in res and "is_rainy_season" in res


def test_get_prevention_advice_levels():
    tool = flood_mod.FloodRiskTool()
    adv = tool.get_prevention_advice("Modéré")
    assert isinstance(adv, list) and any("Sécuriser" in a or "Surveillance" in a for a in adv)
