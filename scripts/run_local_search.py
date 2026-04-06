import json
import traceback
import sys
from agriconnect.protocols.mcp.servers.rag_server import AgriRAGMCPServer

try:
    s = AgriRAGMCPServer()
    res = s.call_tool_sync('search_agronomy_docs', {'query':'physionomie du mil','level':'debutant','top_k':4})
    print(json.dumps(res, ensure_ascii=False, indent=2))
except Exception:
    traceback.print_exc()
    sys.exit(1)
