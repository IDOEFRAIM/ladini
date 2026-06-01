from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))


def check_import_only() -> Dict[str, Any]:
    """Validate we can import retriever abstraction without initializing full stack."""
    from agriconnect.rag.retriever import AgileRetriever

    required = ["search", "search_memory"]
    missing = [name for name in required if not hasattr(AgileRetriever, name)]
    return {
        "ok": len(missing) == 0,
        "class": "AgileRetriever",
        "missing_methods": missing,
    }


class FakeRetriever:
    """Minimal contract used by RagService abstraction tests."""

    ready = True

    def search(self, query: str, user_level: str = "debutant") -> List[Dict[str, Any]]:
        return [
            {
                "text": f"fake result for: {query}",
                "score": 0.99,
                "metadata": {"source": "fake://retriever", "user_level": user_level},
            }
        ]

    def search_memory(self, user_id: str, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        return [
            {
                "id": "mem-1",
                "text": f"memory for {user_id}: {query}",
                "category": "test",
                "score": 1.0,
            }
        ][:top_k]


async def check_service_abstraction() -> Dict[str, Any]:
    """Validate RagService works with only the retriever contract (duck typing)."""
    from agriconnect.services.rag_service import RagService

    service = RagService(retriever_factory=FakeRetriever)
    warmed = await service.warmup()
    docs = await service.search_documents("test abstraction", level="expert", top_k=2)
    mem = await service.search_memory(user_id="u1", query="besoin irrigation", top_k=1)

    return {
        "ok": bool(warmed) and docs.get("total_found", 0) >= 1 and len(mem) >= 1,
        "warmup": bool(warmed),
        "documents_found": int(docs.get("total_found", 0)),
        "memory_found": len(mem),
    }


async def _amain() -> int:
    import_result = check_import_only()
    service_result = await check_service_abstraction()
    payload = {
        "import_only": import_result,
        "service_abstraction": service_result,
        "overall_ok": bool(import_result.get("ok")) and bool(service_result.get("ok")),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["overall_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))
