import traceback
try:
    from agriconnect.core.settings import settings
    out = {
        'DATABASE_URL': getattr(settings, 'DATABASE_URL', None),
        'DO_DATABASE_URL': getattr(settings, 'DO_DATABASE_URL', None),
        'DB_SSL_MODE': getattr(settings, 'DB_SSL_MODE', None),
        'BASE_DIR': getattr(settings, 'BASE_DIR', None),
    }
    with open('backend/tmp/db_settings_out.txt','w',encoding='utf-8') as f:
        f.write(str(out))
except Exception:
    with open('backend/tmp/db_settings_out.txt','w',encoding='utf-8') as f:
        traceback.print_exc(file=f)
print('written')
