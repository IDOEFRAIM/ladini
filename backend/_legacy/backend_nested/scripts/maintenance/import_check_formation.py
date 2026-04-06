import sys
sys.path.insert(0, 'backend/src')
try:
    from agriconnect.graphs.nodes import formation
    print('formation import ok')
except Exception as e:
    print('formation import error:', e)
    raise
