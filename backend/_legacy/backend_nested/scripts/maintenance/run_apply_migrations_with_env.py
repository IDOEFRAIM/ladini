import runpy
import re
import os
import sys
from pathlib import Path


def load_env(path: Path) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(.*?)=(.*)$", line)
        if not m:
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        # strip surrounding quotes
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        os.environ[key] = val


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    env_path = root / ".env"
    load_env(env_path)
    os.chdir(root)
    script_path = root / "scripts" / "apply_sql_migrations.py"
    if not script_path.exists():
        print("apply_sql_migrations.py not found", file=sys.stderr)
        sys.exit(1)
    runpy.run_path(str(script_path), run_name="__main__")


if __name__ == "__main__":
    main()
