import sys
import os
from pathlib import Path

# Add src to path
current_dir = Path(__file__).resolve().parent
backend_dir = current_dir.parent
src_dir = backend_dir / "src"
sys.path.insert(0, str(src_dir))

print(f"Added to sys.path: {src_dir}")

try:
    from agriconnect.core.settings import settings
    print(f"Settings imported successfully.")
    
    env_file = settings.model_config.get("env_file")
    print(f"Configured env_file: {env_file}")
    
    if env_file:
        ef = Path(env_file)
        if ef.exists():
            print(f"Executing env_file exists: {ef}")
            content = ef.read_text(encoding='utf-8')
            print(f"--- CONTENT START ---")
            print(content[:200]) # First 200 chars
            print(f"--- CONTENT END ---")
        else:
             print(f"❌ Configured env_file DOES NOT exist: {ef}")
    else:
        print("No env_file configured in model_config.")

    print(f"Settings.S3_BUCKET value: '{settings.S3_BUCKET}'")

except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
