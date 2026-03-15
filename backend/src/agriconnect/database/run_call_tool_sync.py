from agriconnect.protocols.mcp.servers.agri_rag_server import AgriRAGMCPServer
import json, traceback
try:
    s = AgriRAGMCPServer()
    res = s.call_tool_sync('search_agronomy_docs', {'query':'test rag query', 'level':'debutant', 'top_k':2})
    print('CALL_TOOL_SYNC RESULT:', json.dumps(res, ensure_ascii=False, indent=2))
except Exception:
    traceback.print_exc()
