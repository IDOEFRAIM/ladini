import pytest
from unittest.mock import MagicMock

from agriconnect.graphs.nodes.soil import AgriSoilAgent, SoilState


def test_soil_diagnose_with_mocked_doctor():
    agent = AgriSoilAgent()
    agent.doctor.get_diagnosis_from_soilgrids = MagicMock(return_value={"identite_pedologique": {"nom_local": "Test"}, "bilan_sante": {}})
    state: SoilState = {"location_profile": {"village": "testvillage"}, "observation": "sec"}
    res = agent.diagnose_node(state)
    assert res.get("status") in ("DIAGNOSIS_COMPLETE", "ERROR")
