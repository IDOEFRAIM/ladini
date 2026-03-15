import urllib.request, json

BASE = 'http://127.0.0.1:8000'
HEADERS = {'Content-Type': 'application/json', 'X-Session-Id': 'test-session'}

def call_tool(tool, args):
    url = f"{BASE}/api/v1/mcp/call"
    payload = {'tool': tool, 'args': args}
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=HEADERS, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"TOOL {tool} -> status", resp.status)
            print(resp.read().decode('utf-8'))
    except Exception as e:
        print(f"TOOL {tool} ERROR:", e)

if __name__ == '__main__':
    print('Calling RAG tool...')
    call_tool('search_agronomy_docs', {'query': 'riz', 'level': 'debutant', 'top_k': 2})

    print('\nCalling DB tool...')
    call_tool('persist_conversation', {'user_id': 'test-user', 'query_json': json.dumps({'q':'hello'}), 'response_json': json.dumps({'r':'ok'}), 'agent_type': 'test'})
