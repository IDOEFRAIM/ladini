import sys
sys.path.insert(0, 'backend/src')
try:
    from agriconnect.tools import refine
    print('refine import ok')
except Exception as e:
    print('refine import error:', e)
    raise
