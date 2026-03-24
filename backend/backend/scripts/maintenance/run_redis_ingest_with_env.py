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
        os.environ[key] = val


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    env_path = root / ".env"
    load_env(env_path)
    # ensure working directory is backend so collect_documents() finds files
    os.chdir(root)
    # forward CLI args if provided, otherwise default to a safe limited run
    if argv is None:
        if len(sys.argv) > 1:
            argv = sys.argv[1:]
        else:
            argv = ["--ingest-all", "--limit", "100"]
    sys.argv = ["scripts/redis_ingest.py"] + argv
    script_path = root / "scripts" / "redis_ingest.py"
    runpy.run_path(str(script_path), run_name="__main__")


if __name__ == "__main__":
    main()
