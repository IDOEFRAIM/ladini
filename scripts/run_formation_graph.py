"""Run the Formation agent graph end-to-end for a sample query.

Loads `backend/.env` into environment, compiles the Formation graph, and invokes it
for the query: "Donne-moi les conseils pour 2 hectares de maïs dans le Centre.".

Usage:
  & ".venv\Scripts\python.exe" scripts\run_formation_graph.py
"""
import os
import sys
import asyncio

# ensure project package imports work
sys.path.insert(0, os.path.abspath("backend/src"))

def load_env(path: str):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k.strip(), v)


def main():
    # load backend .env to pick up LLM keys and DB if needed
    env_path = os.path.join(os.path.dirname(__file__), "..", "backend", ".env")
    env_path = os.path.abspath(env_path)
    load_env(env_path)

    try:
        from agriconnect.graphs.agents.formation.graph import get_agent_graph, FormationConfig
    except Exception as exc:
        print("Failed importing formation graph:", exc)
        raise

    cfg = FormationConfig()
    # allow local bypass if shield unavailable
    os.environ.setdefault("FORMATION_LOCAL_NO_SHIELD", "1")

    print("Compiling Formation graph...")
    app = get_agent_graph(config=cfg)

    state = {
        "user_query": "Donne-moi les conseils pour 2 hectares de maïs dans le Centre.",
        "learner_profile": {"user_id": "local_test", "zone_category": "Centre", "area_ha": 2},
    }

    print("Invoking graph...")
    try:
        final = app.invoke(state)
    except Exception:
        final = asyncio.run(app.ainvoke(state))

    print("\n--- Final state ---")
    import json
    print(json.dumps(final, ensure_ascii=False, indent=2))

    print("\n--- Final response (text) ---")
    print(final.get("final_response"))


if __name__ == '__main__':
    main()
