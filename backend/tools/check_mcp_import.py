import sys
import os
ROOT = os.path.abspath(os.path.join(os.getcwd(), '..'))
# Ensure backend/src on path
sys.path.insert(0, os.path.abspath(os.path.join(os.getcwd(), '..', 'src')))
print('sys.path[0]=', sys.path[0])
try:
    import agriconnect.protocols.mcp as m
    print('Imported agriconnect.protocols.mcp OK')
    print('MCPContextServer attr exists:', hasattr(m, 'MCPContextServer'))
    print('MCPContextServer value:', getattr(m, 'MCPContextServer', None))
except Exception as e:
    print('Import failed:', repr(e))
    raise
