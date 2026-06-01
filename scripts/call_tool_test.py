import httpx
import json

body = {"tool": "search_agronomy_docs", "arguments": {"query": "maïs", "level": "debutant", "top_k": 2}}
try:
    r = httpx.post("http://localhost:8000/call_tool", json=body, timeout=10)
    print(r.status_code)
    print(r.text)
except Exception as exc:
    import traceback
    traceback.print_exc()
    print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
    raise
