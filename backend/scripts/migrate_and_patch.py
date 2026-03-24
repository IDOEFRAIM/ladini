import shutil
import os
from pathlib import Path
import sys

# Windows path fix
sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = Path(__file__).resolve().parent.parent # backend/scripts -> backend
SRC_SCRIPTS_DIR = BASE_DIR / "src" / "agriconnect" / "scripts"
TARGET_DIR = BASE_DIR / "scripts"

MAPPING = {
    "core": ["seed_db.py", "seed_zones.py", "init_marketplace_tables.py", "patch_marketplace_tables.py", "seed_crop_sheets.py"],
    "tasks": ["run_evaluation.py", "extract_vcf_to_csv.py"],
    "maintenance": ["check_tables.py", "fix_climatic_regions.py", "validate_alignment.py", "verify_database.py"],
}

NEW_IMPORT_BLOCK = """
import sys
from pathlib import Path
# Add project source to path (3 levels up from <category>/script.py -> backend/src)
sys.path.append(str(Path(__file__).resolve().parents[2] / "src"))
"""

def migrate():
    if not SRC_SCRIPTS_DIR.exists():
        print(f"Source dir {SRC_SCRIPTS_DIR} does not exist.")
        return

    for category, files in MAPPING.items():
        dest_dir = TARGET_DIR / category
        dest_dir.mkdir(parents=True, exist_ok=True)
        
        for filename in files:
            src_file = SRC_SCRIPTS_DIR / filename
            if not src_file.exists():
                print(f"Skipping missing file: {src_file}")
                continue
                
            dest_file = dest_dir / filename
            
            try:
                content = src_file.read_text(encoding='utf-8')
                
                # Replace old sys.path hack
                if "sys.path.insert" in content:
                    lines = content.splitlines()
                    new_lines = []
                    replaced = False
                    for line in lines:
                        if "sys.path.insert" in line and "os.path.dirname" in line and not replaced:
                            new_lines.append(NEW_IMPORT_BLOCK)
                            replaced = True
                        elif "sys.path.insert" in line and "os.path.dirname" in line and replaced:
                             continue # Skip duplicate inserts
                        else:
                            new_lines.append(line)
                    content = "\n".join(new_lines)
                else:
                    # Prepend if not found
                    content = NEW_IMPORT_BLOCK + "\n" + content
                
                dest_file.write_text(content, encoding='utf-8')
                print(f"Migrated & Patched: {filename} -> {category}/")
                
                # Verify write before delete
                if dest_file.exists() and dest_file.stat().st_size > 0:
                    src_file.unlink()
                else:
                    print(f"Failed to write destination {dest_file}, keeping source.")

            except Exception as e:
                print(f"Error processing {filename}: {e}")

    # Clean up empty dir
    try:
        remaining = list(SRC_SCRIPTS_DIR.glob("*"))
        if len(remaining) == 1 and remaining[0].name == "__init__.py":
            remaining[0].unlink()
            SRC_SCRIPTS_DIR.rmdir()
            print("Removed empty source directory.")
        elif len(remaining) == 0:
            SRC_SCRIPTS_DIR.rmdir()
            print("Removed empty source directory.")
        else:
            print(f"Source directory not empty: {[f.name for f in remaining]}")
    except Exception as e:
        print(f"Error removing source dir: {e}")

if __name__ == "__main__":
    migrate()
