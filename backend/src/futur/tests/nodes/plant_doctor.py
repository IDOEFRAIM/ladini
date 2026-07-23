import pytest
from unittest.mock import MagicMock

from agriconnect.graphs.nodes.plant_doctor import PlantHealthDoctor, PlantDoctorState


def test_diagnose_node_requires_input():
    agent = PlantHealthDoctor()
    state: PlantDoctorState = {"user_query": "", "photo_paths": [], "warnings": []}
    res = agent.diagnose_node(state)
    assert res.get("status") == "ERROR"


def test_diagnose_node_with_doctor_mock():
    agent = PlantHealthDoctor()
    agent.doctor.diagnose_and_prescribe = MagicMock(return_value={"diagnostique": "ok", "traitement_recommande": {"bio": "neem_oil"}})
    state: PlantDoctorState = {"user_query": "Feuilles jaunissantes", "culture_config": {"crop_name": "maïs"}}
    res = agent.diagnose_node(state)
    assert res.get("status") == "DIAGNOSED"
    assert "treatment_costs" in res
