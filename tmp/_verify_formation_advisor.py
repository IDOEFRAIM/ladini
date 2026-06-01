import asyncio
import importlib.util
import importlib.machinery
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"
sys.path.insert(0, str(BACKEND_SRC))

agri_pkg = types.ModuleType("agriconnect")
agri_pkg.__path__ = [str(BACKEND_SRC / "agriconnect")]
sys.modules.setdefault("agriconnect", agri_pkg)

tools_pkg = types.ModuleType("agriconnect.tools")
tools_pkg.__path__ = [str(BACKEND_SRC / "agriconnect" / "tools")]
sys.modules["agriconnect.tools"] = tools_pkg


def load_source_module(mod_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, str(file_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_pyc_module(mod_name: str, pyc_path: Path):
    loader = importlib.machinery.SourcelessFileLoader(mod_name, str(pyc_path))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    loader.exec_module(mod)
    return mod


shared_mod = load_source_module("agriconnect.tools.shared_math", BACKEND_SRC / "agriconnect" / "tools" / "shared_math.py")
crop_mod = load_source_module("agriconnect.tools.crop", BACKEND_SRC / "agriconnect" / "tools" / "crop.py")
formation_mod = load_pyc_module("agriconnect.tools.formation_advisor", BACKEND_SRC / "agriconnect" / "tools" / "__pycache__" / "formation_advisor.cpython-313.pyc")

FormationAdvisor = formation_mod.FormationAdvisor
CropProfileNotFoundError = crop_mod.CropProfileNotFoundError
SahelianCropProfile = shared_mod.SahelianCropProfile


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
            {"step_order": 1, "stage": "Semis", "type": "NPK", "dose_kg_ha": 100, "mode": "Epandage"},
            {"step_order": 2, "stage": "45 JAS", "type": "NPK", "dose_kg_ha": 100, "mode": "Epandage"},
            {"step_order": 3, "stage": "30 JAS", "type": "Urée", "dose_kg_ha": 50, "mode": "Epandage"},
            {"step_order": 4, "stage": "50 JAS", "type": "Urée", "dose_kg_ha": 50, "mode": "Epandage"},
        ],
        "yield_potential": [3.0, 5.0],
        "key_pests": ["Chenille légionnaire"],
        "key_diseases": ["Striure"],
        "pre_flight_checks": [],
    }

    profile_payload = SahelianCropProfile.model_validate(payload).model_dump()
    hybrid_profile = to_hybrid(profile_payload)

    async def fake_get_profile_ok(*args, **kwargs):
        return hybrid_profile

    advisor.crop_tool._get_profile_from_db = fake_get_profile_ok
    result_ok = await advisor.generate_technical_diagnosis(crop="Maïs", zone="Centre", area_ha="2")
    markdown = advisor.format_as_markdown(result_ok)

    status_ok = result_ok.get("status")
    npk_total = result_ok.get("synthese_intrants", {}).get("NPK_total")
    uree_total = result_ok.get("synthese_intrants", {}).get("Uree_total")
    print(f"OK status={status_ok}")
    print(f"OK NPK_total={npk_total}")
    print(f"OK Uree_total={uree_total}")
    print(f"OK markers=start={'TECHNICAL_BLOCK_START' in markdown},end={'TECHNICAL_BLOCK_END' in markdown}")

    async def fake_get_profile_missing(*args, **kwargs):
        raise CropProfileNotFoundError("not found")

    advisor.crop_tool._get_profile_from_db = fake_get_profile_missing
    result_missing = await advisor.generate_technical_diagnosis(crop="Maïs", zone="Centre", area_ha="2")
    print(f"MISSING status={result_missing.get('status')}")


if __name__ == "__main__":
    asyncio.run(main())
