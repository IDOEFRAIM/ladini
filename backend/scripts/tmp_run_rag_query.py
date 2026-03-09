import json
from agriconnect.protocols.mcp import get_mcp_rag_server

def main():
    srv = get_mcp_rag_server()
    print('Available tools:', [t['name'] for t in srv.list_tools()])
    args = {'query': 'irrigation best practices', 'top_k': 3}
    resp = srv.call_tool('search_agronomy_docs', args)
    try:
        # pydantic models may have dict()/json()
        if hasattr(resp, 'dict'):
            out = resp.dict()
        elif hasattr(resp, 'json'):
            out = json.loads(resp.json())
        else:
            out = resp
    except Exception:
        out = resp
    print('\nResponse:')
    print(json.dumps(out, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
