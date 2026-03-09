from agriconnect.protocols.mcp.servers.agri_db_server import AgriDBMCPServer
import json

s = AgriDBMCPServer()
print('Imported AgriDBMCPServer; tools:', len(s.list_tools()))
res = s.call_tool_sync('create_agent_action', {'agent_name':'agent','action_type':'notify','payload':'{bad json','user_id':'u1'})
print('create_agent_action with bad json ->', json.dumps(res, indent=2, ensure_ascii=False))
res2 = s.call_tool_sync('create_product', {'producer_id':'p1','name':'Test','price':10.0,'quantity_for_sale':5.0,'local_names':'"Maïs, Jaune (Premium)",Petit sac'})
print('create_product with csv local_names ->', json.dumps(res2, indent=2, ensure_ascii=False))
