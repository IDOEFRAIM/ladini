import os
import json

# Set DB env vars programmatically (use the URL-provided credentials)
os.environ['DB_HOST'] = 'db-postgresql-fra1-38999-do-user-31802282-0.a.db.ondigitalocean.com'
os.environ['DB_PORT'] = '25060'
os.environ['DB_NAME'] = 'defaultdb'
os.environ['DB_USER'] = 'doadmin'
os.environ['DB_PASSWORD'] = 'AVNS_-TtxFZrkDLQSQ2W8UiX'
# For immediate testing we use sslmode=require (no cert verification).
# For production, set to 'verify-full' and ensure DB_SSLROOTCERT points to a valid CA bundle.
os.environ['DB_SSLMODE'] = 'require'
os.environ['DB_CONNECT_TIMEOUT'] = '30'
# point to the repo CA bundle for DigitalOcean managed Postgres
root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
# For now do not pass a root cert so we rely on sslmode=require behaviour
os.environ['DB_SSLROOTCERT'] = ''
# Use a FARMERS_SQL tailored to the provided schema
os.environ['FARMERS_SQL'] = (
    "SELECT u.id, u.phone, u.latitude as lat, u.longitude as lon, COALESCE(uc.culture_name, '') as crop, u.zone_id "
    "FROM auth.users u LEFT JOIN auth.user_cultures uc ON uc.user_id = u.id "
    "WHERE u.phone IS NOT NULL "
    "  AND ( (u.latitude IS NOT NULL AND u.longitude IS NOT NULL) OR u.zone_id IS NOT NULL ) "
    "LIMIT 100"
)

# Enable real sending: publish directly to farmer phone numbers (no topic)
os.environ['SNS_TOPIC_ARN'] = ''
# Ensure AWS creds are available to boto3 (update if you want to use a topic instead)
os.environ['AWS_ACCESS_KEY_ID'] = os.environ.get('AWS_ACCESS_KEY_ID', 'REDACTED_AWS_ACCESS_KEY_ID')
os.environ['AWS_SECRET_ACCESS_KEY'] = os.environ.get('AWS_SECRET_ACCESS_KEY', 'gZzBOHsqqEyFcVRenpOJvagfGRGua4DjRO4YlDo6')
os.environ['AWS_REGION'] = os.environ.get('AWS_REGION', 'us-east-1')

from lambda_function import lambda_handler

if __name__ == '__main__':
    print('Invoking lambda_handler with DB connection...')
    res = lambda_handler({}, None)
    print(json.dumps(res, ensure_ascii=False, indent=2))
