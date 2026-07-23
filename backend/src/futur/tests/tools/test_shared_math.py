import sys
import os
import math

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from futur.tools.shared_math import SahelAgroMath


def test_hargreaves_et0_reasonable():
    # Typical Sahel values
    et0 = SahelAgroMath.calculate_hargreaves_et0(22.0, 34.0, 12.37, 200)
    assert isinstance(et0, float)
    assert et0 > 0.0 and et0 < 15.0


def test_delta_t_bounds_and_errors():
    # Normal range
    dt, advice = SahelAgroMath.calculate_delta_t(30.0, 50.0)
    assert isinstance(dt, float)
    assert advice in ("OPTIMAL", "DANGER_EVAPORATION", "RISQUE_LESSIVAGE")

    # Invalid RH
    dt2, adv2 = SahelAgroMath.calculate_delta_t(25.0, -5.0)
    assert dt2 == 0.0
    assert adv2 == "ERREUR_RH"
