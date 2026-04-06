from agriconnect.protocols.mcp.tools.agronomy import AgronomyTools
import asyncio
import json
import traceback
try:
    provider = AgronomyTools()
    res = asyncio.run(provider.search_agronomy_docs(query='test rag query', level='debutant', top_k=2))
    print('CALL_TOOL_SYNC RESULT:', json.dumps(res, ensure_ascii=False, indent=2))
except Exception:
    traceback.print_exc()
