import traceback, sys
out='backend/tmp/runtime_import_trace.txt'
with open(out,'w',encoding='utf-8') as f:
    f.write('START\n')
    f.flush()
    try:
        import importlib
        f.write('ABOUT TO IMPORT runtime\n')
        f.flush()
        importlib.import_module('agriconnect.infrastructure.mcp.runtime')
        f.write('IMPORTED OK\n')
    except Exception:
        f.write('EXCEPTION:\n')
        traceback.print_exc(file=f)
    f.write('END\n')
print('written')
