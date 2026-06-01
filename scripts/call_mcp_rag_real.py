import json
import httpx

query = "conseils sur le semis du maïs"
body = {
    "tool": "search_agronomy_docs",
    "arguments": {"query": query, "level": "debutant", "top_k": 3},
}

resp = httpx.post("http://localhost:8000/call_tool", json=body, timeout=30)
print(resp.status_code)
print(resp.text)
try:
    data = resp.json()
except Exception:
    data = {}

print("total_found:", ((data.get("data") or {}).get("total_found") if isinstance(data, dict) else None))
