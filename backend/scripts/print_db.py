from agriconnect.core.settings import settings
print('DATABASE_URL->', repr(settings.DATABASE_URL))
print('DO_DATABASE_URL->', repr(getattr(settings, 'DO_DATABASE_URL', None)))
print('DB_SSL_MODE->', getattr(settings, 'DB_SSL_MODE', None))
