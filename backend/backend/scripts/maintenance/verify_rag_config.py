import sys
import os
# Add backend/src to path
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from agriconnect.core.settings import settings
from agriconnect.rag import config

print(f"S3_BUCKET from settings: '{settings.S3_BUCKET}'")
print(f"RAW_DATA_DIR from rag.config: '{config.RAW_DATA_DIR}'")

if str(config.RAW_DATA_DIR).startswith("s3://"):
    print("✅ Configuration is pointing to S3.")
else:
    print("❌ Configuration is still pointing to local filesystem.")
