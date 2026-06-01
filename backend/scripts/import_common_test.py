try:
    from agriconnect.domain import models as m
    print('OK', hasattr(m, '_uuid4'))
except Exception:
    import traceback
    traceback.print_exc()
    raise
