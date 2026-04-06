import sys
sys.path.insert(0, 'backend/src')
try:
    from agriconnect.tools import sentinelle
    print('import ok')
except Exception as e:
    print('import error:', e)
    raise
