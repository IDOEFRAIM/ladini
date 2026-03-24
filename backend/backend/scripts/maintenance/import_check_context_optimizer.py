import sys
sys.path.insert(0, 'backend/src')
try:
    from agriconnect.services.memory import context_optimizer
    print('context_optimizer import ok')
except Exception as e:
    print('context_optimizer import error:', e)
    raise
