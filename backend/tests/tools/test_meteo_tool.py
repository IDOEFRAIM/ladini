import sys
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from futur.tools import meteo as meteo_mod
from futur.tools.shared_math import SoilType


def test_calculate_pe_values():
    tool = meteo_mod.MeteoAdvisorTool()
    # For sand soil, coeff 0.5 -> 10 * 0.5 = 5.0
    assert tool._calculate_pe(10, SoilType.SABLEUX) == 5.0
    # If rain <=5 -> 0
    assert tool._calculate_pe(3, SoilType.ARGILEUX) == 0.0


def test_get_daily_diagnosis_unknown_crop():
    tool = meteo_mod.MeteoAdvisorTool()
    res = tool.get_daily_diagnosis("unknown", SoilType.STANDARD, 22, 32, 60, 0, 120, 12.0)
    assert "error" in res


def test_get_daily_diagnosis_known_crop():
    tool = meteo_mod.MeteoAdvisorTool()
    res = tool.get_daily_diagnosis("maïs", SoilType.STANDARD, 22, 32, 60, 10, 120, 12.0)
    assert res.get("culture") == "Maïs"
    assert "etc" in res and "bilan" in res
