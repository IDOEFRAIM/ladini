import importlib,traceback,sys
out='backend/tmp/import_check.txt'
try:
    importlib.import_module('agriconnect.infrastructure.mcp.runtime')
    with open(out,'w',encoding='utf8') as f:
        f.write('IMPORT_OK')
except Exception:
    with open(out,'w',encoding='utf8') as f:
        traceback.print_exc(file=f)
    sys.exit(1)
