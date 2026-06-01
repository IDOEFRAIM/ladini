import traceback, os
out = os.path.abspath('backend/tmp/runtime_import_direct.txt')
with open(out, 'w', encoding='utf-8') as f:
    f.write('target path: ' + out + '\n')
    try:
        import importlib
        f.write('ABOUT TO IMPORT runtime\n')
        f.flush()
        importlib.import_module('agriconnect.infrastructure.mcp.runtime')
        f.write('IMPORTED OK\n')
    except Exception:
        f.write('EXCEPTION:\n')
        traceback.print_exc(file=f)
print('written', out)
