import traceback
try:
    import importlib
    importlib.import_module('agriconnect.infrastructure.mcp.runtime')
    print('IMPORT_OK')
except Exception:
    traceback.print_exc()
