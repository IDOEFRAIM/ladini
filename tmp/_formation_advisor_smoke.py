import asyncio
import logging
import sys
from pathlib import Path

logging.disable(logging.CRITICAL)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str((ROOT / "backend" / "src").resolve()))

from agriconnect.tools.formation_advisor import FormationAdvisor
from agriconnect.tools.crop import CropProfileNotFoundError
from agriconnect.tools.shared_math import SahelianCropProfile


class Hybrid(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def to_hybrid(value):
    if isinstance(value, dict):
        return Hybrid({k: to_hybrid(v) for k, v in value.items()})
    if isinstance(value, list):
        return [to_hybrid(v) for v in value]
    return value


async def main():
    advisor = FormationAdvisor()

    payload = {
        "name": "Maïs",
        "varieties": {"Centre": ["TZEE-W"]},
        "cycle_days": 100,
        "seeding_density": "N/A",
        "depth_cm": 5,
        "organic_matter_min_tha": 3,
        "mineral_fertilizer": {},
        "water_strategy": "Pluvial",
        "scientific_name": "Zea mays",
        "sowing_config": {"inter_row": 80, "inter_plant": 40, "seeds_pocket": 2},
        "fertilizer_plan": [
            {"step_order": 1, "stage": "Semis", "type": "NPK", "dose_kg_ha": 150, "mode": "Epandage"},
            {"step_order": 2, "stage": "30 JAS", "type": "Urée", "dose_kg_ha": 100, "mode": "Epandage"},
        ],
        "yield_potential": [3, 6],
        "key_pests": ["Chenille légionnaire"],
        "key_diseases": ["Striure"],
        "pre_flight_checks": [],
    }

    profile_payload = SahelianCropProfile.model_validate(payload).model_dump()
    hybrid_profile = to_hybrid(profile_payload)

    async def fake_get_profile_ok(*args, **kwargs):
        return hybrid_profile

    advisor.crop_tool._get_profile_from_db = fake_get_profile_ok
    result = await advisor.generate_technical_diagnosis("Maïs", "Centre", area_ha="2")

    intrants = result.get("synthese_intrants", {})
    npk_total = str(intrants.get("NPK_total", ""))
    uree_total = str(intrants.get("Uree_total", ""))

    assert "300.0 kg" in npk_total, f"NPK_total mismatch: {npk_total}"
    assert "200.0 kg" in uree_total, f"Uree_total mismatch: {uree_total}"
    print("OK_VALID")

    markdown = advisor.format_as_markdown(result)
    assert "TECHNICAL_BLOCK_START" in markdown and "TECHNICAL_BLOCK_END" in markdown, "Missing technical block markers"
    print("OK_MARKERS")

    async def fake_get_profile_missing(*args, **kwargs):
        raise CropProfileNotFoundError("not found")

    advisor.crop_tool._get_profile_from_db = fake_get_profile_missing
    missing = await advisor.generate_technical_diagnosis("Maïs", "Centre", area_ha="2")
    assert missing.get("status") == "UNAVAILABLE", f"Unexpected status: {missing.get('status')}"
    print("OK_UNAVAILABLE")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"FAIL: {exc}")
        raise SystemExit(1)
