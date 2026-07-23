import sys
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from futur.tools import crop as crop_mod


def test_get_technical_sheet_known_crop():
    tool = crop_mod.BurkinaCropTool()
    txt = tool.get_technical_sheet("maïs", "Centre")
    assert "fiche technique" in txt.lower()
    assert "maïs" in txt.lower() or "mais" in txt.lower()


def test_calculate_inputs_and_unknown():
    tool = crop_mod.BurkinaCropTool()
    inp = tool.calculate_inputs("maïs", 1.0)
    assert "NPK_sacs" in inp and inp["NPK_sacs"] > 0

    assert tool.calculate_inputs("inconnue", 1.0) == {}
