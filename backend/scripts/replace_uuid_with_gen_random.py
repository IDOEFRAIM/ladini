#!/usr/bin/env python3
"""
Replace uuid_generate_v4() with gen_random_uuid() in .sql files
and add CREATE EXTENSION IF NOT EXISTS pgcrypto; if missing.

This script makes a timestamped backup for each modified file.
"""
import io
import os
import shutil
import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SEARCH_DIRS = [os.path.join(ROOT, 'src', 'agriconnect', 'database')]

EXT_LINE = 'CREATE EXTENSION IF NOT EXISTS pgcrypto;'


def find_sql_files():
    files = []
    for base in SEARCH_DIRS:
        for dirpath, _, filenames in os.walk(base):
            for fn in filenames:
                if fn.endswith('.sql'):
                    files.append(os.path.join(dirpath, fn))
    return files


def backup_file(path):
    ts = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    bak = f"{path}.bak.{ts}"
    shutil.copy2(path, bak)
    return bak


def process_file(path):
    with io.open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    if 'uuid_generate_v4' not in content:
        return False

    bak = backup_file(path)
    new = content.replace('uuid_generate_v4()', 'gen_random_uuid()')

    # Ensure extension line exists (case-insensitive check)
    if EXT_LINE.lower() not in new.lower():
        # Insert after initial comment block or at top
        lines = new.splitlines()
        insert_at = 0
        # skip leading empty lines
        while insert_at < len(lines) and lines[insert_at].strip() == '':
            insert_at += 1
        # skip leading comment lines
        while insert_at < len(lines) and lines[insert_at].lstrip().startswith('--'):
            insert_at += 1
        lines.insert(insert_at, EXT_LINE)
        new = '\n'.join(lines) + '\n'

    with io.open(path, 'w', encoding='utf-8') as f:
        f.write(new)

    print(f"Patched: {path} (backup: {bak})")
    return True


def main():
    files = find_sql_files()
    changed = []
    for p in files:
        try:
            if process_file(p):
                changed.append(p)
        except Exception as e:
            print(f"ERROR processing {p}: {e}")

    print('\nSummary:')
    print(f'Total SQL scanned: {len(files)}')
    print(f'Total modified: {len(changed)}')
    for c in changed:
        print(' - ' + c)


if __name__ == '__main__':
    main()
