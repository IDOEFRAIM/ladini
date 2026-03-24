import sys
import os
from pathlib import Path

# Fix encoding
try:
    sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    pass

# Add backend/src to path
sys.path.append(str(Path(__file__).resolve().parent.parent / "src"))

print(f"Current working directory: {os.getcwd()}")

# 1. Check .env file
base_dir = Path(__file__).resolve().parent.parent
env_file = base_dir / ".env"
print(f"Looking for .env at: {env_file}")

if env_file.exists():
    print("✅ .env file exists.")
    try:
        content = env_file.read_text(encoding="utf-8")
        if "S3_BUCKET" in content:
            print("  - S3_BUCKET key found in file.")
        else:
            print("  - ❌ S3_BUCKET key NOT found in file.")
            
        if "AWS_S3_BUCKET" in content:
            print("  - AWS_S3_BUCKET key found in file.")
    except Exception as e:
        print(f"Warning: could not read .env text: {e}")
else:
    print("❌ .env file NOT found.")

# 2. Check Loaded Env Vars (before dotenv)
print(f"Env var S3_BUCKET (initial): '{os.environ.get('S3_BUCKET', '')}'")

# 3. Load Dotenv manually
from dotenv import load_dotenv
load_dotenv(env_file, override=True)
print(f"Env var S3_BUCKET (manual load): '{os.environ.get('S3_BUCKET', '')}'")

# 4. Check Settings
try:
    from agriconnect.core.settings import settings
    # Force reload of pydantic settings if needed? No, it's a singleton.
    # But if env vars changed, it won't update unless we re-instantiate.
    # Check what it has.
    print(f"Settings.S3_BUCKET: '{settings.S3_BUCKET}'")
except Exception as e:
    print(f"Error loading settings: {e}")

# 5. Check RAG Config
try:
    import importlib
    from agriconnect.rag import config
    importlib.reload(config)
    print(f"RAG RAW_DATA_DIR: '{config.RAW_DATA_DIR}'")
    
    if str(config.RAW_DATA_DIR).startswith("s3://"):
         print("✅ RAG is configured for S3.")
    else:
         print("❌ RAG is configured for LOCAL.")
except Exception as e:
    print(f"Error loading RAG config: {e}")
