from agriconnect.protocols.mcp.servers.agri_rag_server import AgriRAGMCPServer

print('Instantiating server')
s = AgriRAGMCPServer()
print('Calling call_tool_sync')
res = s.call_tool_sync('search_agronomy_docs', {'query':'test'})
print('Result:', res)
