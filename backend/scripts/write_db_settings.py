from agriconnect.core.settings import settings
out = {
    'DATABASE_URL': settings.DATABASE_URL,
    'DO_DATABASE_URL': getattr(settings, 'DO_DATABASE_URL', None),
    'DB_SSL_MODE': getattr(settings, 'DB_SSL_MODE', None),
}
with open('backend/tmp/db_settings_out.txt','w',encoding='utf-8') as f:
    f.write(str(out))
print('WROTE')
