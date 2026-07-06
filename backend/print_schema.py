import json
from agriconnect.protocols.mcp.h import TOOL_SCHEMAS
print(json.dumps(TOOL_SCHEMAS.get('declare_future_production'), indent=2))
