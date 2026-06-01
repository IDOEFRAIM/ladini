import asyncio
import json
import sys
from pathlib import Path

# Ensure backend/src is on sys.path so we can import the package
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "backend" / "src"
sys.path.insert(0, str(SRC))

try:
    from agriconnect.graphs.agents.formation import tools
except Exception as exc:
    print(json.dumps({"ok": False, "error": f"import error: {exc}"}, ensure_ascii=False))
    raise

async def main():
    query = "Quels sont les conseils pour semer le maïs ?"
    try:
        result = await tools.invoke_query_rag(query)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
